"""Guided-setup writer: saves the owner's answers one block at a time and composes the instance files from them.

    state/setup.json          checkpoint: per block a status and the hash of the saved answer, plus the next question
    state/answers/<block>.json  the answers themselves (the composed org.json / policy.json / reporting.json come from these)

Rules this module enforces, so that no chat session has to be trusted with them:
  * an answer is validated before it is saved; a bad answer changes nothing
  * a saved answer is never replaced unless the caller says `change`; the old one is kept in state/answers/history/
  * the checkpoint is bound to the answer by hash; an answer edited behind the writer's back is noticed and the block
    goes back to needs_action instead of being trusted
  * resuming reads this state, never a chat history
  * no credential is ever a field of any block
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import check, config, report
from .config import SETUP_BLOCKS, SLUG, block_hash
from .fsio import atomic_write, file_lock

AUTH_METHODS = ("claude.ai", "oauth_token")
DELIVERY = ("local", "discord", "email")
PHASES = ("collecting", "validated", "demo_running", "demo_ready")


class SetupError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _answer_path(root: Path, block: str) -> Path:
    return root / "state" / "answers" / f"{block}.json"


def _read_answer(root: Path, block: str):
    try:
        return json.loads(_answer_path(root, block).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data) -> None:
    atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _fresh_state() -> dict:
    return {"schema_version": config.SCHEMA_VERSION, "revision": 0, "phase": "collecting",
            "pending_question": SETUP_BLOCKS[0], "demo_run_id": None,
            "blocks": {name: {"status": "pending", "hash": None} for name in SETUP_BLOCKS}}


def load_state(root: Path) -> dict | None:
    path = root / "state" / "setup.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        config.validate("state/setup.json", state)
    except (json.JSONDecodeError, config.ConfigError) as exc:
        errors = exc.errors if isinstance(exc, config.ConfigError) else [f"state/setup.json: not valid JSON: {exc.msg}"]
        raise SetupError(errors + ["state/setup.json is damaged; it is not rewritten automatically. "
                                   "Fix or move it aside (saved answers stay in state/answers/)."]) from None
    return state


def _store(root: Path, state: dict) -> None:
    state["revision"] += 1
    state["pending_question"] = next((b for b in SETUP_BLOCKS if state["blocks"][b]["status"] != "accepted"), None)
    config.save_file(root, "state/setup.json", state, overwrite=True)


def start(root: Path) -> dict:
    """Create the instance directory and an empty checkpoint; an existing checkpoint is returned untouched."""
    root.mkdir(parents=True, exist_ok=True)
    existing = load_state(root)
    if existing is not None:
        return existing
    state = _fresh_state()
    config.save_file(root, "state/setup.json", state)
    return state


# --- validation -------------------------------------------------------------------------------------------------

def _need(answer, fields: dict, optional: tuple = ()) -> list[str]:
    """Shape check for a flat answer: `fields` maps name -> python type (or tuple of types)."""
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    errors = [f"{k}: unknown field" for k in answer if k not in fields]
    for name, kind in fields.items():
        if name not in answer:
            if name not in optional:
                errors.append(f"{name}: missing required field")
        elif isinstance(answer[name], bool) and kind is not bool:
            errors.append(f"{name}: wrong type")
        elif not isinstance(answer[name], kind):
            errors.append(f"{name}: wrong type")
        elif isinstance(answer[name], str) and not answer[name].strip():
            errors.append(f"{name}: must not be empty")
    return errors


def _slug_errors(where: str, value) -> list[str]:
    return [] if isinstance(value, str) and re.fullmatch(SLUG, value) else [f"{where}: must be lowercase letters, digits and hyphens (got {value!r})"]


def _schema_errors(name: str, data: dict) -> list[str]:
    try:
        config.validate(name, data)
    except config.ConfigError as exc:
        return exc.errors
    return []


def _policy(spending=None, windows=None, rules=None, concurrency=1) -> dict:
    return {"schema_version": config.SCHEMA_VERSION, "live_enabled": False, "live_consent": None, "max_concurrency": concurrency,
            "spending": {**(spending or {"currency": "USD", "period": "month", "cap": 0}), "paid_apis_allowed": False},
            "subscription_windows": windows or [{"name": "w", "reserve_percent": 0, "max_share_percent": 100,
                                                 "max_observation_age_minutes": 60}],
            "hard_gates": {key: True for key in ("external_contact", "deploy", "trading", "purchases")},
            "rules": rules or {"text": "x", "version": 1}}


def _reporting(tz="UTC", times=None, quiet=None, preferred="local") -> dict:
    return {"schema_version": config.SCHEMA_VERSION, "timezone": tz, "report_times": times or [],
            "quiet_hours": quiet, "delivery": {"active": "local", "preferred": preferred}}


def validate_block(block: str, answer, others: dict) -> list[str]:
    """Every problem with `answer` for `block`, given the answers of the other accepted blocks."""
    if block == "identity":
        return _need(answer, {"organization_name": str, "owner_name": str, "instance_id": str}) or \
            _slug_errors("instance_id", answer["instance_id"])
    if block == "goal":
        return _need(answer, {"goal": str})
    if block == "lines":
        errors = _need(answer, {"lines": list})
        if errors:
            return errors
        ids = []
        for i, line in enumerate(answer["lines"]):
            errors += [f"lines[{i}].{e}" for e in _need(line, {"id": str, "name": str})]
            if not errors:
                errors += _slug_errors(f"lines[{i}].id", line["id"])
                ids.append(line["id"])
        if not answer["lines"]:
            errors.append("lines: at least one line is needed")
        if len(ids) != len(set(ids)):
            errors.append("lines: duplicate id")
        return errors
    if block == "projects":
        errors = _need(answer, {"projects": list})
        if errors:
            return errors
        line_ids = {l["id"] for l in others.get("lines", {}).get("lines", [])}
        if not line_ids:
            return ["projects: the lines block must be saved first"]
        ids = []
        for i, project in enumerate(answer["projects"]):
            bad = [f"projects[{i}].{e}" for e in _need(project, {"id": str, "name": str, "line": str, "path": (str, type(None))},
                                                        optional=("path",))]
            errors += bad
            if not bad:
                errors += _slug_errors(f"projects[{i}].id", project["id"])
                ids.append(project["id"])
                if project["line"] not in line_ids:
                    errors.append(f"projects[{i}].line: '{project['line']}' is not a saved line")
        errors += [f"lines: '{l}' has no project (every line needs at least one)" for l in sorted(line_ids)
                   if not any(isinstance(p, dict) and p.get("line") == l for p in answer["projects"])]
        if len(ids) != len(set(ids)) or set(ids) & line_ids:
            errors.append("projects: ids must be unique and must not repeat a line id")
        return errors
    if block == "weights":
        errors = _need(answer, {"lines": dict, "projects": dict})
        if errors:
            return errors
        want = {"lines": {l["id"] for l in others.get("lines", {}).get("lines", [])},
                "projects": {p["id"] for p in others.get("projects", {}).get("projects", [])}}
        if not want["lines"] or not want["projects"]:
            return ["weights: save lines and projects first"]
        for kind in ("lines", "projects"):
            for ident in sorted(want[kind] - set(answer[kind])):
                errors.append(f"{kind}.{ident}: no weight given")
            for ident, weight in answer[kind].items():
                if ident not in want[kind]:
                    errors.append(f"{kind}.{ident}: not a saved {kind[:-1]}")
                elif isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                    errors.append(f"{kind}.{ident}: weight must be a number above 0")
        return errors
    if block == "time":
        errors = _need(answer, {"timezone": str})
        if errors:
            return errors
        errors = _schema_errors("reporting.json", _reporting(tz=answer["timezone"]))
        if not errors:
            try:
                report.zone(answer["timezone"])
            except report.ReportError:
                errors = [f"timezone: '{answer['timezone']}' cannot be resolved on this machine. Offer the user two choices: "
                          f"change it to \"UTC\", or install the tzdata package (python -m pip install tzdata) and save "
                          f"this answer again"]
        return errors
    if block == "reporting":
        errors = _need(answer, {"report_times": list, "quiet_hours": (dict, type(None)), "preferred": str})
        return errors or _schema_errors("reporting.json", _reporting(times=answer["report_times"], quiet=answer["quiet_hours"],
                                                                       preferred=answer["preferred"]))
    if block == "spending":
        errors = _need(answer, {"currency": str, "period": str, "cap": (int, float)})
        return errors or _schema_errors("policy.json", _policy(spending=answer))
    if block == "subscription":
        errors = _need(answer, {"windows": list, "max_concurrency": int})
        return errors or _schema_errors("policy.json", _policy(windows=answer["windows"], concurrency=answer["max_concurrency"]))
    if block == "rules":
        errors = _need(answer, {"text": str})
        return errors or _schema_errors("policy.json", _policy(rules={"text": answer["text"], "version": 1}))
    if block == "install":
        errors = _need(answer, {"auth_method": str})
        if not errors and answer["auth_method"] not in AUTH_METHODS:
            errors = [f"auth_method: must be one of {', '.join(AUTH_METHODS)}"]
        return errors
    if block == "confirm":
        errors = _need(answer, {"confirmed": bool})
        if not errors and answer["confirmed"] is not True:
            errors = ["confirmed: must be true"]
        missing = [b for b in SETUP_BLOCKS if b != "confirm" and b not in others]
        if missing:
            errors.append("confirm: these blocks are not accepted yet: " + ", ".join(missing))
        return errors
    return [f"unknown block '{block}'"]


def _live_problems(root: Path, state: dict) -> dict[str, list[str]]:
    """Re-check every saved answer against the others, in order. A block whose answer no longer holds is reported."""
    accepted: dict[str, object] = {}
    problems: dict[str, list[str]] = {}
    for block in SETUP_BLOCKS:
        entry = state["blocks"][block]
        if entry["status"] == "pending" and _read_answer(root, block) is None:
            continue
        answer = _read_answer(root, block)
        if answer is None:
            problems[block] = ["the saved answer file is missing"]
        elif entry["status"] == "accepted" and block_hash(answer) != entry["hash"]:
            problems[block] = ["the saved answer was changed outside the setup writer; save it again to accept it"]
        else:
            errors = validate_block(block, answer, accepted)
            if errors:
                problems[block] = errors
            elif entry["status"] == "accepted":
                accepted[block] = answer
    return problems


def _sync(root: Path, state: dict) -> dict[str, list[str]]:
    """Move blocks whose answer no longer holds to needs_action (only ever away from accepted)."""
    problems = _live_problems(root, state)
    changed = False
    for block, errors in problems.items():
        entry = state["blocks"][block]
        if entry["status"] == "accepted":
            state["blocks"][block] = {"status": "needs_action", "hash": None}
            changed = True
    if changed:
        _store(root, state)
    return problems


def _accepted_answers(root: Path, state: dict) -> dict:
    return {b: _read_answer(root, b) for b in SETUP_BLOCKS if state["blocks"][b]["status"] == "accepted"}


# --- operations ------------------------------------------------------------------------------------------------

def _require(root: Path) -> dict:
    state = load_state(root)
    if state is None:
        raise SetupError([f"setup has not been started in {root}; run `setup {root} start` first"])
    return state


def save(root: Path, block: str, answer, change: bool = False) -> str:
    """Validate and save one block. Returns a one-line description of what happened."""
    state = _require(root)
    if block not in SETUP_BLOCKS:
        raise SetupError([f"unknown block '{block}'; blocks are: {', '.join(SETUP_BLOCKS)}"])
    _sync(root, state)
    entry = state["blocks"][block]
    if entry["status"] == "accepted" and not change:
        raise SetupError([f"{block} is already saved and is kept. If the user wants to change it, ask them first, "
                          f"then save again with --change"])
    others = {b: a for b, a in _accepted_answers(root, state).items() if b != block}
    errors = validate_block(block, answer, others)
    if errors:
        if block == "time" and any("cannot be resolved" in e for e in errors):
            state["blocks"]["time"] = {"status": "needs_action", "hash": None}
            _store(root, state)
        raise SetupError(errors)
    old = _read_answer(root, block)
    if entry["status"] == "accepted" and old is not None and block_hash(old) == block_hash(answer):
        return f"{block}: unchanged"
    if entry["status"] == "accepted" and old is not None:
        _write_json(root / "state" / "answers" / "history" / f"{block}.{block_hash(old)[:12]}.json", old)
    with file_lock(root / ".orgcore"):  # a crash after this write leaves the answer on disk but the block unaccepted
        _write_json(_answer_path(root, block), answer)
    state["blocks"][block] = {"status": "accepted", "hash": block_hash(answer)}
    _store(root, state)
    after = _sync(root, state)
    stale = [b for b in after if b != block]
    return f"{block}: saved" + (f"; these later blocks no longer hold and need attention: {', '.join(stale)}" if stale else "")


def accept(root: Path, block: str) -> str:
    """Accept the answer already on disk for `block` (used to finish a save that was interrupted)."""
    state = _require(root)
    answer = _read_answer(root, block)
    if answer is None:
        raise SetupError([f"{block}: no saved answer on disk"])
    if state["blocks"][block]["status"] == "accepted":
        return f"{block}: already accepted"
    others = {b: a for b, a in _accepted_answers(root, state).items() if b != block}
    errors = validate_block(block, answer, others)
    if errors:
        raise SetupError(errors)
    state["blocks"][block] = {"status": "accepted", "hash": block_hash(answer)}
    _store(root, state)
    return f"{block}: accepted"


def set_phase(root: Path, phase: str, demo_run_id: str | None = None) -> None:
    state = _require(root)
    if phase not in PHASES:
        raise SetupError([f"unknown phase '{phase}'"])
    state["phase"] = phase
    if demo_run_id is not None:
        state["demo_run_id"] = demo_run_id
    _store(root, state)


def describe(root: Path) -> list[str]:
    state = _require(root)
    problems = _sync(root, state)
    state = _require(root)
    lines = [f"Setup in {root}", f"Phase: {state['phase']}   revision {state['revision']}"]
    for block in SETUP_BLOCKS:
        status = state["blocks"][block]["status"]
        extra = ""
        if block in problems:
            extra = f"  <- {problems[block][0]}"
        elif status == "pending" and _read_answer(root, block) is not None:
            extra = "  <- an answer is on disk but not accepted; run accept"
        lines.append(f"  {block:<13}{status}{extra}")
    lines.append(f"Next question: {state['pending_question'] or 'none, every block is accepted'}")
    return lines


def show(root: Path, block: str | None = None) -> str:
    _require(root)
    blocks = [block] if block in SETUP_BLOCKS else [b for b in SETUP_BLOCKS if _read_answer(root, b) is not None]
    return json.dumps({b: _read_answer(root, b) for b in blocks}, indent=2, ensure_ascii=False)


def compose(root: Path) -> list[str]:
    """Write org.json, policy.json, reporting.json and a task board from the accepted answers. Existing files that
    differ are never replaced. Returns the files written."""
    state = _require(root)
    problems = _sync(root, state)
    state = _require(root)
    open_blocks = [b for b in SETUP_BLOCKS if state["blocks"][b]["status"] != "accepted"]
    if open_blocks:
        raise SetupError(["these blocks are not accepted yet: " + ", ".join(open_blocks)] +
                         [f"{b}: {e}" for b in open_blocks for e in problems.get(b, [])[:1]])
    a = _accepted_answers(root, state)
    projects = a["projects"]["projects"]
    org = {"schema_version": config.SCHEMA_VERSION, "instance_id": a["identity"]["instance_id"],
           "organization_name": a["identity"]["organization_name"], "owner_name": a["identity"]["owner_name"],
           "lines": [{"id": line["id"], "name": line["name"], "weight": a["weights"]["lines"][line["id"]],
                      "projects": [{"id": p["id"], "name": p["name"], "weight": a["weights"]["projects"][p["id"]],
                                    **({"path": p["path"]} if p.get("path") else {})}
                                   for p in projects if p["line"] == line["id"]]} for line in a["lines"]["lines"]]}
    policy = _policy(spending=a["spending"], windows=a["subscription"]["windows"],
                     rules={"text": a["rules"]["text"], "version": 1}, concurrency=a["subscription"]["max_concurrency"])
    reporting = _reporting(a["time"]["timezone"], a["reporting"]["report_times"], a["reporting"]["quiet_hours"],
                           a["reporting"]["preferred"])
    board = "# Backlog\n\n" + "\n".join(f"## Line: {l['id']}\nWeight: {l['weight']:g}%\n" for l in org["lines"])
    for name, data in (("org.json", org), ("policy.json", policy), ("reporting.json", reporting)):
        config.validate(name, data)
    written = []
    for name, data in (("org.json", org), ("policy.json", policy), ("reporting.json", reporting)):
        path = root / name
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != data:
                raise SetupError([f"{name} already exists and differs from the saved answers; it is not replaced. "
                                  f"Move it aside or compare it with state/answers/ first."])
            continue
        config.save_file(root, name, data)
        written.append(name)
    board_path = root / check.BOARD
    if not board_path.exists():
        atomic_write(board_path, board.encode("utf-8"))
        written.append(check.BOARD)
    errors, _ = check.check_instance(root)
    if errors:
        raise SetupError(errors)
    if state["phase"] == "collecting":
        set_phase(root, "validated")
    return written
