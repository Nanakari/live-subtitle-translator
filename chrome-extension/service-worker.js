const OFFSCREEN_DOCUMENT = "offscreen.html";
let activeTabId = null;
const DEFAULT_SETTINGS = { displayMode: "sentence", subtitleLanguage: "bilingual" };

async function getLiveState() {
  const state = await chrome.storage.session.get(["translationActive", "latestSubtitle", "subtitleState"]);
  return {
    translationActive: Boolean(state.translationActive),
    latestSubtitle: state.latestSubtitle || null,
    subtitleState: state.subtitleState || null,
  };
}

async function setLiveState(translationActive, latestSubtitle = null) {
  await chrome.storage.session.set({ translationActive, latestSubtitle });
}

async function setSubtitleState(state) {
  await chrome.storage.session.set({ subtitleState: state || null });
}

async function setPluginError(message = "") {
  if (message) await chrome.storage.session.set({ lastPluginError: message });
  else await chrome.storage.session.remove("lastPluginError");
}

async function getActiveTabId() {
  if (activeTabId) return activeTabId;
  const stored = await chrome.storage.session.get("activeTabId");
  if (stored.activeTabId) {
    activeTabId = stored.activeTabId;
    return activeTabId;
  }
  // Service workers are suspended by Chrome. Recover the target from the
  // capture API when the worker wakes to forward later subtitle events.
  const captures = await chrome.tabCapture.getCapturedTabs();
  const activeCapture = captures.find(capture => capture.status !== "stopped");
  if (activeCapture?.tabId) {
    activeTabId = activeCapture.tabId;
    await chrome.storage.session.set({ activeTabId });
  }
  return activeTabId;
}

async function clearActiveTab() {
  activeTabId = null;
  await chrome.storage.session.remove("activeTabId");
}

async function ensureOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (!contexts.length) {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_DOCUMENT,
      reasons: ["USER_MEDIA"],
      justification: "Capture the user-selected tab's audio for real-time translation."
    });
  }
}

async function tellTab(tabId, message) {
  try { await chrome.tabs.sendMessage(tabId, message); } catch (_) { /* Chrome internal pages cannot host subtitles. */ }
}

async function prepareTab(tabId) {
  const target = { tabId };
  await chrome.scripting.insertCSS({ target, files: ["subtitle.css"] });
  await chrome.scripting.executeScript({ target, files: ["content.js"] });
}

async function getSubtitleLayout() {
  const { subtitleLayout } = await chrome.storage.local.get("subtitleLayout");
  return subtitleLayout || null;
}

function supportsSubtitleOverlay(tab) {
  return Boolean(tab?.id && /^https?:\/\//i.test(tab.url || ""));
}

async function showSubtitleInTab(tabId, latestSubtitle = null, subtitleState = null) {
  try {
    await prepareTab(tabId);
  } catch (_) {
    return;
  }
  const { subtitleSettings = DEFAULT_SETTINGS } = await chrome.storage.local.get("subtitleSettings");
  const subtitleLayout = await getSubtitleLayout();
  await tellTab(tabId, { type: "subtitle-start" });
  await tellTab(tabId, { type: "subtitle-settings", settings: subtitleSettings });
  if (subtitleLayout) await tellTab(tabId, { type: "subtitle-layout", layout: subtitleLayout });
  if (subtitleState) await tellTab(tabId, { type: "subtitle-state", state: subtitleState });
  else if (latestSubtitle) await tellTab(tabId, latestSubtitle);
}

async function showSubtitleInAllTabs() {
  const { translationActive, latestSubtitle, subtitleState } = await getLiveState();
  if (!translationActive) return;
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs.filter(supportsSubtitleOverlay).map(tab => showSubtitleInTab(tab.id, latestSubtitle, subtitleState)));
}

async function broadcastToSubtitleTabs(message) {
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs.filter(supportsSubtitleOverlay).map(tab => tellTab(tab.id, message)));
}

async function repairSubtitleUi() {
  await showSubtitleInAllTabs();
}

async function stopCaptureAndWait(tabId) {
  // Offscreen owns the actual MediaStream, timers, AudioContext, and bridge
  // socket. Always stop it even if Chrome no longer reports a captured tab.
  if (!tabId) {
    chrome.runtime.sendMessage({ type: "stop-capture" }).catch(() => {});
    return;
  }
  const captures = await chrome.tabCapture.getCapturedTabs();
  const isActive = captures.some(capture => capture.tabId === tabId && capture.status !== "stopped");
  if (!isActive) {
    chrome.runtime.sendMessage({ type: "stop-capture" }).catch(() => {});
    return;
  }
  await new Promise(resolve => {
    const timeout = setTimeout(done, 2500);
    function done() {
      clearTimeout(timeout);
      chrome.tabCapture.onStatusChanged.removeListener(onStatusChanged);
      resolve();
    }
    function onStatusChanged(info) {
      if (info.tabId === tabId && info.status === "stopped") done();
    }
    chrome.tabCapture.onStatusChanged.addListener(onStatusChanged);
    chrome.runtime.sendMessage({ type: "stop-capture" }).catch(() => done());
  });
  for (let attempt = 0; attempt < 10; attempt++) {
    const remaining = await chrome.tabCapture.getCapturedTabs();
    if (!remaining.some(capture => capture.tabId === tabId && capture.status !== "stopped")) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("旧的标签页音频流仍在释放，请稍后再试。");
}

async function restoreSubtitleAfterNavigation(tabId) {
  const { translationActive, latestSubtitle, subtitleState } = await getLiveState();
  if (!translationActive) return;
  const tab = await chrome.tabs.get(tabId);
  if (supportsSubtitleOverlay(tab)) await showSubtitleInTab(tabId, latestSubtitle, subtitleState);
}

chrome.tabs.onActivated.addListener(({ tabId }) => {
  restoreSubtitleAfterNavigation(tabId).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "complete") restoreSubtitleAfterNavigation(tabId).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // These are one-way notifications from the offscreen document. Returning
  // true here would make Chrome wait for a response that will never arrive.
  if (message.type === "start-capture" || message.type === "stop-capture") return;
  if (message.type === "subtitle") {
    // Ignore a final WebSocket packet that races with an explicit Stop;
    // otherwise it could recreate overlays in every tab after shutdown.
    getLiveState()
      .then(state => state.translationActive ? setLiveState(true, message).then(() => broadcastToSubtitleTabs(message)) : null)
      .catch(() => {});
    return;
  }
  if (message.type === "subtitle-state") {
    getLiveState()
      .then(state => state.translationActive ? setSubtitleState(message.state) : null)
      .catch(() => {});
    return;
  }
  if (message.type === "capture-error") {
    setPluginError(message.message)
      .then(() => broadcastToSubtitleTabs({ type: "subtitle-status", text: message.message }))
      .catch(() => {});
    return;
  }
  if (message.type === "subtitle-rendered") return;
  if (message.type === "subtitle-layout") {
    chrome.storage.local.set({ subtitleLayout: message.layout })
      .then(() => broadcastToSubtitleTabs({ type: "subtitle-layout", layout: message.layout }))
      .catch(() => {});
    return;
  }
  if (message.type === "direct-diagnostics") {
    chrome.storage.session.set({ directDiagnostics: message.diagnostics }).catch(() => {});
    return;
  }
  if (message.type === "repair-subtitle-ui") {
    repairSubtitleUi().catch(() => {});
    return;
  }
  if (message.type === "bridge-connected" || message.type === "gemini-connected") {
    setPluginError("").then(() => repairSubtitleUi()).catch(() => {});
    return;
  }

  (async () => {
    if (message.type === "start") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) throw new Error("没有找到当前标签页。");
      await setPluginError("");
      await chrome.storage.session.remove(["directDiagnostics", "lastPluginError", "subtitleState"]);
      const previousTabId = await getActiveTabId();
      if (previousTabId) {
        await stopCaptureAndWait(previousTabId);
        await broadcastToSubtitleTabs({ type: "subtitle-stop" });
      }
      await ensureOffscreenDocument();
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
      activeTabId = tab.id;
      await chrome.storage.session.set({ activeTabId });
      await setLiveState(true);
      await showSubtitleInAllTabs();
      // Offscreen documents do not send a response to this one-way command.
      // Do not await it, otherwise the service worker can wait on its own listener.
      chrome.runtime.sendMessage({ type: "start-capture", streamId }).catch(() => {});
      sendResponse({ ok: true });
    } else if (message.type === "stop") {
      const captureTabId = await getActiveTabId();
      await setLiveState(false);
      await chrome.storage.session.remove("subtitleState");
      try { await stopCaptureAndWait(captureTabId); } catch (_) { /* The UI must still close. */ }
      // Remove every visible overlay, including overlays created in tabs the
      // user navigated to after starting the translation.
      await broadcastToSubtitleTabs({ type: "subtitle-stop" });
      await clearActiveTab();
      sendResponse({ ok: true });
    } else if (message.type === "settings") {
      await chrome.storage.local.set({ subtitleSettings: { ...DEFAULT_SETTINGS, ...message.settings } });
      const { translationActive } = await getLiveState();
      if (translationActive) await broadcastToSubtitleTabs({ type: "subtitle-settings", settings: message.settings });
      sendResponse({ ok: true });
    }
  })().catch(error => sendResponse({ ok: false, error: error.message }));
  return true;
});
