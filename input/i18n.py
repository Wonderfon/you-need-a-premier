"""Language switch for the fixed text of the dashboard, input station and to-do pages (Premier public edition, block 5).

The pages are written in Chinese. With language "en" (instance.json, see control/instance_config.py) the server swaps every
Chinese fragment of the page source for its English text from `i18n_en.json` before sending it. Only source text is touched:
backlog rows, verbatim words and transcripts arrive as data later and are shown as they are. "zh" returns the text unchanged.
The fragments in the json are exact pieces of the page source (runs of text between quotes, tags and template placeholders),
so a longest-first single pass cannot cut a Chinese word in half. Standard library only."""
import json
import re
from functools import lru_cache
from pathlib import Path

EN_FILE = Path(__file__).resolve().parent / "i18n_en.json"
LANGUAGES = ("zh", "en")


@lru_cache(maxsize=1)
def _english() -> tuple[re.Pattern, dict]:
    table = json.loads(EN_FILE.read_text(encoding="utf-8"))
    keys = sorted(table, key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys)), table


def page(text: str, language: str) -> str:
    if language == "zh":
        return text
    if language != "en":
        raise ValueError(f"unknown language {language!r}; expected one of {LANGUAGES}")
    pattern, table = _english()
    def swap(m: re.Match) -> str:
        en = table[m.group()]
        before, after = text[m.start() - 1:m.start()], text[m.end():m.end() + 2]
        if before == "}" and en[:1].isalnum():  # a piece right after a template placeholder: Chinese needs no space, English does
            en = " " + en
        if after == "${" and (en[-1:].isalnum() or en[-1:] in ",;:"):
            en += " "
        return en
    return pattern.sub(swap, text).replace('lang="zh-CN"', 'lang="en"')


# Fixed labels and messages that the server itself puts into JSON answers (accumulation rows, plan headings, card kinds, button
# replies). Whole-string match only, so a backlog row or a transcript is never touched unless it equals one of these exactly.
DATA_EN = {
    "组织线": "Organization line", "工作线": "Work line", "Arbitros 线": "Arbitros line", "Beacon Listener 线": "Beacon Listener line",
    "量化线": "Quant line", "情报线": "Intelligence line", "生活线": "Life line",
    "终端": "Terminal", "其他": "Other", "条": "", "字": " chars", "个": "", "版": "", "次": "", "小时": "h", "项／批": "",
    "终端原话": "Terminal quotes", "Discord原话": "Discord quotes", "其他原话": "Other quotes",
    "终端原话字数": "Terminal quote characters", "Discord原话字数": "Discord quote characters", "其他原话字数": "Other quote characters",
    "Discord 消息": "Discord messages", "Discord 已存附件": "Discord saved attachments",
    "已确认录音": "Confirmed recordings", "已确认语音": "Confirmed speech", "未确认录音": "Unconfirmed recordings",
    "未确认语音": "Unconfirmed speech", "手改转写版本": "Hand-edited transcript versions", "要你的 · 回复": "To-do page · replies",
    "评价 · 用": "Ratings · useful", "评价 · 没用": "Ratings · not useful", "评价 · 返工": "Ratings · rework", "已登记材料": "Registered materials",
    "验收": "Review", "规划": "Planning", "跨线复盘": "Cross-line review", "写日报": "Daily report", "终端会话": "Terminal session",
    "处理你的指令": "Handling your instruction", "（工作区根目录）": "(workspace root)", "[已隐藏]": "[hidden]", "[路径]": "[path]",
    "定": "Decide", "做": "Do", "看/试": "Look / try", "知道就行": "FYI",
    "部分原话存档无法读取": "Some verbatim archives could not be read", "部分旧会话存档无法读取": "Some old session archives could not be read",
    "旧会话存档无法读取": "Old session archive could not be read",
    "操作无效，请刷新页面后重试。": "Invalid action; refresh the page and try again.",
    "这一张已经不在「要你的」里了，请刷新页面。": "This card is no longer on the page; refresh.",
    "这一张现在不能回复，请刷新页面。": "This card cannot be answered now; refresh.",
    "请选一个选项或写几个字。": "Pick an option or write a few words.",
    "这一张不能评价，请刷新页面。": "This card cannot be rated; refresh.",
    "评价只能是「用」「没用」「返工」。": "A rating must be useful, not useful or rework.",
    "这一张现在不能这样处理，请刷新页面。": "This card cannot be handled this way now; refresh.",
    "已撤回，这一张回到上面。": "Withdrawn; this card is back on top.",
    "已收起。": "Folded away.", "已收起（没能在任务行上加注，等审查时再说）。": "Folded away (the task row could not be annotated; review will check).",
    "已记下「再想想」，这一张留在上面。": "Noted \"think again\"; this card stays on top.",
    "这一张在别处答过了，页面撤不回来；要改请在终端或 Discord 里告诉总理。": "Answered elsewhere; the page cannot withdraw it. Tell the coordinator in the terminal or Discord.",
    "这一条已经不在「等你决定」里了，请刷新页面。": "This item is no longer waiting for you; refresh.",
    "这一件已经不在「请你评价」里了，请刷新页面。": "This item is no longer waiting for a rating; refresh.",
    "没有可撤回的卡，请刷新页面。": "No cards to withdraw; refresh.", "这些卡现在不在这一栏里，请刷新页面。": "These cards are not in this group now; refresh.",
    "从下面选一个，可以补一句话": "Pick one below; you may add a sentence",
    "这一行没写成「A 做什么」「B 做什么」的选项，看全文后回一句话": "This row has no 'A do this / B do that' options; read the full text and reply in a sentence",
    "照行里写的做": "Do what the row says", "打开看一下，再点用／没用／返工": "Open it and look, then click useful / not useful / rework",
    "知道就行，不用做什么": "Just so you know; nothing to do",
    "请求内容格式不正确。": "The request body is malformed.", "请求格式不正确。": "The request is malformed.",
    "请使用 JSON 请求格式。": "Use JSON.", "音频片段格式不正确。": "Bad audio chunk format.", "音频片段为空或超过 8 MiB。": "Audio chunk is empty or over 8 MiB.",
    "修订类型无效。": "Invalid revision type.", "这条记录正在润色。": "This note is being polished.",
    "本地保存遇到问题；已收到的原始片段仍会保留。": "Saving locally hit a problem; the raw chunks received are kept.",
    "可训练语音小时数暂时读不出来（不影响录音）": "Trainable speech hours cannot be read right now (recording is not affected)",
    "本地 ASR 状态暂不可读取；原始音频仍可保存。": "Local ASR status cannot be read; raw audio can still be saved.",
}
DATA_RE_EN = [
    (re.compile(r"已记下 (\d+) 件，行尾各加了一句，各线下次审查核对。"), r"Noted \1 cards; each row got a line at its end, the lines check it at the next review."),
    (re.compile(r"已撤回 (\d+) 件，回到原处。"), r"Withdrew \1 cards; they are back."),
    (re.compile(r"已收到，(.+)会照办。"), r"Got it; \1 will follow it."),
    (re.compile(r"已记下评价「(.+)」，总理每周对照审查结论。"), r"Rating noted; the coordinator compares it with review verdicts weekly."),
    (re.compile(r"已记下，行尾加了一句，(.+)下次审查核对。"), r"Noted; a line was added at the row's end; \1 checks it at the next review."),
]


# Only the values of these fields carry a server-made label or message; `text`, `detail`, `note` and the like carry backlog rows
# and transcripts and are never looked at.
DATA_FIELDS = {"label", "unit", "section", "kind_label", "kind", "do", "message", "error", "warnings", "folder"}


def data(obj, language: str, field: str | None = None):
    """Translate the fixed server labels in the label fields of a JSON answer (see DATA_EN, DATA_FIELDS); zh returns it unchanged."""
    if language == "zh":
        return obj
    if isinstance(obj, str):
        if field not in DATA_FIELDS:
            return obj
        if obj in DATA_EN:
            return DATA_EN[obj]
        for rx, rep in DATA_RE_EN:
            if rx.fullmatch(obj):
                return rx.sub(rep, obj)
        return obj
    if isinstance(obj, list):
        return [data(x, language, field) for x in obj]
    if isinstance(obj, dict):
        return {k: data(x, language, k) for k, x in obj.items()}
    return obj
