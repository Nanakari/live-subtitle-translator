const status = document.querySelector("#status");
const displayMode = document.querySelector("#display-mode");
const subtitleLanguage = document.querySelector("#subtitle-language");
function settings() { return { displayMode: displayMode.value, subtitleLanguage: subtitleLanguage.value }; }
async function saveSettings() {
  const value = settings();
  await chrome.storage.local.set({ subtitleSettings: value });
  await chrome.runtime.sendMessage({ type: "settings", settings: value });
}
async function send(type) { await saveSettings(); const result = await chrome.runtime.sendMessage({ type }); status.textContent = result?.ok ? (type === "start" ? "已开始。切回标签页查看字幕。" : "已停止。") : (result?.error || "操作失败。"); }
Promise.all([chrome.storage.local.get("subtitleSettings"), chrome.storage.session.get("lastPluginError")]).then(([{ subtitleSettings }, { lastPluginError }]) => { if (subtitleSettings) { displayMode.value = subtitleSettings.displayMode || "sentence"; subtitleLanguage.value = subtitleSettings.subtitleLanguage || "bilingual"; } if (lastPluginError) status.textContent = lastPluginError; });
displayMode.addEventListener("change", saveSettings); subtitleLanguage.addEventListener("change", saveSettings);
document.querySelector("#start").addEventListener("click", () => send("start")); document.querySelector("#stop").addEventListener("click", () => send("stop"));
