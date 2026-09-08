(() => {
const previousState = globalThis.__geminiLiveTranslatorContentState;
try { previousState?.dispose?.(); } catch (_) {}
if (!previousState?.dispose) document.querySelector("#gemini-live-subtitle")?.remove();
delete globalThis.__geminiLiveTranslatorContentLoaded;

const defaults = { displayMode: "sentence", subtitleLanguage: "bilingual" };
let settings = { ...defaults };
let root, history, sourceHistory, translationHistory, pending;
let latestInput = "", latestOutput = "", lastCommitted = "";
let historyEntries = [];
let pointerAction = null;
let closedByUser = false;
let disposed = false;
let messageListener = null;
let pendingCommitTimer = null;
let savedLayout = null;

function disposeContentScript() {
  if (disposed) return;
  disposed = true;
  root?.remove();
  clearTimeout(pendingCommitTimer);
  root = history = sourceHistory = translationHistory = pending = null;
  historyEntries = [];
  try { if (messageListener) chrome.runtime.onMessage.removeListener(messageListener); } catch (_) {}
}

globalThis.__geminiLiveTranslatorContentState = {
  version: "1.0.12",
  dispose: disposeContentScript,
};

function normalize(text) { return (text || "").replace(/\s+/g, " ").trim(); }
function isComplete(text) { return /[。！？!?；;]$/.test(normalize(text)); }
function notifyBackground(message) {
  // A content script can outlive an extension reload on an already-open page.
  // In that brief state Chrome invalidates runtime APIs; closing locally must
  // still succeed without turning it into an uncaught page-console error.
  try {
    if (!chrome?.runtime?.id) return;
    chrome.runtime.sendMessage(message).catch(() => {});
  } catch (_) {}
}
function mergeFragments(previous, incoming) {
  const left = normalize(previous); const right = normalize(incoming);
  if (!left) return right;
  if (!right || left === right || left.endsWith(right)) return left;
  if (right.startsWith(left)) return right;
  const max = Math.min(left.length, right.length, 80);
  for (let size = max; size > 0; size--) {
    if (left.slice(-size) === right.slice(0, size)) return left + right.slice(size);
  }
  return left + right;
}

function ensureSubtitle() {
  if (closedByUser) return;
  if (root) return;
  root = document.createElement("section");
  root.id = "gemini-live-subtitle";
  // Explicitly override the old non-interactive subtitle layer after an
  // extension reload on an already-open page.
  root.style.pointerEvents = "auto";
  root.innerHTML = `<button class="glt-close" type="button" aria-label="停止翻译">×</button><main class="glt-history"><div class="glt-track glt-source"></div><div class="glt-track glt-translation"></div></main><div class="glt-pending"></div>`;
  history = root.querySelector(".glt-history");
  sourceHistory = history.querySelector(".glt-source");
  translationHistory = history.querySelector(".glt-translation");
  pending = root.querySelector(".glt-pending");
  root.querySelector(".glt-close").addEventListener("pointerdown", event => event.stopPropagation());
  root.querySelector(".glt-close").addEventListener("click", event => {
    event.stopPropagation();
    closedByUser = true;
    root?.remove();
    root = history = sourceHistory = translationHistory = pending = null;
    latestInput = latestOutput = lastCommitted = "";
    notifyBackground({ type: "stop" });
  });
  document.documentElement.append(root);
  root.addEventListener("pointerdown", beginPointerAction);
  root.addEventListener("pointermove", updateCursor);
  root.addEventListener("pointerleave", () => { if (!pointerAction) root.style.cursor = ""; });
  root.addEventListener("pointermove", movePointerAction);
  root.addEventListener("pointerup", endPointerAction);
  root.addEventListener("pointercancel", endPointerAction);
}

function applyLayout(layout) {
  if (!root || !layout) return;
  const width = Math.min(Math.max(380, Number(layout.width) || root.getBoundingClientRect().width), window.innerWidth);
  const height = Math.min(Math.max(72, Number(layout.height) || root.getBoundingClientRect().height), window.innerHeight);
  const left = Math.min(Math.max(0, Number(layout.left) || 0), Math.max(0, window.innerWidth - width));
  const top = Math.min(Math.max(0, Number(layout.top) || 0), Math.max(0, window.innerHeight - height));
  root.style.left = `${left}px`;
  root.style.top = `${top}px`;
  root.style.bottom = "auto";
  root.style.transform = "none";
  root.style.width = `${width}px`;
  root.style.height = `${height}px`;
  savedLayout = { left, top, width, height };
}

function currentLayout() {
  const rect = root.getBoundingClientRect();
  return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
}

function setSettings(next) {
  settings = { ...defaults, ...next };
  renderPending();
  if (sourceHistory) sourceHistory.hidden = settings.subtitleLanguage === "translation";
  if (translationHistory) translationHistory.hidden = settings.subtitleLanguage === "source";
}

function makeBlock(source, translation, className = "") {
  const block = document.createElement("article");
  block.className = `glt-block ${className}`.trim();
  block.dataset.source = source;
  block.dataset.translation = translation;
  const sourceLine = document.createElement("div"); sourceLine.className = "glt-source"; sourceLine.textContent = source;
  const translationLine = document.createElement("div"); translationLine.className = "glt-translation"; translationLine.textContent = translation;
  block.append(sourceLine, translationLine);
  applyLanguageVisibility(block);
  return block;
}

function applyLanguageVisibility(block) {
  const language = settings.subtitleLanguage;
  block.querySelector(".glt-source").hidden = language === "translation";
  block.querySelector(".glt-translation").hidden = language === "source";
}

function appendSegment(track, text) {
  if (!text) return;
  const segment = document.createElement("span");
  segment.className = "glt-segment";
  segment.textContent = text;
  track.append(segment);
}

function trackOverflows(track) {
  return !track.hidden && track.scrollWidth > track.clientWidth + 1;
}

function reflowHistory() {
  if (!sourceHistory || !translationHistory) return;
  if (!trackOverflows(sourceHistory) && !trackOverflows(translationHistory)) return;
  const sourceLast = sourceHistory.lastElementChild?.cloneNode(true);
  const translationLast = translationHistory.lastElementChild?.cloneNode(true);
  sourceHistory.replaceChildren();
  translationHistory.replaceChildren();
  if (sourceLast) sourceHistory.append(sourceLast);
  if (translationLast) translationHistory.append(translationLast);
}

function appendHistory(source, translation) {
  const signature = `${source}\n${translation}`;
  if (!signature.trim() || signature === lastCommitted) return;
  lastCommitted = signature;
  const entry = { source, translation };
  historyEntries.push(entry);
  appendSegment(sourceHistory, source);
  appendSegment(translationHistory, translation);
  // The two tracks are deliberately single-line.  Test their horizontal width
  // after appending: if either cannot fit, begin this sentence on fresh tracks
  // rather than letting a final word wrap onto a third visual line.
  if (trackOverflows(sourceHistory) || trackOverflows(translationHistory)) {
    historyEntries = [entry];
    sourceHistory.replaceChildren();
    translationHistory.replaceChildren();
    appendSegment(sourceHistory, source);
    appendSegment(translationHistory, translation);
  }
}

function currentSubtitleState() {
  return {
    history: historyEntries.slice(-20),
    latestInput,
    latestOutput,
    lastCommitted,
  };
}

function notifySubtitleState() {
  notifyBackground({ type: "subtitle-state", state: currentSubtitleState() });
}

function restoreSubtitleState(state) {
  if (!state || !root || !sourceHistory || !translationHistory) return;
  historyEntries = Array.isArray(state.history)
    ? state.history
        .filter(entry => entry && (entry.source || entry.translation))
        .slice(-20)
        .map(entry => ({ source: normalize(entry.source), translation: normalize(entry.translation) }))
    : [];
  sourceHistory.replaceChildren();
  translationHistory.replaceChildren();
  for (const entry of historyEntries) {
    appendSegment(sourceHistory, entry.source);
    appendSegment(translationHistory, entry.translation);
  }
  latestInput = normalize(state.latestInput);
  latestOutput = normalize(state.latestOutput);
  lastCommitted = state.lastCommitted || "";
  reflowHistory();
  renderPending();
}

function commitPending(force = false) {
  if (!latestOutput || (!force && !isComplete(latestOutput))) return;
  appendHistory(latestInput, latestOutput);
  latestInput = "";
  latestOutput = "";
  clearTimeout(pendingCommitTimer);
}

function schedulePendingCommit() {
  clearTimeout(pendingCommitTimer);
  if (!latestOutput) return;
  // Live transcription sometimes pauses without emitting sentence punctuation.
  // Commit the stable fragment after a short quiet period instead of leaving
  // sentence mode visually frozen.
  pendingCommitTimer = setTimeout(() => {
    if (!disposed && !closedByUser) {
      commitPending(true);
      notifySubtitleState();
    }
  }, 1200);
}

function renderPending() {
  if (!pending) return;
  pending.replaceChildren();
  if (settings.displayMode !== "streaming") return;
  if (latestInput || latestOutput) pending.append(makeBlock(latestInput, latestOutput, "glt-live"));
}

function handleSubtitle(message) {
  if (closedByUser) return;
  ensureSubtitle();
  if (message.error) return;
  if (message.input) latestInput = mergeFragments(latestInput, message.input);
  if (message.output) latestOutput = mergeFragments(latestOutput, message.output);
  commitPending();
  schedulePendingCommit();
  renderPending();
  notifySubtitleState();
  notifyBackground({ type: "subtitle-rendered" });
}

function showStatus(text) {
  if (closedByUser) return;
  ensureSubtitle();
  if (!pending) return;
  pending.replaceChildren();
  const status = document.createElement("div");
  status.className = "glt-status";
  status.textContent = normalize(text);
  pending.append(status);
}

function edgeAt(event) {
  const rect = root.getBoundingClientRect(); const gap = 15;
  return { left: event.clientX - rect.left < gap, right: rect.right - event.clientX < gap, top: event.clientY - rect.top < gap, bottom: rect.bottom - event.clientY < gap };
}
function cursorFor(edge) {
  if ((edge.left || edge.right) && (edge.top || edge.bottom)) return "nwse-resize";
  if (edge.left || edge.right) return "ew-resize";
  if (edge.top || edge.bottom) return "ns-resize";
  return "";
}
function updateCursor(event) { if (!pointerAction) root.style.cursor = cursorFor(edgeAt(event)); }
function beginPointerAction(event) {
  if (event.button !== 0) return;
  const edge = edgeAt(event); const rect = root.getBoundingClientRect();
  const resizing = edge.left || edge.right || edge.top || edge.bottom;
  pointerAction = { resizing, edge, startX: event.clientX, startY: event.clientY, left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  root.style.left = `${rect.left}px`; root.style.top = `${rect.top}px`; root.style.transform = "none";
  root.setPointerCapture(event.pointerId); event.preventDefault();
}
function movePointerAction(event) {
  if (!pointerAction) return;
  const action = pointerAction; const dx = event.clientX - action.startX; const dy = event.clientY - action.startY;
  if (!action.resizing) { root.style.left = `${Math.max(0, action.left + dx)}px`; root.style.top = `${Math.max(0, action.top + dy)}px`; return; }
  let { left, top, width, height } = action;
  if (action.edge.right) width += dx;
  if (action.edge.bottom) height += dy;
  if (action.edge.left) { width -= dx; left += dx; }
  if (action.edge.top) { height -= dy; top += dy; }
  if (width < 380) { if (action.edge.left) left -= 380 - width; width = 380; }
  if (height < 72) { if (action.edge.top) top -= 72 - height; height = 72; }
  root.style.left = `${Math.max(0, left)}px`; root.style.top = `${Math.max(0, top)}px`;
  root.style.width = `${width}px`; root.style.height = `${height}px`;
}
function endPointerAction(event) {
  if (!pointerAction) return;
  root.releasePointerCapture?.(event.pointerId);
  const resized = pointerAction.resizing;
  pointerAction = null;
  root.style.cursor = "";
  if (resized) { reflowHistory(); renderPending(); }
  savedLayout = currentLayout();
  notifyBackground({ type: "subtitle-layout", layout: savedLayout });
}

messageListener = message => {
  if (disposed) return;
  if (message.type === "subtitle-start") { closedByUser = false; ensureSubtitle(); }
  if (message.type === "subtitle-stop") { root?.remove(); clearTimeout(pendingCommitTimer); root = history = sourceHistory = translationHistory = pending = null; latestInput = latestOutput = lastCommitted = ""; historyEntries = []; }
  if (message.type === "subtitle-status") showStatus(message.text);
  if (message.type === "subtitle-settings") { if (root) setSettings(message.settings); }
  if (message.type === "subtitle-layout") { savedLayout = message.layout || null; ensureSubtitle(); applyLayout(savedLayout); reflowHistory(); renderPending(); }
  if (message.type === "subtitle-state") { ensureSubtitle(); restoreSubtitleState(message.state); }
  if (message.type === "subtitle") handleSubtitle(message);
};
chrome.runtime.onMessage.addListener(messageListener);
})();
