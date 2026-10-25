"""Instance configuration: four JSON files with a strict schema. No personal defaults; a missing field is an error.

The instance directory lives outside the repository. It is never guessed: pass it explicitly or set ORGCORE_INSTANCE.

    org.json            names, lines, projects, relative weights
    policy.json         spending, subscription windows, hard gates, consent
    reporting.json      time zone, report times, quiet hours, delivery
    state/setup.json    guided-setup checkpoint
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .fsio import atomic_write, file_lock

SCHEMA_VERSION = 1
INSTANCE_ENV = "ORGCORE_INSTANCE"
SLUG = r"[a-z0-9][a-z0-9-]*"
HHMM = r"(?:[01]\d|2[0-3]):[0-5]\d"
SETUP_BLOCKS = ("identity", "goal", "lines", "projects", "weights", "time", "reporting", "spending", "subscription",
                "rules", "install", "confirm")


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _str(**kw):
    return {"t": "str", **kw}


def _num(**kw):
    return {"t": "num", **kw}


def _obj(fields: dict, optional: tuple = ()):
    return {"t": "obj", "fields": fields, "optional": set(optional)}


def _list(items: dict, min: int = 0):
    return {"t": "list", "items": items, "min": min}


VERSION = {"t": "int", "const": SCHEMA_VERSION}

SCHEMAS = {
    "org.json": _obj({
        "schema_version": VERSION,
        "instance_id": _str(pattern=SLUG),
        "organization_name": _str(min=1),
        "owner_name": _str(min=1),
        "lines": _list(_obj({
            "id": _str(pattern=SLUG),
            "name": _str(min=1),
            "weight": _num(gt=0),
            "projects": _list(_obj({
                "id": _str(pattern=SLUG),
                "name": _str(min=1),
                "weight": _num(gt=0),
                "path": {"t": "null_or", "spec": _str(min=1)},
                "demo": {"t": "bool"},
            }, optional=("path", "demo")), min=1),
        }), min=1),
    }),
    "policy.json": _obj({
        "schema_version": VERSION,
        "live_enabled": {"t": "bool"},
        "live_consent": {"t": "null_or", "spec": _obj({
            "granted_at": _str(pattern=r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"),
            "projects": _list(_str(pattern=SLUG), min=1),
            "task_scope": _str(min=1),
        })},
        "max_concurrency": {"t": "int", "ge": 1},
        "spending": _obj({
            "currency": _str(pattern=r"[A-Z]{3}"),
            "period": {"t": "enum", "values": ["day", "week", "month"]},
            "cap": _num(ge=0),
            "paid_apis_allowed": {"t": "bool", "const": False},
        }),
        "subscription_windows": _list(_obj({
            "name": _str(min=1),
            "reserve_percent": _num(ge=0, le=100),
            "max_share_percent": _num(gt=0, le=100),
            "max_observation_age_minutes": {"t": "int", "ge": 1},
            "length_hours": _num(gt=0),  # optional: with pacing_slack_percent turns on the pacing line
            "pacing_slack_percent": _num(ge=0, le=100),
        }, optional=("length_hours", "pacing_slack_percent")), min=1),
        "hard_gates": _obj({key: {"t": "bool", "const": True}
                            for key in ("external_contact", "deploy", "trading", "purchases")}),
        "rules": _obj({"text": _str(min=1), "version": {"t": "int", "ge": 1}}),
    }),
    "reporting.json": _obj({
        "schema_version": VERSION,
        "timezone": _str(pattern=r"UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+"),
        "report_times": _list(_str(pattern=HHMM)),  # [] means "no scheduled reports", stated explicitly
        "quiet_hours": {"t": "null_or", "spec": _obj({"start": _str(pattern=HHMM), "end": _str(pattern=HHMM)})},
        "delivery": _obj({
            "active": {"t": "enum", "values": ["local"]},
            "preferred": {"t": "enum", "values": ["local", "discord", "email"]},
        }),
    }),
    "state/setup.json": _obj({
        "schema_version": VERSION,
        "revision": {"t": "int", "ge": 0},
        "phase": {"t": "enum", "values": ["collecting", "validated", "demo_running", "demo_ready", "live_waiting_for_usage",
                                                       "live_ready"]},
        "pending_question": {"t": "null_or", "spec": {"t": "enum", "values": list(SETUP_BLOCKS)}},
        "demo_run_id": {"t": "null_or", "spec": _str(min=1)},
        "blocks": _obj({name: _obj({
            "status": {"t": "enum", "values": ["pending", "accepted", "needs_action"]},
            "hash": {"t": "null_or", "spec": _str(pattern=r"[0-9a-f]{64}")},
        }) for name in SETUP_BLOCKS}),
    }),
}


def _kind(value) -> str:
    return {dict: "an object", list: "a list", str: "a string", bool: "a boolean", int: "an integer",
            float: "a number", type(None): "null"}.get(type(value), type(value).__name__)


def _validate(value, spec: dict, where: str, errors: list[str]) -> None:
    t = spec["t"]
    if t == "null_or":
        if value is not None:
            _validate(value, spec["spec"], where, errors)
        return
    if t == "obj":
        if not isinstance(value, dict):
            errors.append(f"{where or 'file'}: must be an object, got {_kind(value)}")
            return
        for name, sub in spec["fields"].items():
            if name in value:
                _validate(value[name], sub, f"{where}.{name}" if where else name, errors)
            elif name not in spec["optional"]:
                errors.append(f"{where + '.' if where else ''}{name}: missing required field")
        for name in value:
            if name not in spec["fields"]:
                errors.append(f"{where + '.' if where else ''}{name}: unknown field")
    elif t == "list":
        if not isinstance(value, list):
            errors.append(f"{where}: must be a list, got {_kind(value)}")
            return
        if len(value) < spec["min"]:
            errors.append(f"{where}: needs at least {spec['min']} item(s)")
        for position, item in enumerate(value):
            _validate(item, spec["items"], f"{where}[{position}]", errors)
    elif t == "str":
        if not isinstance(value, str):
            errors.append(f"{where}: must be a string, got {_kind(value)}")
        elif len(value.strip()) < spec.get("min", 0):
            errors.append(f"{where}: must not be empty")
        elif "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            errors.append(f"{where}: has an invalid format")
    elif t == "bool":
        if not isinstance(value, bool):
            errors.append(f"{where}: must be true or false, got {_kind(value)}")
        elif "const" in spec and value != spec["const"]:
            errors.append(f"{where}: must be {str(spec['const']).lower()} in this version")
    elif t == "enum":
        if value not in spec["values"]:
            errors.append(f"{where}: must be one of {', '.join(map(str, spec['values']))}")
    elif t in ("int", "num"):
        ok = isinstance(value, int) if t == "int" else isinstance(value, (int, float))
        if not ok or isinstance(value, bool):
            errors.append(f"{where}: must be {'an integer' if t == 'int' else 'a number'}, got {_kind(value)}")
        elif "const" in spec and value != spec["const"]:
            errors.append(f"{where}: must be {spec['const']} (schema version)")
        elif ("ge" in spec and value < spec["ge"]) or ("gt" in spec and value <= spec["gt"]) \
                or ("le" in spec and value > spec["le"]):
            bounds = ", ".join(f"{k} {spec[k]}" for k in ("ge", "gt", "le") if k in spec)
            errors.append(f"{where}: out of range ({bounds})")


def _cross_check(name: str, data: dict, errors: list[str]) -> None:
    if name == "org.json":
        seen: set[str] = set()
        for line in data.get("lines", []):
            for item in [line, *line.get("projects", [])]:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    if item["id"] in seen:
                        errors.append(f"{name}: duplicate id '{item['id']}'")
                    seen.add(item["id"])
    elif name == "policy.json":
        if data.get("live_enabled") is True and data.get("live_consent") is None:
            errors.append(f"{name}: live_enabled needs live_consent (explicit, scoped)")
        names = [w.get("name") for w in data.get("subscription_windows", []) if isinstance(w, dict)]
        if len(names) != len(set(names)):
            errors.append(f"{name}: duplicate subscription window name")
        for window in data.get("subscription_windows", []):
            if isinstance(window, dict) and ("length_hours" in window) != ("pacing_slack_percent" in window):
                errors.append(f"{name}: window '{window.get('name')}': length_hours and pacing_slack_percent go together")
    elif name == "state/setup.json":
        for block, state in data.get("blocks", {}).items():
            if isinstance(state, dict) and state.get("status") == "accepted" and state.get("hash") is None:
                errors.append(f"{name}: blocks.{block}: an accepted block needs its hash")


def validate(name: str, data) -> None:
    """Raise ConfigError listing every problem in `data` against the schema called `name` (a key of SCHEMAS)."""
    errors: list[str] = []
    _validate(data, SCHEMAS[name], "", errors)
    if not errors:
        _cross_check(name, data, errors)
    if errors:
        raise ConfigError([f"{name}: {e}" if not e.startswith(name) else e for e in errors])


def block_hash(data) -> str:
    """Stable fingerprint of a saved answer: sha256 of canonical JSON."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def resolve_instance(explicit: str | os.PathLike | None = None, environ=os.environ) -> Path:
    chosen = explicit or environ.get(INSTANCE_ENV)
    if not chosen:
        raise ConfigError([f"no instance directory: pass a path or set {INSTANCE_ENV}"])
    path = Path(chosen).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError([f"instance directory does not exist: {path}"])
    return path


def _load_json(path: Path, name: str):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"{name}: file not found at {path}"]) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{name}: not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"]) from None


@dataclass
class Instance:
    root: Path
    org: dict
    policy: dict
    reporting: dict
    setup: dict | None  # None: setup has not started

    @property
    def backlog_path(self) -> Path:
        return self.root / "projects" / "backlog.md"

    def demo_projects(self) -> set[str]:
        return {p["id"] for line in self.org["lines"] for p in line["projects"] if p.get("demo")}

    def line_projects(self) -> dict[str, set[str]]:
        return {line["id"]: {p["id"] for p in line["projects"]} for line in self.org["lines"]}


def load_instance(root: str | os.PathLike | None = None) -> Instance:
    """Load and validate all files; every error across all files is reported together."""
    base = resolve_instance(root)
    loaded, errors = {}, []
    for name in SCHEMAS:
        path = base / name
        if name == "state/setup.json" and not path.exists():
            loaded[name] = None
            continue
        try:
            data = _load_json(path, name)
            validate(name, data)
            loaded[name] = data
        except ConfigError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigError(errors)
    return Instance(base, loaded["org.json"], loaded["policy.json"], loaded["reporting.json"], loaded["state/setup.json"])


def save_file(root: Path, name: str, data, overwrite: bool = False) -> Path:
    """Validate, then write atomically under the instance lock. Existing files are kept unless `overwrite`."""
    validate(name, data)
    path = root / name
    with file_lock(root / ".orgcore"):
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path} already exists; refusing to replace saved answers")
        atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path
