"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const DB_NAME = "demo01-local-input";
const CHUNK_BYTES = 4 * 1024 * 1024;
const MAX_BYTES = 2 * 1024 * 1024 * 1024;
const AUTOSAVE_DELAY = 650;
const busyUploads = new Set();
const byId = (id) => document.getElementById(id);

let dbPromise;
let recorder = null;
let mediaStream = null;
let recordingSession = null;
let recordingWrites = Promise.resolve();
let recordingDone = Promise.resolve();
let startedAt = 0;
let visualContext = null;
let visualAnalyser = null;
let visualSamples = null;
let visualFrame = 0;
let visualClockTime = 0;
let visualClockAt = 0;
let clockTimer = 0;
let quietSince = 0;
let stopping = false;
let serverSessions = [];
let selectedSessionId = null;
let selectedDetail = null;
let archiveDetailSignature = "";
let baseRevisionId = null;
let savedText = "";
let autosaveTimer = 0;
let autosavePromise = null;
let editorDirty = false;
let screenMode = "record";

function setStatus(id, message, error = false) {
  const node = byId(id);
  node.textContent = message || "";
  node.classList.toggle("error", Boolean(error));
}

function showScreen(mode) {
  screenMode = mode;
  byId("record-screen").hidden = mode !== "record";
  byId("text-screen").hidden = mode !== "text";
  byId("result-screen").hidden = mode !== "result";
  if (mode === "text") setTimeout(() => byId("text-input").focus(), 0);
}

function openDb() {
  if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("sessions")) db.createObjectStore("sessions", { keyPath: "id" });
      if (!db.objectStoreNames.contains("chunks")) db.createObjectStore("chunks", { keyPath: ["sessionId", "seq"] });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

async function dbRequest(store, mode, operation) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    const req = operation(tx.objectStore(store));
    let result;
    req.onsuccess = () => { result = req.result; };
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error || new Error("本机浏览器保存失败"));
    tx.onabort = () => reject(tx.error || new Error("本机浏览器保存失败"));
  });
}

const putLocalSession = (value) => dbRequest("sessions", "readwrite", (s) => s.put(value));
const getLocalSession = (id) => dbRequest("sessions", "readonly", (s) => s.get(id));
const allLocalSessions = () => dbRequest("sessions", "readonly", (s) => s.getAll());
const putLocalChunk = (value) => dbRequest("chunks", "readwrite", (s) => s.put(value));
const getAllLocalChunks = () => dbRequest("chunks", "readonly", (s) => s.getAll());
const deleteLocalChunk = (key) => dbRequest("chunks", "readwrite", (s) => s.delete(key));

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrf);
  if (options.body && !(options.body instanceof Blob) && typeof options.body !== "string") {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? {} : response.json();
}

async function digest(blob) {
  const bytes = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function generatedTitle(prefix = "录音") {
  const stamp = new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date());
  return `${prefix} · ${stamp}`;
}

function localStatus(session, chunks) {
  const own = chunks.filter((chunk) => chunk.sessionId === session.id);
  return { own, waiting: own.filter((chunk) => !chunk.uploaded).length, bytes: own.reduce((n, chunk) => n + chunk.size, 0) };
}

async function saveChunk(session, seq, blob) {
  if (!blob.size) return;
  if (session.totalBytes + blob.size > MAX_BYTES) throw new Error("单条音频不能超过 2 GiB。");
  const chunk = { sessionId: session.id, seq, blob, size: blob.size, uploaded: false };
  await putLocalChunk(chunk);
  session.totalBytes += blob.size;
  session.nextSeq = Math.max(session.nextSeq || 0, seq + 1);
  await putLocalSession(session);
}

async function pumpUploads(sessionId) {
  if (busyUploads.has(sessionId)) return false;
  busyUploads.add(sessionId);
  try {
    const chunks = (await getAllLocalChunks()).filter((chunk) => chunk.sessionId === sessionId).sort((a, b) => a.seq - b.seq);
    for (const chunk of chunks) {
      if (chunk.uploaded) continue;
      const hash = await digest(chunk.blob);
      const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/chunks/${chunk.seq}`, {
        method: "PUT", credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": csrf, "X-Chunk-SHA256": hash },
        body: chunk.blob
      });
      if (!response.ok) {
        let message = `片段 ${chunk.seq + 1} 尚未收到确认`;
        try { message = (await response.json()).error || message; } catch (_) {}
        throw new Error(message);
      }
      chunk.uploaded = true;
      await putLocalChunk(chunk);
      const session = await getLocalSession(sessionId);
      if (session) {
        session.lastDurableAt = new Date().toISOString();
        await putLocalSession(session);
      }
      if (recordingSession?.id === sessionId) {
        const complete = (await getAllLocalChunks()).filter((item) => item.sessionId === sessionId && item.uploaded).length;
        setStatus("record-status", `录音正在本机保存；服务端已确认 ${complete} 段。`);
      }
    }
    return true;
  } finally {
    busyUploads.delete(sessionId);
  }
}

async function finalizeLocalSession(sessionId) {
  const session = await getLocalSession(sessionId);
  if (!session) throw new Error("浏览器中找不到这条录音的重试队列。");
  const chunks = (await getAllLocalChunks()).filter((chunk) => chunk.sessionId === sessionId).sort((a, b) => a.seq - b.seq);
  if (!chunks.length) throw new Error("没有可完成的音频片段。");
  for (let i = 0; i < chunks.length; i++) if (chunks[i].seq !== i) throw new Error("本机片段有缺口，请先恢复或重试。");
  if (chunks.some((chunk) => !chunk.uploaded)) await pumpUploads(sessionId);
  const stored = (await getAllLocalChunks()).filter((chunk) => chunk.sessionId === sessionId).sort((a, b) => a.seq - b.seq);
  if (stored.some((chunk) => !chunk.uploaded)) throw new Error("仍有片段等待本机服务确认。");
  const total = stored.reduce((n, chunk) => n + chunk.size, 0);
  const result = await api(`/api/sessions/${encodeURIComponent(sessionId)}/finalize`, {
    method: "POST", body: { chunk_count: stored.length, total_bytes: total }
  });
  session.finished = true;
  session.serverStatus = result.session.status;
  session.totalBytes = total;
  await putLocalSession(session);
  for (const chunk of stored) await deleteLocalChunk([sessionId, chunk.seq]);
  return result.session;
}

function setRecordButton(recording, disabled = false, label = "") {
  const button = byId("record-toggle");
  button.disabled = disabled;
  button.classList.toggle("is-recording", recording);
  button.textContent = label || (recording ? "停止并转写" : "开始录音");
  byId("wave-panel").hidden = !recording;
  byId("quick-actions").hidden = recording;
  byId("history-panel").hidden = recording;
}

function formatDuration(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function setLevelMessage(message, warning = false) {
  const node = byId("level-status");
  if (node.textContent !== message) node.textContent = message;
  if (node.classList.contains("error") !== warning) node.classList.toggle("error", warning);
}

function resizeWaveform() {
  if (!visualAnalyser) return;
  const canvas = byId("waveform");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const context = canvas.getContext("2d");
  context.lineWidth = 2 * dpr;
  context.strokeStyle = getComputedStyle(canvas).color;
  context.lineJoin = "round";
}

function drawWaveform() {
  if (!visualAnalyser || !mediaStream) return;
  const now = performance.now();
  if (visualContext.currentTime !== visualClockTime) { visualClockTime = visualContext.currentTime; visualClockAt = now; }
  else if (now - visualClockAt > 3000) { restartVisualMonitoring(); return; }
  const canvas = byId("waveform");
  const { width, height } = canvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, width, height);
  const samples = visualSamples;
  visualAnalyser.getByteTimeDomainData(samples);
  let sum = 0;
  for (const sample of samples) { const amplitude = (sample - 128) / 128; sum += amplitude * amplitude; }
  const rms = Math.sqrt(sum / samples.length);
  context.beginPath();
  for (let i = 0; i < samples.length; i++) {
    const x = (i / (samples.length - 1)) * width;
    const y = (samples[i] / 255) * height;
    if (i === 0) context.moveTo(x, y); else context.lineTo(x, y);
  }
  context.stroke();

  const track = mediaStream.getAudioTracks()[0];
  if (track?.readyState === "ended") setLevelMessage("麦克风已断开，请停止录音", true);
  else if (track?.muted) setLevelMessage("麦克风没有输入，请检查设备", true);
  else if (rms < 0.008) {
    if (!quietSince) quietSince = performance.now();
    setLevelMessage(performance.now() - quietSince > 1400 ? "收音偏小，靠近麦克风再说" : "正在听取麦克风音量…", performance.now() - quietSince > 1400);
  } else {
    quietSince = 0;
    setLevelMessage("收音正常");
  }
  visualFrame = requestAnimationFrame(drawWaveform);
}

function closeVisualGraph() {
  if (visualFrame) cancelAnimationFrame(visualFrame);
  visualFrame = 0;
  const context = visualContext;
  visualContext = null;
  visualAnalyser = null;
  visualSamples = null;
  if (context && context.state !== "closed") context.close().catch(() => {});
}

function stopVisualMonitoring() {
  closeVisualGraph();
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = 0;
  mediaStream?.getTracks().forEach((track) => { track.onended = null; track.onmute = null; });
}

// A mic/output switch can stall the analyser's AudioContext or feed it silence while MediaRecorder keeps
// recording the same live track; rebuild the analyser on that track.
function restartVisualMonitoring() {
  if (!visualContext || stopping) return;
  closeVisualGraph();
  startVisualMonitoring();
}

function startVisualMonitoring() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("浏览器不提供实时音量分析");
    visualContext = new AudioContextClass();
    visualAnalyser = visualContext.createAnalyser();
    visualAnalyser.fftSize = 1024;
    visualSamples = new Uint8Array(visualAnalyser.fftSize);
    resizeWaveform();
    const source = visualContext.createMediaStreamSource(mediaStream);
    source.connect(visualAnalyser);
    visualContext.resume().catch(() => {});
    const track = mediaStream.getAudioTracks()[0];
    if (track) {
      track.onended = () => { if (!stopping) setLevelMessage("麦克风已断开，请停止录音", true); };
      track.onmute = () => { if (!stopping) setLevelMessage("麦克风没有输入，请检查设备", true); };
    }
    quietSince = 0;
    visualClockTime = 0;
    visualClockAt = performance.now();
    visualFrame = requestAnimationFrame(drawWaveform);
  } catch (_) {
    setLevelMessage("麦克风已连接；此浏览器无法显示音量波形");
  }
}

async function startRecording() {
  if (recorder && recorder.state !== "inactive") { stopRecording(); return; }
  // The previous recording's stop handler resets the shared recorder state; starting before it ends loses this one's chunks.
  await recordingDone;
  setRecordButton(false, true, "正在启动麦克风…");
  setStatus("record-status", "请允许使用麦克风。录音片段会先保存到浏览器，再传给本机服务。");
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error("此浏览器不支持网页录音，请使用“上传音频”。");
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    let rawSettings = {};
    try { rawSettings = mediaStream.getAudioTracks()[0]?.getSettings?.() || {}; } catch (_) {}
    const captureSettings = {};
    for (const key of ["sampleRate", "channelCount", "echoCancellation", "noiseSuppression", "autoGainControl"]) {
      if (typeof rawSettings[key] === "number" || typeof rawSettings[key] === "boolean") captureSettings[key] = rawSettings[key];
    }
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    const mimeType = types.find((type) => MediaRecorder.isTypeSupported(type));
    recorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined);
    const created = await api("/api/sessions", { method: "POST", body: {
      source: "audio_capture", title: generatedTitle(), mime_type: recorder.mimeType || "application/octet-stream"
    } });
    recordingSession = {
      id: created.session.id, source: "audio_capture", title: created.session.title,
      mimeType: recorder.mimeType, startedAt: new Date().toISOString(), nextSeq: 0,
      totalBytes: 0, finished: false, serverStatus: "collecting", captureSettings
    };
    await putLocalSession(recordingSession);
    try { await navigator.storage?.persist?.(); } catch (_) {}
    recordingWrites = Promise.resolve();
    stopping = false;
    recorder.ondataavailable = (event) => {
      if (!event.data?.size || !recordingSession) return;
      const session = recordingSession;
      const seq = session.nextSeq || 0;
      session.nextSeq = seq + 1;
      recordingWrites = recordingWrites.then(() => saveChunk(session, seq, event.data))
        .then(() => pumpUploads(session.id))
        .catch((err) => { setStatus("record-status", `录音仍在浏览器；保存未完成：${err.message}。可在历史记录中重试。`, true); });
    };
    let finished;
    const onStopped = async () => {
      stopVisualMonitoring();
      mediaStream?.getTracks().forEach((track) => track.stop());
      await recordingWrites;
      const session = recordingSession;
      if (!session) return;
      session.finished = true;
      session.stoppedAt = new Date().toISOString();
      await putLocalSession(session);
      selectedSessionId = session.id;
      resetEditorState();
      showScreen("result");
      byId("result-title").textContent = session.title;
      byId("result-kind").textContent = "正在保存原音";
      byId("result-editor").value = "";
      byId("result-editor").disabled = true;
      byId("copy-primary").disabled = true;
      setStatus("save-status", "正在确认录音片段并开始转写…");
      try {
        await finalizeLocalSession(session.id);
        setStatus("save-status", "原始音频已保存；正在转写。完成后会显示可编辑文字。可在历史记录中回听原音。");
      } catch (err) {
        setStatus("save-status", `录音保留在浏览器本机队列中；${err.message}。打开历史记录后可重试。`, true);
      }
      recordingSession = null;
      recorder = null;
      mediaStream = null;
      stopping = false;
      byId("wave-panel").hidden = true;
      setRecordButton(false, false, "开始录音");
      await refreshArchive(session.id, false);
      await refreshSelectedSession();
    };
    recorder.onstop = () => onStopped().finally(finished);
    recorder.start(5000);
    recordingDone = new Promise((resolve) => { finished = resolve; });
    startedAt = performance.now();
    showScreen("record");
    setRecordButton(true, false);
    byId("record-time").textContent = "00:00";
    setStatus("record-status", "录音中。每 5 秒保存一个片段；按主按钮停止并转写。");
    startVisualMonitoring();
    clockTimer = window.setInterval(() => {
      const node = byId("record-time");
      const elapsed = formatDuration(performance.now() - startedAt);
      if (node.textContent !== elapsed) node.textContent = elapsed;
    }, 250);
  } catch (err) {
    stopVisualMonitoring();
    mediaStream?.getTracks().forEach((track) => track.stop());
    mediaStream = null;
    recorder = null;
    setRecordButton(false, false, "开始录音");
    setStatus("record-status", err.message, true);
  }
}

function stopRecording() {
  if (!recorder || recorder.state === "inactive") return;
  stopping = true;
  setRecordButton(true, true, "正在保存最后一段…");
  setStatus("record-status", "正在保存最后一个录音片段…");
  try { recorder.stop(); }
  catch (err) { setStatus("record-status", `无法正常结束录音：${err.message}`, true); }
}

function fileMime(file) {
  if (file.type) return file.type;
  const ext = file.name.split(".").pop().toLowerCase();
  return ({ wav: "audio/wav", mp3: "audio/mpeg", m4a: "audio/mp4", mp4: "audio/mp4", webm: "audio/webm", ogg: "audio/ogg", flac: "audio/flac", aac: "audio/aac", opus: "audio/opus" })[ext] || "application/octet-stream";
}

async function uploadFile(file) {
  if (!file || !file.size) return;
  if (file.size > MAX_BYTES) { setStatus("record-status", "文件超过 2 GiB，上限内分成几段上传。", true); return; }
  setStatus("record-status", "音频已选中；正在保存到本机…");
  try {
    const locals = await allLocalSessions();
    let session = locals.filter((item) => item.source === "audio_upload" && !item.finished && item.sourceFilename === file.name && item.totalBytesHint === file.size)
      .sort((a, b) => b.startedAt.localeCompare(a.startedAt))[0];
    if (!session) {
      const created = await api("/api/sessions", { method: "POST", body: {
        source: "audio_upload", title: file.name || generatedTitle("上传音频"), filename: file.name, mime_type: fileMime(file)
      } });
      session = { id: created.session.id, source: "audio_upload", title: created.session.title,
        sourceFilename: file.name, totalBytesHint: file.size, mimeType: fileMime(file),
        startedAt: new Date().toISOString(), nextSeq: 0, totalBytes: 0, finished: false, serverStatus: "collecting" };
      await putLocalSession(session);
    }
    try { await navigator.storage?.persist?.(); } catch (_) {}
    await pumpUploads(session.id);
    const count = Math.ceil(file.size / CHUNK_BYTES);
    const existing = (await getAllLocalChunks()).filter((item) => item.sessionId === session.id).map((item) => item.seq);
    let start = existing.length ? Math.max(...existing) + 1 : 0;
    for (let seq = start; seq < count; seq++) {
      const chunk = file.slice(seq * CHUNK_BYTES, Math.min(file.size, (seq + 1) * CHUNK_BYTES));
      await saveChunk(session, seq, chunk);
      setStatus("record-status", `正在保存音频 ${Math.min(file.size, (seq + 1) * CHUNK_BYTES).toLocaleString()} / ${file.size.toLocaleString()} 字节…`);
      await pumpUploads(session.id);
    }
    session.finished = true;
    await putLocalSession(session);
    selectedSessionId = session.id;
    resetEditorState();
    showScreen("result");
    byId("result-title").textContent = session.title;
    byId("result-kind").textContent = "正在保存原音";
    byId("result-editor").value = "";
    byId("result-editor").disabled = true;
    byId("copy-primary").disabled = true;
    setStatus("save-status", "完整音频已保存；正在转写…");
    await finalizeLocalSession(session.id);
    await refreshArchive(session.id, false);
    await refreshSelectedSession();
  } catch (err) {
    const message = `音频保留在浏览器本机队列中：${err.message}。可在历史记录中重试。`;
    if (screenMode === "result") setStatus("save-status", message, true);
    else setStatus("record-status", message, true);
    await refreshArchive(selectedSessionId, true);
  }
}

function prettyStatus(session) {
  const labels = {
    collecting: "正在接收音频", queued: "原音频已保存，等待转写", transcribing: "正在转写",
    transcribed: "转写完成，请核对", transcription_failed: "转写失败，原音已保存，可重试",
    transcription_unavailable: "转写服务未就绪，原音已保存", text_saved: "文字已保存"
  };
  return labels[session.status] || session.status || "状态未知";
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const PURPOSE_LABELS = { recognition_correction: "听写纠错", user_rewrite: "表达改写", other: "其他修订" };
const chosenPurpose = () => document.querySelector('input[name="purpose"]:checked')?.value || "recognition_correction";

// The purpose this exact revision was confirmed with; null for transcripts and drafts nobody confirmed.
function confirmedPurpose(rev) {
  if (rev?.verification?.current) return "recognition_correction";
  const type = rev?.kind === "correction" ? rev.data?.correction_type : null;
  return type === "user_rewrite" || type === "other" ? type : null;
}

function revisionLabel(rev) {
  if (rev.kind === "correction") {
    if (rev.data?.correction_type === "draft") return "修改稿 · 未确认";
    return `用户修订 · ${PURPOSE_LABELS[rev.data?.correction_type] || "未分类"}`;
  }
  if (rev.source === "llm_postprocess") return "本地模型润色 · 未人工核对";
  return ({ original_text: "原始文字", asr: "原始转写", postprocess: "自动术语修正 · 未人工核对", correction: "用户修订", instruction: "明确创建的指令" })[rev.kind] || rev.kind;
}

function revisionForEditing(revisions) {
  return [...(revisions || [])].reverse().find((rev) => rev.kind !== "instruction") || null;
}

function polishCoverageWarning(detail, rev) {
  // Keep the machine warning on its saved edits; viewing the original clears it.
  while (rev?.kind === "correction") rev = detail.revisions.find((item) => item.id === rev.parent_id);
  if (rev?.source !== "llm_postprocess") return "";
  const reasons = [];
  const ratio = rev.data?.polish_length_ratio;
  if (typeof ratio === "number" && ratio < 0.6) reasons.push(`字数只有原稿的 ${Math.round(ratio * 100)}%`);
  if (rev.data?.polish_tail_found === false) reasons.push("没找到原稿最后一句");
  const unpolished = rev.data?.polish_unpolished_sentences || [];
  const warnings = unpolished.map((sentence, index) =>
    `第 ${index + 1} 句因模型重试 2 次仍复读，已保留原文：「${sentence}」`);
  if (!unpolished.length) {
    warnings.push(...(rev.data?.polish_fallback_pieces || []).map((index) =>
      `旧版第 ${index}/${rev.data.polish_piece_count} 段没润色成，保留原文`));
  }
  if (reasons.length) warnings.push(`润色稿可能漏了内容（${reasons.join("／")}），请对照原始转写`);
  return warnings.join("；");
}

function detailSignature(detail) {
  return JSON.stringify([detail.id, detail.status, detail.error || "", detail.audio_ready, detail.original_sha256 || "", (detail.revisions || []).map((rev) => [rev.id, Boolean(rev.verification?.current)])]);
}

function resetEditorState() {
  clearTimeout(autosaveTimer);
  autosaveTimer = 0;
  baseRevisionId = null;
  savedText = "";
  editorDirty = false;
  autosavePromise = null;
  byId("result-editor").value = "";
  byId("result-editor").disabled = true;
  byId("copy-primary").disabled = true;
  byId("verify-panel").hidden = true;
  byId("llm-coverage-warning").hidden = true;
}

function showRevision(detail, rev, focus = false) {
  if (!rev) return;
  selectedDetail = detail;
  baseRevisionId = rev.id;
  savedText = rev.content;
  editorDirty = false;
  if (byId("result-editor").value !== rev.content) byId("result-editor").value = rev.content;
  byId("result-editor").disabled = false;
  byId("copy-primary").disabled = false;
  document.querySelector(`input[name="purpose"][value="${confirmedPurpose(rev) || "recognition_correction"}"]`).checked = true;
  byId("result-title").textContent = detail.title || "输入记录";
  const warning = polishCoverageWarning(detail, rev);
  byId("llm-coverage-warning").textContent = warning;
  byId("llm-coverage-warning").hidden = !warning;
  const llm = rev.source === "llm_postprocess";
  byId("result-kind").textContent = llm ? "本地模型润色 · 请核对（原稿已保留）" : rev.kind === "postprocess" ? "自动术语修正 · 请核对（原稿已保留）" : rev.kind === "asr" ? "录音转写 · 请核对" : rev.kind === "original_text" ? "原始文字" : "已保存文字 · 可继续修订";
  setStatus("save-status", llm ? "这是机器稿，核对前不算逐字稿。本地模型已整理这段文字，原稿已保留；点「看回原文」对照。" : rev.kind === "postprocess" ? `已应用个人术语规则，原稿已保留。${reviewNote(rev)}` : rev.kind === "asr" ? reviewNote(rev) : "已保存到本机。修改会自动存为修改稿，原始内容完整保留。");
  updateVerifyPanel(detail, rev);
  if (focus) byId("result-editor").focus();
}

function updateVerifyPanel(detail, rev) {
  const panel = byId("verify-panel");
  panel.hidden = !(detail && rev && detail.source !== "text" && detail.audio_ready && rev.kind !== "instruction");
  const done = confirmedPurpose(rev);
  const same = Boolean(done) && done === chosenPurpose() && !editorDirty;
  byId("verify-revision").textContent = same ? "已确认" : "确认保存";
  byId("verify-revision").disabled = same || Boolean(byId("verify-revision").dataset.busy);
  if (panel.hidden) return;
  setStatus("verify-status", same
    ? `已确认为${PURPOSE_LABELS[done]}${done === "recognition_correction" ? "，与原音逐字一致，可作听写参考" : "，不作听写训练数据"}。再修改需要重新确认。`
    : done ? `之前确认为${PURPOSE_LABELS[done]}；要改成所选用途，点“确认保存”。`
    : rev.verification ? "这一版之后又有修改，需要重新确认。"
    : rev.source === "llm_postprocess" ? "这是机器稿，核对前不算逐字稿。回听原音、改好后选好用途，点“确认保存”才算数。"
    : "修改会自动存为修改稿。改完选好用途，点“确认保存”才算数。");
}

function markVerifyStale() {
  if (byId("verify-panel").hidden) return;
  byId("verify-revision").textContent = "确认保存";
  byId("verify-revision").disabled = Boolean(byId("verify-revision").dataset.busy);
  setStatus("verify-status", "修改会自动存为修改稿。改完选好用途，点“确认保存”才算数。");
}

function onPurposeChange() {
  const rev = selectedDetail?.revisions?.find((item) => item.id === baseRevisionId);
  if (rev) updateVerifyPanel(selectedDetail, rev); else markVerifyStale();
}

async function verifyCurrentRevision() {
  const button = byId("verify-revision");
  if (button.dataset.busy || button.disabled) return;
  const intendedSessionId = selectedSessionId;
  const reviewedText = byId("result-editor").value;
  const purpose = chosenPurpose();
  button.dataset.busy = "true";
  button.disabled = true;
  try {
    await flushEditor();
    if (selectedSessionId !== intendedSessionId || byId("result-editor").value !== reviewedText) throw new Error("记录或文字已变化，请重新确认。");
    const sessionId = selectedSessionId;
    const baseId = baseRevisionId;
    if (!sessionId || !baseId) throw new Error("当前修订还没有保存。");
    if (editorDirty || byId("result-editor").value !== savedText) throw new Error("有新的修改尚未保存，请保存后重新确认。");
    const detail = (await api(`/api/sessions/${encodeURIComponent(sessionId)}`)).session;
    if (selectedSessionId !== intendedSessionId || baseRevisionId !== baseId || editorDirty || byId("result-editor").value !== reviewedText) throw new Error("记录或文字已变化，请重新确认。");
    if (!detail.original_sha256) throw new Error("原始音频尚未完成。");
    const base = detail.revisions.find((item) => item.id === baseId);
    if (!base) throw new Error("找不到当前版本，请刷新后重试。");
    // The purpose chosen now is what gets recorded: label this exact text unless it already carries it.
    const labelled = base.data?.correction_type === purpose
      || (purpose === "recognition_correction" && (base.kind === "asr" || base.kind === "postprocess"));
    if (!labelled) {
      const result = await api(`/api/sessions/${encodeURIComponent(sessionId)}/corrections`, {
        method: "POST", body: { parent_revision_id: baseId, text: reviewedText, correction_type: purpose }
      });
      baseRevisionId = result.revision.id;
      savedText = reviewedText;
    }
    const revisionId = baseRevisionId;
    if (purpose === "recognition_correction") {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/verifications`, {
        method: "POST", body: { revision_id: revisionId, original_sha256: detail.original_sha256 }
      });
    }
    const fresh = (await api(`/api/sessions/${encodeURIComponent(sessionId)}`)).session;
    if (selectedSessionId !== intendedSessionId) return;
    selectedDetail = fresh;
    setStatus("save-status", `已确认并保存为${PURPOSE_LABELS[purpose]}；原始内容完整保留。`);
    await renderArchiveDetail(fresh);
    await refreshHistoryListOnly();
  } catch (err) {
    if (selectedSessionId === intendedSessionId) setStatus("verify-status", `未确认：${err.message}`, true);
    button.disabled = false;
    return;
  } finally {
    delete button.dataset.busy;
  }
  onPurposeChange();
}

function reviewNote(rev) {
  if (rev.data?.metadata?.boundary_review_required) return "长录音按窗口转写，请回听分段边界及人名、数字、否定词；尚未人工核对。";
  const gaps = rev.data?.metadata?.coverage_gaps;
  if (Array.isArray(gaps) && gaps.length) {
    const ranges = gaps.map((gap) => `${gap.start}–${gap.end} 秒`).join("、");
    return `转写草稿已保存，请回听 ${ranges} 等未覆盖区间；也请核对人名、数字和否定词。`;
  }
  return "未经人工核对的转写草稿已保存，请回听原音核对人名、数字和否定词。";
}

async function appendLatestRevision() {
  while (true) {
    const text = byId("result-editor").value;
    if (text === savedText) { editorDirty = false; return baseRevisionId; }
    if (!text.trim()) throw new Error("文字不能为空；原始版本仍保留。请先恢复文字后再保存。");
    if (!baseRevisionId || !selectedSessionId) throw new Error("找不到可作为父版本的原始文字。");
    setStatus("save-status", "正在保存修改稿…");
    const result = await api(`/api/sessions/${encodeURIComponent(selectedSessionId)}/corrections`, {
      method: "POST", body: { parent_revision_id: baseRevisionId, text, correction_type: "draft" }
    });
    baseRevisionId = result.revision.id;
    savedText = text;
    editorDirty = byId("result-editor").value !== savedText;
    setStatus("save-status", editorDirty ? "已保存一版，正在保存刚才的继续编辑…" : "修改稿已自动保存，原始内容完整保留。");
    selectedDetail = null;
    await refreshHistoryListOnly();
    if (!editorDirty) return baseRevisionId;
  }
}

async function flushEditor() {
  clearTimeout(autosaveTimer);
  autosaveTimer = 0;
  if (!editorDirty && byId("result-editor").value === savedText) return baseRevisionId;
  if (!autosavePromise) autosavePromise = appendLatestRevision().finally(() => { autosavePromise = null; });
  return autosavePromise;
}

function scheduleAutosave() {
  editorDirty = byId("result-editor").value !== savedText;
  if (!editorDirty) {
    clearTimeout(autosaveTimer);
    setStatus("save-status", "与最近保存版本相同。");
    return;
  }
  setStatus("save-status", "有未保存修改…");
  markVerifyStale();
  clearTimeout(autosaveTimer);
  autosaveTimer = window.setTimeout(() => flushEditor().catch((err) => setStatus("save-status", `保存失败：${err.message}；文字仍留在编辑框中，可重试。`, true)), AUTOSAVE_DELAY);
}

async function copyText() {
  let saveError = null;
  try {
    await flushEditor();
  } catch (err) {
    saveError = err;
  }
  try {
    const text = byId("result-editor").value;
    if (!text.trim()) throw new Error("目前没有可复制的文字。");
    try { await navigator.clipboard.writeText(text); }
    catch (_) {
      const editor = byId("result-editor");
      editor.focus(); editor.select();
      if (!document.execCommand("copy")) throw new Error("复制未成功，请手动选择文字复制。");
    }
    setStatus("save-status", saveError
      ? `文字已复制，可先粘贴；本机修订仍未保存：${saveError.message}。请留在此页重试。`
      : "文字已复制。可以粘贴到任何聊天或软件。");
  } catch (err) {
    setStatus("save-status", `${saveError ? `修订尚未保存：${saveError.message}。` : ""}${err.message}`, true);
  }
}

function backupTime(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.valueOf()) ? date.toLocaleString() : null;
}

async function refreshBackupStatus() {
  const node = byId("backup-status");
  try {
    const response = await fetch("/api/backup", { credentials: "same-origin" });
    if (!response.ok) throw new Error("status unavailable");
    const status = await response.json();
    const phrases = ["本机已保存"];
    if (status.local_snapshot_verified) phrases.push(`最近本地备份已校验 ${backupTime(status.local_snapshot_at_utc) || "时间未知"}`);
    else if (status.local_snapshot_recorded) phrases.push(`本地备份清单 ${backupTime(status.local_snapshot_at_utc) || "时间未知"}，尚未重新校验`);
    else phrases.push("尚无已验证本地备份");
    if (status.last_attempt_failed) phrases.push("最近备份失败");
    if (status.chat_capture?.status === "failed") phrases.push("聊天记录同步失败，已有记录仍保留");
    if (status.remote_receipt_valid && status.remote_matches_latest_local) phrases.push(`云端已核验 ${backupTime(status.remote_verified_at_utc) || "时间未知"}`);
    else if (status.daily_archive_waiting) phrases.push("等待每日美东 20:00 打包备份");
    else if (status.drive_staged_for_sync) phrases.push("Drive 已暂存，云端尚未确认");
    else phrases.push("云端备份尚未确认");
    node.textContent = phrases.join(" · ");
  } catch (_) { node.textContent = "备份状态暂不可读；录音和文字仍显示本机保存状态。"; }
}

async function fetchSessions() {
  const result = await api("/api/sessions");
  serverSessions = result.sessions;
}

async function refreshHistoryListOnly() {
  const [locals, chunks] = await Promise.all([allLocalSessions().catch(() => []), getAllLocalChunks().catch(() => [])]);
  const localById = new Map(locals.map((session) => [session.id, session]));
  const merged = new Map(serverSessions.map((session) => [session.id, { ...session, local: localById.get(session.id) }]));
  for (const session of locals) if (!merged.has(session.id)) merged.set(session.id, { ...session, status: session.serverStatus || "collecting", local: session });
  const list = byId("session-list");
  list.replaceChildren();
  const sorted = [...merged.values()].sort((a, b) => (b.updated_at || b.startedAt || "").localeCompare(a.updated_at || a.startedAt || ""));
  for (const session of sorted) {
    const button = make("button", `session-item${session.id === selectedSessionId ? " selected" : ""}`);
    button.type = "button";
    const local = session.local || localById.get(session.id);
    const info = local ? localStatus(local, chunks) : { waiting: 0 };
    const state = local?.finished === false && local.source === "audio_capture" && session.id !== recordingSession?.id ? "录音中断，可恢复" : prettyStatus(session);
    button.append(make("span", "session-title", session.title || session.sourceFilename || "输入记录"), make("span", "session-meta", info.waiting ? `${state} · ${info.waiting} 段待重试` : state));
    button.addEventListener("click", async () => {
      try { await flushEditor(); } catch (err) { setStatus("save-status", `请先完成修订保存：${err.message}`, true); return; }
      selectedSessionId = session.id;
      resetEditorState();
      await loadSession(session.id, true);
    });
    list.append(button);
  }
  if (!sorted.length) list.append(make("p", "empty", "暂无记录。完成一次录音或保存文字后会显示在这里。"));
}

async function refreshArchive(preferId = selectedSessionId, loadSelected = true) {
  await fetchSessions();
  if (preferId && serverSessions.some((session) => session.id === preferId)) selectedSessionId = preferId;
  await refreshBackupStatus();
  await refreshHistoryListOnly();
  if (loadSelected && selectedSessionId) await loadSession(selectedSessionId, true);
}

async function loadSession(sessionId, showResult = false) {
  selectedSessionId = sessionId;
  const host = byId("archive-detail");
  let detail = null;
  try { detail = (await api(`/api/sessions/${encodeURIComponent(sessionId)}`)).session; }
  catch (_) {
    host.replaceChildren(make("p", "empty", "本机服务暂不可读。浏览器仍保留的片段会在服务恢复后重试。"));
    return;
  }
  selectedDetail = detail;
  byId("result-screen").setAttribute("aria-busy", String(detail.status === "transcribing"));
  const latest = revisionForEditing(detail.revisions);
  if (latest && (showResult || !baseRevisionId || (baseRevisionId && !detail.revisions.some((rev) => rev.id === baseRevisionId)))) {
    showRevision(detail, latest, showResult);
    if (showResult) showScreen("result");
  } else if (latest && !baseRevisionId && selectedSessionId === detail.id) {
    showRevision(detail, latest, showResult);
    if (showResult) showScreen("result");
  } else if (!latest) {
    if (showResult) showScreen("result");
    byId("result-title").textContent = detail.title || "输入记录";
    byId("result-kind").textContent = prettyStatus(detail);
    byId("result-editor").value = "";
    byId("result-editor").disabled = true;
    byId("copy-primary").disabled = true;
    byId("verify-panel").hidden = true;
    setStatus("save-status", detail.error ? `转写失败：${detail.error}；原始音频已保留。可在历史记录中重试。` : detail.source === "text" ? "等待原始文字…" : "原始音频已保留；等待转写完成。可在历史记录中回听或重试。");
  }
  await renderArchiveDetail(detail);
  await refreshHistoryListOnly();
}

// Local-model post-processing: button, "back to original" and the default checkbox.
let pinnedLatestId = null;  // newest revision the user chose to look past (viewing the original)
let llmState = { enabled: false, auto: false, running: [], errors: {} };
let llmBusy = false;

async function refreshAsrHours() {
  try { byId("asr-hours").textContent = (await api("/api/asr-hours")).text; }
  catch (_) { byId("asr-hours").textContent = "可训练语音小时数暂时读不出来（不影响录音）"; }
}

async function refreshLlmUi() {
  try { llmState = await api("/api/llm-postprocess"); } catch (_) { return; }
  const run = byId("llm-run"), back = byId("llm-original");
  const detail = selectedDetail;
  const original = detail?.revisions?.find((rev) => rev.kind === "asr" || rev.kind === "original_text");
  const shown = detail?.revisions?.find((rev) => rev.id === baseRevisionId);
  const working = llmBusy || (detail && llmState.running.includes(detail.id));
  run.disabled = !(detail && original && baseRevisionId) || !llmState.enabled || working;
  run.textContent = working ? "润色中…" : "润色";
  run.title = llmState.enabled ? "" : "本地润色未启用：把 llm-postprocess.json 里的 enabled 设为 true";
  back.hidden = !(shown && shown.source === "llm_postprocess" && shown.parent_id);
  byId("llm-auto").checked = Boolean(llmState.auto);
  byId("llm-note").textContent = llmState.config_error || (llmState.enabled
    ? `本地模型：${llmState.model}。勾上后，新转写完成时自动润色一次；原稿始终保留。`
    : "本地润色未启用（默认关闭）：把数据目录里 llm-postprocess.json 的 enabled 设为 true 后可用。");
  const failure = detail && llmState.errors[detail.id];
  if (failure && !working) setStatus("save-status", `润色未完成：${failure}`, true);
}

byId("llm-run").addEventListener("click", async () => {
  if (!selectedSessionId) return;
  const sid = selectedSessionId;  // autosaving can clear selectedDetail before this click
  llmBusy = true; byId("llm-run").disabled = true; byId("llm-run").textContent = "润色中…";
  setStatus("save-status", "润色中…本地模型正在整理，原稿已保留。");
  try {
    const shownId = await flushEditor();  // the version on screen, saved: this is what gets polished
    const result = await api(`/api/sessions/${encodeURIComponent(sid)}/llm-postprocess`, { method: "POST", body: { revision_id: shownId || baseRevisionId } });
    pinnedLatestId = null;
    const detail = (await api(`/api/sessions/${encodeURIComponent(sid)}`)).session;
    const rev = detail.revisions.find((item) => item.id === result.revision.id);
    showRevision(detail, rev, false);
    await renderArchiveDetail(detail);
  } catch (err) { setStatus("save-status", `润色失败，没有写入新版本：${err.message}`, true); }
  finally { llmBusy = false; await refreshLlmUi(); }
});
byId("llm-original").addEventListener("click", async () => {
  const shown = selectedDetail?.revisions?.find((rev) => rev.id === baseRevisionId);
  const original = selectedDetail?.revisions?.find((rev) => rev.id === shown?.parent_id);
  if (!original) return;
  try { await flushEditor(); } catch (err) { setStatus("save-status", `请先保存当前修订后再切换版本：${err.message}`, true); return; }
  pinnedLatestId = shown.id;
  showRevision(selectedDetail, original, false);
  await refreshLlmUi();
});
byId("llm-auto").addEventListener("change", async (event) => {
  const box = event.target;
  try { llmState = await api("/api/llm-postprocess/auto", { method: "POST", body: { auto: box.checked } }); }
  catch (err) { box.checked = !box.checked; byId("llm-note").textContent = `保存设置失败：${err.message}`; return; }
  await refreshLlmUi();
});

async function refreshSelectedSession() {
  if (!selectedSessionId) return;
  try {
    const detail = (await api(`/api/sessions/${encodeURIComponent(selectedSessionId)}`)).session;
    const changed = archiveDetailSignature !== detailSignature(detail);
    const nowLatest = revisionForEditing(detail.revisions);
    selectedDetail = detail;
    byId("result-screen").setAttribute("aria-busy", String(detail.status === "transcribing"));
    if (nowLatest && (!baseRevisionId || (!editorDirty && byId("result-editor").value === savedText && nowLatest.id !== baseRevisionId && nowLatest.id !== pinnedLatestId))) {
      showRevision(detail, nowLatest);
      showScreen("result");
    } else if (!nowLatest) {
      byId("result-kind").textContent = prettyStatus(detail);
      if (detail.error) setStatus("save-status", `转写失败：${detail.error}；原始音频已保留。可在历史记录中重试。`, true);
    }
    if (changed) {
      await renderArchiveDetail(detail);
      await refreshHistoryListOnly();
    }
  } catch (_) {
    if (screenMode === "result") setStatus("save-status", "本机服务暂不可读；当前文字留在编辑框中，可稍后重试保存。", true);
  }
}

async function renderArchiveDetail(detail) {
  const host = byId("archive-detail");
  const [local, chunks] = await Promise.all([getLocalSession(detail.id).catch(() => null), getAllLocalChunks().catch(() => [])]);
  const info = local ? localStatus(local, chunks) : { own: [], waiting: 0 };
  archiveDetailSignature = detailSignature(detail);
  host.replaceChildren();
  host.append(make("h2", "", detail.title || "输入记录"));
  host.append(make("p", "", `${prettyStatus(detail)} · ${detail.source} · ${detail.created_at || "时间未知"}`));
  if (detail.error) host.append(make("p", "", `处理未完成：${detail.error}`));

  if (detail.source !== "text" && detail.audio_ready) {
    const audio = make("audio", "", "");
    audio.controls = true; audio.preload = "metadata";
    audio.src = `/api/sessions/${encodeURIComponent(detail.id)}/audio`;
    host.append(audio);
    const download = make("a", "", `下载原始音频 · SHA-256 ${detail.original_sha256}`);
    download.href = `/api/sessions/${encodeURIComponent(detail.id)}/audio?download=1`;
    download.download = "original-audio";
    host.append(download);
  }
  if (local?.captureSettings && Object.keys(local.captureSettings).length) {
    const settings = local.captureSettings;
    const parts = [];
    if (settings.sampleRate) parts.push(`${settings.sampleRate} Hz`);
    if (settings.channelCount) parts.push(`${settings.channelCount} 声道`);
    for (const key of ["echoCancellation", "noiseSuppression", "autoGainControl"]) {
      if (typeof settings[key] === "boolean") parts.push(`${({ echoCancellation: "回声消除", noiseSuppression: "降噪", autoGainControl: "自动增益" })[key]}${settings[key] ? "开" : "关"}`);
    }
    host.append(make("p", "", `浏览器录音设置（本机记录）：${parts.join(" · ")}`));
  }

  if (local && info.waiting) {
    const recovery = make("div", "recovery");
    recovery.append(make("p", "", `浏览器本地还有 ${info.waiting} 个未确认片段；它们尚未计入服务端原件。`));
    const retry = make("button", "", "重试上传片段");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        await pumpUploads(detail.id);
        if (local.finished) await finalizeLocalSession(detail.id);
        await refreshArchive(detail.id, true);
      } catch (err) { recovery.querySelector("p").textContent = `上传未完成，浏览器片段仍保留：${err.message}`; }
      finally { retry.disabled = false; }
    });
    recovery.append(retry); host.append(recovery);
  }
  if (local && local.finished === false && detail.source === "audio_capture" && info.own.length && detail.id !== recordingSession?.id) {
    const recovery = make("div", "recovery");
    recovery.append(make("p", "", "这条录音被刷新或关闭打断。可以完成已经安全保存的片段；未保存的最后一段无法恢复。"));
    const finish = make("button", "", "完成已保存片段"); finish.type = "button";
    finish.addEventListener("click", async () => {
      finish.disabled = true;
      try { local.finished = true; await putLocalSession(local); await finalizeLocalSession(detail.id); await refreshArchive(detail.id, true); }
      catch (err) { recovery.querySelector("p").textContent = `尚未完成；浏览器片段仍保留：${err.message}`; finish.disabled = false; }
    });
    recovery.append(finish); host.append(recovery);
  }
  if (detail.source === "audio_upload" && detail.status === "collecting") host.append(make("p", "", "上传中断时，请重新选择同一文件继续。"));

  if (detail.audio_ready && ["transcription_failed", "transcription_unavailable"].includes(detail.status)) {
    const retry = make("button", "", "重试转写"); retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try { await api(`/api/sessions/${encodeURIComponent(detail.id)}/retry-asr`, { method: "POST", body: {} }); await refreshSelectedSession(); }
      catch (err) { retry.disabled = false; setStatus("save-status", `重试未开始：${err.message}`, true); }
    });
    host.append(retry);
  }

  const nonInstructions = (detail.revisions || []).filter((rev) => rev.kind !== "instruction");
  if (!nonInstructions.length) host.append(make("p", "empty", "暂无文字版本。录音原件保留后，转写结果会出现在编辑区。"));
  if (nonInstructions.length) {
    host.append(make("h3", "", `原文与修订 · ${nonInstructions.length} 个版本`));
    const list = make("div", "revision-list");
    for (const rev of [...detail.revisions].reverse()) {
      const card = make("article", "revision");
      const head = make("div", "revision-head");
      head.append(make("strong", "", `${revisionLabel(rev)} · ${rev.created_at || ""}`));
      const use = make("button", "", rev.kind === "instruction" ? "查看指令" : "在编辑区打开");
      use.type = "button";
      use.addEventListener("click", async () => {
        if (rev.kind !== "instruction") {
          try { await flushEditor(); }
          catch (err) { setStatus("save-status", `请先保存当前修订后再切换版本：${err.message}`, true); return; }
          pinnedLatestId = revisionForEditing(detail.revisions)?.id || null;  // the polling refresh must not jump back past this deliberate pick
          showRevision(detail, rev, true);
          showScreen("result");
        }
      });
      head.append(use); card.append(head);
      const content = make("p", "revision-content", rev.content);
      card.append(content);
      card.append(make("p", "revision-meta", `SHA-256 ${rev.sha256} · ${rev.source} · ${rev.parent_id ? `父版本 ${rev.parent_id}` : "原始版本"}`));
      if (rev.verification) {
        card.append(make("p", "revision-meta", rev.verification.current
          ? `人工核对全段录音 · ${rev.verification.verified_at} · 原音 SHA-256 ${rev.verification.original_sha256}`
          : "曾人工核对 · 已被后续版本取代，不再有效"));
      }
      if (["asr", "postprocess"].includes(rev.kind)) {
        const gaps = rev.data?.metadata?.coverage_gaps;
        if (Array.isArray(gaps) && gaps.length) card.append(make("p", "", `建议回听未覆盖区间：${gaps.map((gap) => `${gap.start}–${gap.end} 秒`).join("、")}。`));
        else if (rev.data?.metadata?.review_required) card.append(make("p", "", "转写未经人工核对，请回听人名、数字、否定和未识别区间。"));
      }
      const exportLink = make("a", "", "导出此版本为文本文件");
      exportLink.href = `/api/revisions/${encodeURIComponent(rev.id)}/export.txt`;
      exportLink.download = `revision-${rev.id}.txt`;
      card.append(exportLink);
      list.append(card);
    }
    host.append(list);
  }

  const current = selectedSessionId === detail.id ? baseRevisionId : null;
  if (current && nonInstructions.some((rev) => rev.id === current)) {
    const actions = make("div", "recovery");
    const handoff = make("button", "", "交给总理"); handoff.type = "button";
    handoff.title = "将当前保存版本创建为明确待处理指令；录音本身不会自动提交。";
    handoff.addEventListener("click", async () => {
      handoff.disabled = true;
      try {
        await flushEditor();
        if (!baseRevisionId) throw new Error("当前修订还没有保存。");
        const result = await api(`/api/sessions/${encodeURIComponent(detail.id)}/instructions`, { method: "POST", body: { parent_revision_id: baseRevisionId, text: savedText } });
        setStatus("save-status", "已保存，等待总理处理。");
        await refreshArchive(detail.id, false);
        await renderArchiveDetail((await api(`/api/sessions/${encodeURIComponent(detail.id)}`)).session);
        void result;
      } catch (err) { setStatus("save-status", `未创建待处理指令：${err.message}`, true); }
      finally { handoff.disabled = false; }
    });
    actions.append(handoff, make("p", "", "创建前会先保存最新修订；录音本身不会自动提交。指令已保存也不代表已回复。"));
    host.append(actions);
  }
}

async function saveText() {
  const text = byId("text-input").value;
  if (!text.trim()) { setStatus("text-status", "先写下一些文字。", true); return; }
  const button = byId("save-text"); button.disabled = true;
  setStatus("text-status", "正在把原始文字保存到本机…");
  try {
    const result = await api("/api/text", { method: "POST", body: { title: generatedTitle("文字"), text } });
    byId("text-input").value = "";
    selectedSessionId = result.session.id;
    resetEditorState();
    await refreshArchive(result.session.id, false);
    await loadSession(result.session.id, true);
  } catch (err) { setStatus("text-status", `保存失败；文字仍留在输入框中：${err.message}`, true); }
  finally { button.disabled = false; }
}

async function resumePending() {
  const sessions = await allLocalSessions();
  const chunks = await getAllLocalChunks();
  for (const session of sessions) {
    const own = chunks.filter((chunk) => chunk.sessionId === session.id);
    if (own.some((chunk) => !chunk.uploaded)) {
      try { await pumpUploads(session.id); } catch (_) {}
    }
    if (session.finished && own.length) {
      try { await finalizeLocalSession(session.id); } catch (_) {}
    }
  }
}

byId("record-toggle").addEventListener("click", () => {
  if (recorder && recorder.state !== "inactive") stopRecording(); else void startRecording();
});
navigator.mediaDevices?.addEventListener("devicechange", restartVisualMonitoring);
window.addEventListener("resize", resizeWaveform);
byId("text-open").addEventListener("click", async () => {
  try { await flushEditor(); showScreen("text"); }
  catch (err) { setStatus("save-status", `请先保存当前修订后再离开：${err.message}`, true); }
});
byId("upload-open").addEventListener("click", () => byId("audio-file").click());
byId("text-cancel").addEventListener("click", () => { showScreen("record"); });
byId("save-text").addEventListener("click", () => void saveText());
byId("audio-file").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (file) void uploadFile(file);
});
byId("result-editor").addEventListener("input", scheduleAutosave);
byId("result-editor").addEventListener("blur", () => {
  if (editorDirty) flushEditor().catch((err) => setStatus("save-status", `保存失败：${err.message}；文字仍留在编辑框中，可重试。`, true));
});
document.querySelectorAll('input[name="purpose"]').forEach((radio) => radio.addEventListener("change", onPurposeChange));
byId("copy-primary").addEventListener("click", () => void copyText());
byId("verify-revision").addEventListener("click", () => void verifyCurrentRevision());
byId("new-recording").addEventListener("click", async () => {
  const button = byId("new-recording");
  if (button.disabled) return;
  button.disabled = true;
  try {
    await flushEditor();
    resetEditorState(); selectedSessionId = null; selectedDetail = null; showScreen("record");
    await startRecording();
  } catch (err) {
    setStatus("save-status", `请先保存当前修订后再开始新录音：${err.message}`, true);
  } finally { button.disabled = false; }
});
byId("refresh").addEventListener("click", async () => {
  try {
    await flushEditor();
    await refreshArchive(selectedSessionId, false);
    if (selectedSessionId) await loadSession(selectedSessionId, false);
  }
  catch (err) { setStatus("save-status", `刷新失败：${err.message}`, true); }
});
window.addEventListener("keydown", (event) => {
  const tag = event.target?.tagName;
  if (event.code === "Space" && !event.repeat && !event.ctrlKey && !event.metaKey && !event.altKey && !["TEXTAREA", "INPUT", "BUTTON", "A"].includes(tag) && screenMode === "record") {
    event.preventDefault();
    if (recorder && recorder.state !== "inactive") stopRecording(); else void startRecording();
  }
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && screenMode === "result") { event.preventDefault(); void copyText(); }
});
window.addEventListener("beforeunload", (event) => {
  const textDraft = screenMode === "text" && byId("text-input").value.trim();
  if ((recorder && recorder.state !== "inactive") || editorDirty || autosavePromise || textDraft) { event.preventDefault(); event.returnValue = ""; }
});

(async () => {
  try {
    await openDb();
    try { await navigator.storage?.persist?.(); } catch (_) {}
    await refreshArchive(null, false);
    await resumePending();
    await refreshArchive(null, false);
    // /?session=<id>: open that recording directly (the history list only holds the newest 200)
    const wanted = new URLSearchParams(location.search).get("session");
    if (wanted) await loadSession(wanted, true);
    await refreshLlmUi();
    refreshAsrHours();
    window.setInterval(async () => {
      try {
        const model = await api("/api/asr");
        byId("model-status").textContent = model.ready ? `${model.model?.includes("Q5") ? "Nano Q5 · GPU" : "Nano"} 已就绪 · 模型常驻本机` : model.state === "loading" ? "Nano 加载与预热中…录音仍可保存" : "Nano 暂未就绪 · 原音仍会保存，可重试转写";
        await fetchSessions();
        await refreshBackupStatus();
        const session = serverSessions.find((item) => item.id === selectedSessionId);
        if (session && screenMode === "result") await refreshSelectedSession();
        await refreshLlmUi();
      } catch (_) {
        if (screenMode === "record" && !(recorder && recorder.state !== "inactive")) setStatus("record-status", "本机服务暂不可读；本地浏览器保存队列仍可在恢复后重试。", true);
      }
    }, 3000);
  } catch (err) {
    setStatus("record-status", `浏览器不支持本地恢复队列：${err.message}`, true);
  }
})();
