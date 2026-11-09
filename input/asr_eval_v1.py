"""语音评测集 v1：从输入台库里挑人工逐字确认过的麦克风录音，导出评测集，并算生产 Q5 首次转写的字错率基线。

只读 input.sqlite3 和 sessions/；输出写到 OUT_DIR（不进 git）。不调用 ASR worker，不联网。
直接运行：python asr_eval_v1.py
重复运行：评测集成员和日期划分记在 OUT_DIR/members.json，之后新增的合格录音进“微调候选池”，不进评测集。
想要新一版评测集：改 VERSION 和 OUT_DIR，或删掉 members.json。
"""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import unicodedata
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- CONFIG
VERSION = "v1"
WORKSPACE = Path(r"C:\orghome\orgcore_workspace\incore")
DB_PATH = WORKSPACE / "input" / "data" / "input.sqlite3"
SESSIONS_DIR = WORKSPACE / "input" / "data" / "sessions"
OUT_DIR = WORKSPACE / "data" / "asr-eval" / VERSION
POSTPROCESS_PY = WORKSPACE / "input" / "postprocess.py"

# owner 2026-09-30 23:41 决定不重标、直接排除的 14 条（session id 前 8 位，来源 audits/2026-09-30-voice-confirm-labels.md）
EXCLUDE_PREFIXES = ("66187581", "fecc5af9", "a81abbd4", "1d94bf4d", "37caf01b", "0f023f1f", "faaa9358",
                    "e80580c2", "3d83cb76", "9de18209", "3e7f324f", "d104db38", "1d47f8d1", "eab651e2")
TRUSTED_FROM_UTC = "2026-10-01T03:28:00Z"   # 新版（确认时选用途）上线 2026-09-30 23:28 美东
LOCAL_UTC_OFFSET_HOURS = -4                 # 日期按美东夏令时分组（本机没有 tzdata）
ASR_MODEL_MARK = "Q5_K_M"                   # 生产 Q5 的 model 字段
GROUP_BOUNDS_SECONDS = (60, 300)            # <60 s、60–300 s、>300 s
TEST_FRACTION = 0.5                         # 新日期放进当前分钟数较少的一边；整体约这个比例进 test
FINETUNE_GATE_HOURS = 5                     # 评测集之外另有这么多小时核对过的录音，才建议做微调
MAKE_WAV = True
WSL_DISTRO = "Ubuntu"
WSL_PYTHON = "/mnt\x2fc/orghome/orgcore_workspace/incore/.runtime/asr-venv/bin/python"  # 只借用其中的 av，只读
# ----------------------------------------------------------------------

LOCAL_TZ = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_postprocess():
    spec = importlib.util.spec_from_file_location("input_postprocess", POSTPROCESS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local_date(utc_text):
    return datetime.fromisoformat(utc_text.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- selection
def literal_label(db, revision_id):
    """与 store._literal_chain（2026-09-30 版）相同：确认的那一版自己是听写纠错，并溯源到原始转写。"""
    seen, current = set(), revision_id
    while current and current not in seen:
        seen.add(current)
        row = db.execute("SELECT kind,parent_id,data_json,source FROM revisions WHERE id=?", (current,)).fetchone()
        if row is None:
            return False
        if row["kind"] == "asr":
            return row["parent_id"] is None
        if row["kind"] == "postprocess" and row["source"] == "asr_postprocess" and row["parent_id"]:
            current = row["parent_id"]
            continue
        if row["kind"] != "correction" or not row["parent_id"]:
            return False
        try:
            data = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return False
        if current == revision_id and (not isinstance(data, dict) or data.get("correction_type") != "recognition_correction"):
            return False
        current = row["parent_id"]
    return False


def confirmation_survives(db, session_id, revision_id):
    """确认之后若还存过别的，只有“修改稿/表达改写、且最后一版是文字不同的表达改写”才保留（owner 00:13 的用法）；
    之后出现听写纠错或新转写、末尾是没确认的修改稿、或末尾改写文字与确认版相同，都算失效。"""
    rows = db.execute("SELECT id,kind,content,data_json FROM revisions WHERE session_id=? AND kind!='instruction' "
                      "ORDER BY rowid", (session_id,)).fetchall()
    ids = [r["id"] for r in rows]
    later = rows[ids.index(revision_id) + 1:]
    if not later:
        return True

    def label(r):
        try:
            return json.loads(r["data_json"]).get("correction_type") if r["kind"] == "correction" else None
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None

    return (all(label(r) in ("draft", "user_rewrite") for r in later) and label(later[-1]) == "user_rewrite"
            and later[-1]["content"] != rows[ids.index(revision_id)]["content"])


def chain_root(db, revision_id):
    current, seen = revision_id, set()
    while current and current not in seen:
        seen.add(current)
        row = db.execute("SELECT id,parent_id FROM revisions WHERE id=?", (current,)).fetchone()
        if row is None or row["parent_id"] is None:
            return current
        current = row["parent_id"]
    return None


def select_sessions(db):
    """返回 (合格录音列表, 排除原因计数)。参考文本=该录音最后一次“听写纠错”确认时的那个修订。"""
    skipped = {}
    chosen = []

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    sessions = db.execute("SELECT * FROM sessions WHERE source='audio_capture' ORDER BY created_at").fetchall()
    for s in sessions:
        if s["id"].startswith(EXCLUDE_PREFIXES):
            skip("owner_excluded_14")
            continue
        vs = db.execute("SELECT v.*,r.content,r.sha256 AS rsha,r.kind AS rkind FROM verifications v "
                        "JOIN revisions r ON r.id=v.revision_id WHERE v.session_id=? AND v.scope='full_recording' "
                        "ORDER BY v.verified_at DESC, v.rowid DESC", (s["id"],)).fetchall()
        if not vs:
            skip("not_verified_full_recording")
            continue
        ok = next((v for v in vs if literal_label(db, v["revision_id"])), None)
        if ok is None:
            skip("last_confirmation_not_dictation")
            continue
        if not confirmation_survives(db, s["id"], ok["revision_id"]):
            skip("confirmation_superseded")
            continue
        if (ok["original_sha256"] != s["original_sha256"] or ok["revision_sha256"] != ok["rsha"]
                or sha256_bytes(ok["content"].encode("utf-8")) != ok["rsha"]):
            skip("hash_mismatch")
            continue
        asr = None
        for r in db.execute("SELECT * FROM revisions WHERE session_id=? AND kind='asr' ORDER BY rowid", (s["id"],)):
            try:
                model = json.loads(r["data_json"]).get("model", "")
            except (json.JSONDecodeError, TypeError, AttributeError):
                model = ""
            if ASR_MODEL_MARK in str(model):
                asr = r
                break
        if asr is None:
            skip("no_q5_transcript")
            continue
        if not ok["content"].strip():
            skip("empty_reference")
            continue
        audio = SESSIONS_DIR / s["id"] / f"original.{s['audio_extension']}"
        if not audio.is_file() or sha256_bytes(audio.read_bytes()) != s["original_sha256"]:
            skip("audio_missing_or_hash_mismatch")
            continue
        chosen.append({
            "session_id": s["id"], "created_at": s["created_at"], "date": local_date(s["created_at"]),
            "audio_path": audio, "original_sha256": s["original_sha256"],
            "revision_id": ok["revision_id"], "revision_sha256": ok["rsha"], "revision_kind": ok["rkind"],
            "verified_at": ok["verified_at"], "reference": ok["content"],
            "is_latest_revision_text": db.execute("SELECT id FROM revisions WHERE session_id=? AND kind!='instruction' "
                                                  "ORDER BY rowid DESC LIMIT 1", (s["id"],)).fetchone()["id"] == ok["revision_id"],
            "asr_revision_id": asr["id"], "asr_revision_sha256": asr["sha256"], "asr_text": json.loads(asr["data_json"])["text"],
            "asr_model": json.loads(asr["data_json"]).get("model"),
            "reference_root_is_first_q5": chain_root(db, ok["revision_id"]) == asr["id"],
            "trusted_post_deploy": ok["verified_at"] >= TRUSTED_FROM_UTC,
        })
    return chosen, skipped


# ---------------------------------------------------------------- audio
WSL_SCRIPT = r'''
import av, json, wave
jobs = JOBS
for src, dst in jobs:
    out = bytearray()
    with av.open(src) as c:
        rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for f in c.decode(audio=0):
            for g in rs.resample(f):
                out += g.to_ndarray().tobytes()
        for g in rs.resample(None):
            out += g.to_ndarray().tobytes()
    with wave.open(dst, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(out))
print("ok", len(jobs))
'''


def wsl_path(p):
    p = Path(p).resolve()
    return "/mnt/" + p.drive[0].lower() + "/" + "/".join(p.parts[1:])


def make_wavs(items):
    jobs = [(wsl_path(i["audio_path"]), wsl_path(i["wav_path"])) for i in items if not i["wav_path"].is_file()]
    if not jobs:
        return
    script = WSL_SCRIPT.replace("JOBS", json.dumps(jobs))
    r = subprocess.run(["wsl", "-d", WSL_DISTRO, "--", "env", "PYTHONDONTWRITEBYTECODE=1", WSL_PYTHON, "-"],
                       input=script.encode("utf-8"), capture_output=True, timeout=1800)
    if r.returncode != 0:
        sys.exit("wav 转换失败：" + r.stderr.decode("utf-8", "replace")[-500:])


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------- CER
def normalize(text, punct):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text
                   if not ch.isspace() and (punct or unicodedata.category(ch)[0] not in "PS"))


def edit_distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def group_of(seconds):
    lo, hi = GROUP_BOUNDS_SECONDS
    return "<60s" if seconds < lo else ("1-5min" if seconds <= hi else ">5min")


def summarize(rows, key):
    edits = sum(r[key]["edits"] for r in rows)
    chars = sum(r[key]["ref_chars"] for r in rows)
    return {"recordings": len(rows), "ref_chars": chars,
            "cer": round(edits / chars, 4) if chars else None,
            "mean_per_recording_cer": round(sum(r[key]["cer"] for r in rows) / len(rows), 4) if rows else None}


# ---------------------------------------------------------------- main
def assign_splits(items, saved):
    """日期整天归一边；已记录的日期不变，新日期放进当前分钟数较少的一边（目标比例 TEST_FRACTION）。"""
    dates = dict(saved.get("dates", {}))
    minutes = {"dev": 0.0, "test": 0.0}
    for i in items:
        if dates.get(i["date"]):
            minutes[dates[i["date"]]] += i["duration_seconds"] / 60
    new = {}
    for i in items:
        if i["date"] not in dates:
            new[i["date"]] = new.get(i["date"], 0) + i["duration_seconds"] / 60
    for d, m in sorted(new.items(), key=lambda x: -x[1]):
        total = minutes["dev"] + minutes["test"]
        side = "test" if total and minutes["test"] < TEST_FRACTION * total else "dev"
        dates[d] = side
        minutes[side] += m
    return dates


def main():
    pp = load_postprocess()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "wav").mkdir(exist_ok=True)
    (OUT_DIR / "ref").mkdir(exist_ok=True)
    db = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    eligible, skipped = select_sessions(db)
    if not eligible:
        sys.exit(f"没有合格录音，不写任何文件：{skipped}")

    members_file = OUT_DIR / "members.json"
    saved = json.loads(members_file.read_text("utf-8")) if members_file.is_file() else {}
    frozen = set(saved.get("members", []))
    for i in eligible:
        i["wav_path"] = OUT_DIR / "wav" / f"{i['session_id']}.wav"
    if MAKE_WAV:
        make_wavs([i for i in eligible if not frozen or i["session_id"] in frozen])
    for i in eligible:
        i["duration_seconds"] = round(wav_seconds(i["wav_path"]), 2) if i["wav_path"].is_file() else None
    if not frozen:
        frozen = {i["session_id"] for i in eligible}
    members = [i for i in eligible if i["session_id"] in frozen]
    pool = [i for i in eligible if i["session_id"] not in frozen]
    dropped = sorted(frozen - {i["session_id"] for i in eligible})
    for i in pool:   # 池里的录音只需要时长，从库里的转写元数据取
        meta = json.loads(db.execute("SELECT data_json FROM revisions WHERE id=?", (i["asr_revision_id"],)).fetchone()[0])
        i["duration_seconds"] = float(meta.get("metadata", {}).get("duration_seconds", 0))
    dates = assign_splits(members, saved)
    members_file.write_text(json.dumps({"version": VERSION, "frozen_at": saved.get("frozen_at", datetime.now(timezone.utc).isoformat()),
                                        "members": sorted(frozen), "dates": dates}, indent=1), "utf-8")

    manifest, rows = [], []
    for i in sorted(members, key=lambda x: x["created_at"]):
        split = dates[i["date"]]
        ref_path = OUT_DIR / "ref" / f"{i['session_id']}.txt"
        ref_path.write_bytes(i["reference"].encode("utf-8"))
        if sha256_bytes(ref_path.read_bytes()) != i["revision_sha256"]:
            sys.exit(f"参考文本文件哈希与修订不一致：{ref_path}")
        manifest.append({
            "session_id": i["session_id"], "split": split, "date": i["date"], "duration_seconds": i["duration_seconds"],
            "original_sha256": i["original_sha256"], "revision_id": i["revision_id"], "revision_sha256": i["revision_sha256"],
            "revision_kind": i["revision_kind"], "verified_at": i["verified_at"], "trusted_post_deploy": i["trusted_post_deploy"],
            "reference_is_latest_text": i["is_latest_revision_text"], "reference_root_is_first_q5": i["reference_root_is_first_q5"],
            "first_q5_revision_id": i["asr_revision_id"], "first_q5_revision_sha256": i["asr_revision_sha256"],
            "wav": f"wav/{i['session_id']}.wav", "reference": f"ref/{i['session_id']}.txt",
        })
        post_text, hits = pp.apply(i["asr_text"])
        row = {"session_id": i["session_id"], "split": split, "group": group_of(i["duration_seconds"]),
               "duration_seconds": i["duration_seconds"], "trusted_post_deploy": i["trusted_post_deploy"], "postprocess_hits": len(hits)}
        for punct in (True, False):
            ref = normalize(i["reference"], punct)
            for name, hyp_text in (("raw", i["asr_text"]), ("post", post_text)):
                if name == "post" and not hits:
                    row[f"{name}_{'punct' if punct else 'nopunct'}"] = row[f"raw_{'punct' if punct else 'nopunct'}"]
                    continue
                e = edit_distance(ref, normalize(hyp_text, punct))
                row[f"{name}_{'punct' if punct else 'nopunct'}"] = {"edits": e, "ref_chars": len(ref), "cer": round(e / len(ref), 4) if ref else 0}
        rows.append(row)
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    (OUT_DIR / "baseline-per-recording.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")

    def minutes(xs):
        return round(sum(x["duration_seconds"] for x in xs) / 60, 1)

    subsets = {"all": rows, "dev": [r for r in rows if r["split"] == "dev"], "test": [r for r in rows if r["split"] == "test"],
               "trusted_post_deploy": [r for r in rows if r["trusted_post_deploy"]]}
    baseline = {}
    for sname, srows in subsets.items():
        baseline[sname] = {}
        for g in ("all", "<60s", "1-5min", ">5min"):
            grows = srows if g == "all" else [r for r in srows if r["group"] == g]
            baseline[sname][g] = {"minutes": minutes(grows), **{k: summarize(grows, k) for k in
                                  ("raw_punct", "raw_nopunct", "post_punct", "post_nopunct")}} if grows else {"recordings": 0}
    pool_hours = round(sum(x["duration_seconds"] for x in pool) / 3600, 2)
    latest = {
        "version": VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asr": f"生产 Q5（{ASR_MODEL_MARK}）首次转写，直接取库里已存的结果；个人词后处理版本 {pp.VERSION}",
        "eval_set": {"recordings": len(members), "minutes": minutes(members),
                     "dev": {"recordings": len(subsets["dev"]), "minutes": minutes([m for m in members if dates[m["date"]] == "dev"])},
                     "test": {"recordings": len(subsets["test"]), "minutes": minutes([m for m in members if dates[m["date"]] == "test"])},
                     "dates": dates, "dropped_since_freeze": dropped},
        "trusted_post_deploy": {"recordings_in_eval": len(subsets["trusted_post_deploy"]),
                                "minutes_in_eval": minutes([m for m in members if m["trusted_post_deploy"]]),
                                "recordings_all_eligible": sum(1 for e in eligible if e["trusted_post_deploy"]),
                                "minutes_all_eligible": minutes([e for e in eligible if e["trusted_post_deploy"]]),
                                "note": f"{TRUSTED_FROM_UTC} 之后确认，用途是确认时选的"},
        "finetune_pool": {"recordings": len(pool), "hours": pool_hours, "gate_hours": FINETUNE_GATE_HOURS,
                          "progress_pct": round(100 * pool_hours / FINETUNE_GATE_HOURS, 1), "gate_reached": pool_hours >= FINETUNE_GATE_HOURS,
                          "note": "评测集冻结后新增的、合格的核对录音；达到 gate_hours 才建议考虑微调"},
        "skipped": skipped,
        "notes": {"reference_not_latest_text": sum(1 for m in manifest if not m["reference_is_latest_text"]),
                  "reference_root_differs_from_first_q5": sum(1 for m in manifest if not m["reference_root_is_first_q5"])},
        "baseline_cer": baseline,
        "cer_definition": "字符级编辑距离/参考字数；NFKC、忽略大小写和空白；不带标点版额外去掉标点和符号。分组按录音时长。",
    }
    (OUT_DIR / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps({k: latest[k] for k in ("eval_set", "trusted_post_deploy", "finetune_pool", "skipped", "notes")}, ensure_ascii=False, indent=1))
    for g in ("all", "<60s", "1-5min", ">5min"):
        b = baseline["all"][g]
        if b.get("recordings", 1):
            print(g, b["minutes"], "min", {k: (b[k]["recordings"], b[k]["cer"]) for k in ("raw_punct", "raw_nopunct", "post_punct", "post_nopunct")})


if __name__ == "__main__":
    main()
