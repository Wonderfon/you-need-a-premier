"""Conservative personal ASR term fixes; never edits the machine original."""
import re
VERSION = "personal-terms-2026-10-16-v4"
# Distinct recording evidence is recorded in audits/asr-postprocess.json.
# Google Cloud Code is a real product; any vendor-prefixed "cloud" or "cloud code" stays literal.
CLOUD_VENDOR_BEFORE = re.compile(r"(?:(?<![A-Za-z0-9_])(?:google|gcp|aws|azure)|谷歌)\s*$", re.I)
# (rule id, pattern, replacement, required context words or (), preceding text that blocks a match)
RULES = (
    ("q5-model-name", re.compile(r"(?<![A-Za-z0-9_])Q五(?=GPU(?![A-Za-z0-9_])|[^A-Za-z0-9_]|$)", re.I), "Q5", ("GPU", "量化", "模型", "显存"), None),
    ("claude-code-name", re.compile(r"(?<![A-Za-z0-9_])cloud[ ]+code(?![A-Za-z0-9_])", re.I), "Claude Code", (), CLOUD_VENDOR_BEFORE),
    ("anthropic-claude-name", re.compile(r"(?<![A-Za-z0-9_])anthropic[ ]+cloud(?![A-Za-z0-9_])", re.I), "Anthropic Claude", (), None),
    # Owner blanket request (单独cloud也应该替换), not per-recording evidence: any remaining standalone cloud,
    # after the multiword rules. iCloud/clouds/cloudflare/cloudcode never match.
    ("claude-standalone-name", re.compile(r"(?<![A-Za-z0-9_])cloud(?![A-Za-z0-9_])", re.I), "Claude", (), CLOUD_VENDOR_BEFORE),
    # 2026-10-16 offline additions; evidence = distinct recordings (outside the bench40 exam) where the owner's text has the term where the ASR had the left side.
    # Evidence is in audits/2026-10-16-input-asr-postprocess-bench.md. Case-insensitive on purpose: the machine output varies in case.
    ("codex-split", re.compile(r"(?<![A-Za-z0-9_])code[ ]x(?![A-Za-z0-9_])", re.I), "Codex", (), None),                      # 3 recordings
    ("fos-spelled", re.compile(r"(?<![A-Za-z0-9_])f[ ]o[ ]s(?![A-Za-z0-9_])", re.I), "FOS", (), None),                       # 5
    ("fabgrid-misheard", re.compile(r"(?<![A-Za-z0-9_])(?:fiber|five|cyber)[ ]mind(?![A-Za-z0-9_])", re.I), "FabGrid", (), None),  # 4
    ("colab-misspelt", re.compile(r"(?<![A-Za-z0-9_])(?:collab|colan)(?![A-Za-z0-9_])", re.I), "Colab", (), None),           # 4
    ("github-getup", re.compile(r"(?<![A-Za-z0-9_])get[ ]up(?![A-Za-z0-9_])", re.I), "GitHub", ("skill", "上传", "page", "命令"), None),  # 3
    ("uvox-misheard", re.compile(r"(?<![A-Za-z0-9_])(?:woozy|woothie)(?![A-Za-z0-9_])", re.I), "Uvox", (), None),            # 2
    ("arbitros-misheard", re.compile(r"阿瑞斯猫|阿瑞斯莫斯|(?<![A-Za-z0-9_])arismo(?![A-Za-z0-9_])", re.I), "Arbitros", (), None),  # 4

)
# Not applied by default: on bench40 they raise the character error rate against the owner's texts (he kept most of these words), see
# audits/2026-10-16-input-asr-postprocess-bench.md. apply(text, OPTIONAL_RULES) runs only OPTIONAL_RULES, not RULES plus them.
OPTIONAL_RULES = (
    # Fillers, conservative: 呃 is never an answer; 嗯 only in the middle of a sentence (after a character), so "嗯，对" at a sentence start stays;
    # 额 (额度, 额外) and 啊/哦 are never touched. The owner dropped these in only ~40-50% of cases, so expect a small effect.
    ("filler-e", re.compile(r"呃(?!逆)[，,、]?"), "", (), None),
    ("filler-en-mid", re.compile(r"(?<=[㐀-鿿A-Za-z0-9])嗯(?![哼嗯])[，,、]?"), "", (), None),
    ("repeat-3x", re.compile(r"([㐀-鿿]{1,2})\1{2,}"), r"\1", (), None),   # 你你你 -> 你, 这个这个这个 -> 这个; two repeats stay (看看, 可以可以)
)

def apply(text, rules=None):
    hits = []
    for rule_id, pattern, replacement, context, blocked_before in (RULES if rules is None else rules):
        if context and not any(word.casefold() in text.casefold() for word in context):
            continue
        def replace(match):
            if blocked_before and blocked_before.search(text[:match.start()]):
                return match.group(0)
            out = match.expand(replacement)
            hits.append({"rule_id": rule_id, "before": match.group(0), "after": out})
            return out
        text = pattern.sub(replace, text)
    return text, hits
