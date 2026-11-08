"use strict";
// 规则面板：推荐、已生效、手动添加、操作日志。文字一律用 textContent 写入。
// 依赖 app.js 里的 api / make / byId / setStatus。

const RULE_POLL_MS = 10000;
const LOG_LABELS = {
  accept: (e) => `接受推荐：${e.rule.from} → ${e.rule.to}`,
  add: (e) => `手动添加：${e.rule.from} → ${e.rule.to}`,
  delete: (e) => `删除规则：${e.rule.from} → ${e.rule.to}`,
  disable: (e) => `停用内置规则：${e.builtin_id}`,
  enable: (e) => `启用内置规则：${e.builtin_id}`,
  ignore: (e) => `忽略推荐：${e.pair.from} → ${e.pair.to}`,
  undo: (e) => `撤回了上一步（${e.of_action}）`,
};

function ruleButton(label, onClick, cls = "quiet-action") {
  const button = make("button", `${cls} rule-button`, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

async function ruleAction(path, options, doneMessage) {
  try {
    const result = await api(path, options);
    renderRules(result);
    setStatus("rules-status", doneMessage(result));
  } catch (err) { setStatus("rules-status", err.message, true); }
}

function pair(heard, corrected) {
  const line = make("p", "rule-pair");
  line.append(make("span", "rule-heard", heard), make("span", "rule-arrow", " → "), make("span", "rule-fixed", corrected));
  return line;
}

function suggestionCard(s, near = false) {
  const card = make("article", "rule-card");
  card.append(pair(s.from, s.to));
  card.append(make("p", "rule-meta", near
    ? `你在 ${s.count} 段记录里这样改过 · 会触发 ${s.hits} 处 · 其中 ${s.false_hits} 处你没这样改（对照 ${s.checked} 段已确认文本）`
    : `出现 ${s.count} 次（${s.count} 段记录里你都这样改）· 预计误伤 ${s.false_hits} 处（对照了 ${s.checked} 段已确认文本，这条规则会触发 ${s.hits} 处）`));
  (near ? s.evidence.slice(0, 1) : s.evidence).forEach((ev) => {
    if (near) {
      card.append(make("p", "rule-meta", `改对的原句：${ev.original}`), make("p", "rule-meta", `你改成：${ev.edited}`));
      return;
    }
    const details = make("details", "rule-evidence");
    details.append(make("summary", "", `…${ev.before.replace(/\s+/g, " ")}… → …${ev.after.replace(/\s+/g, " ")}…`));
    details.append(make("p", "", `机器原句：${ev.original}`), make("p", "", `你改成：${ev.edited}`));
    card.append(details);
  });
  if (near) {
    s.false_evidence.forEach((ev) => card.append(make("p", "rule-meta", `会改错的原句（按已确认稿判断）：${ev.original}`)));
  }
  const actions = make("div", "rule-actions");
  actions.append(
    ruleButton(near ? "加成规则" : "接受", () => ruleAction("/api/rules/accept", { method: "POST", body: { from: s.from, to: s.to, near } }, () => "已接受，之后的新转写立即生效；旧记录不变。"), "secondary-button"),
    ruleButton("忽略", () => ruleAction("/api/rules/ignore", { method: "POST", body: { from: s.from, to: s.to } }, () => "已忽略，不会再推荐这一条。"))
  );
  card.append(actions);
  return card;
}

function activeRow(kind, line, meta, button) {
  const row = make("div", "rule-row");
  const text = make("div", "rule-row-text");
  text.append(line, make("p", "rule-meta", meta));
  row.append(make("span", `rule-tag ${kind === "内置" ? "builtin" : ""}`, kind), text, button);
  return row;
}

function renderRules(view) {
  const suggestions = byId("rules-suggestions");
  suggestions.replaceChildren(...(view.suggestions.length
    ? view.suggestions.map((s) => suggestionCard(s))
    : [make("p", "empty", "暂时没有推荐。同一种改法在两段以上的记录里出现、并且不会误伤已确认文本时，才会出现在这里。")]));
  byId("rules-badge").textContent = view.suggestions.length ? `${view.suggestions.length} 条推荐` : "";
  byId("rules-near-count").textContent = `（${view.near.length} 条）`;
  byId("rules-near").replaceChildren(...(view.near.length
    ? view.near.map((s) => suggestionCard(s, true))
    : [make("p", "empty", "暂时没有差一点的推荐。至少两段记录里这样改过、且没这样改的地方不超过一半时，才会出现在这里。")]));

  const active = byId("rules-active");
  const rows = view.owner.map((rule) => activeRow(
    "我的", pair(rule.from, rule.to),
    `${rule.source === "suggested" ? "来自推荐" : "手动添加"} · ${rule.added_at.replace("T", " ").replace("Z", " UTC")} · 证据 ${rule.evidence_sessions.length} 段记录`,
    ruleButton("删除", () => ruleAction(`/api/rules/owner/${encodeURIComponent(rule.id)}`, { method: "DELETE" }, () => "已删除，之后的新转写不再套用；可在日志里撤回。"))));
  view.builtin.forEach((rule) => {
    const line = pair(rule.pattern, rule.to);
    line.classList.add("rule-pattern");
    const context = rule.context.length ? ` · 需要上下文出现：${rule.context.join("、")}` : "";
    rows.push(activeRow("内置", line, `${rule.enabled ? "生效中" : "已停用"}${context}`,
      ruleButton(rule.enabled ? "停用" : "启用", () => ruleAction(`/api/rules/builtin/${encodeURIComponent(rule.id)}/enabled`, { method: "POST", body: { enabled: !rule.enabled } },
        () => rule.enabled ? "已停用，之后的新转写不再套用。" : "已启用。"))));
    if (!rule.enabled) rows[rows.length - 1].classList.add("off");
  });
  active.replaceChildren(...rows);
  byId("rules-version").textContent = `当前规则集版本：${view.version}`;

  const log = byId("rules-log");
  log.replaceChildren(...(view.log.length ? view.log.map((entry) => {
    const row = make("div", "rule-log-row");
    row.append(make("span", "rule-meta", entry.at.replace("T", " ").replace("Z", "")), make("span", "", (LOG_LABELS[entry.action] || (() => entry.action))(entry)));
    if (entry.action !== "undo") {
      if (entry.undone) row.append(make("span", "rule-meta", "已撤回"));
      else row.append(ruleButton("撤回", () => ruleAction(`/api/rules/log/${entry.id}/undo`, { method: "POST", body: {} }, () => "已撤回。")));
    }
    return row;
  }) : [make("p", "empty", "还没有操作。")]));
}

async function refreshRules() {
  try { renderRules(await api("/api/rules")); }
  catch (err) { setStatus("rules-status", `读取规则失败：${err.message}`, true); }
}

byId("rules-panel").addEventListener("toggle", () => { if (byId("rules-panel").open) void refreshRules(); });
byId("rule-add").addEventListener("click", async () => {
  const heard = byId("rule-from").value, corrected = byId("rule-to").value;
  try {
    const result = await api("/api/rules/add", { method: "POST", body: { from: heard, to: corrected } });
    renderRules(result);
    byId("rule-from").value = byId("rule-to").value = "";
    setStatus("rules-status", `已添加。对照 ${result.added.checked} 段已确认文本，这条规则会触发 ${result.added.hits} 处，其中预计误伤 ${result.added.false_hits} 处。`, result.added.false_hits > 0);
  } catch (err) { setStatus("rules-status", err.message, true); }
});
byId("verify-revision").addEventListener("click", () => setTimeout(refreshRules, 2000));
window.setInterval(() => { if (!document.hidden) void refreshRules(); }, RULE_POLL_MS);
void refreshRules();
