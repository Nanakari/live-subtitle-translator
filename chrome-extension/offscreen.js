let audioContext, source, worklet, stream, socket, reconnectTimer, keepAliveTimer;
let intentionalStop = false;
let lastAudioAt = 0;
let lastPipelineResetAt = 0;
let rebuildingPipeline = false;
let lastRenderAckAt = Date.now();
let lastUiRepairAt = 0;
let bridgeReady = false;
let lifecycle = Promise.resolve();
let bridgeGeneration = 0;
const MAX_BUFFERED_AUDIO_BYTES = 16000 * 2;
const TARGET_RATE = 16000;
const BRIDGE_READY_TIMEOUT_MS = 30000;

function floatToPcm16(input) {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) output[i] = Math.max(-1, Math.min(1, input[i])) * 0x7fff;
  return output.buffer;
}

function resample(input, inputRate) {
  if (inputRate === TARGET_RATE) return input;
  const length = Math.round(input.length * TARGET_RATE / inputRate);
  const output = new Float32Array(length);
  const ratio = inputRate / TARGET_RATE;
  for (let i = 0; i < length; i++) {
    const position = i * ratio, low = Math.floor(position), high = Math.min(low + 1, input.length - 1);
    output[i] = input[low] + (input[high] - input[low]) * (position - low);
  }
  return output;
}

async function stop() {
  intentionalStop = true;
  bridgeGeneration++;
  bridgeReady = false;
  clearTimeout(reconnectTimer);
  clearInterval(keepAliveTimer);
  worklet?.disconnect();
  source?.disconnect();
  stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
  socket?.close();
  worklet = source = stream = socket = null;
  await audioContext?.close();
  audioContext = null;
}

function sendSilenceKeepAlive() {
  if (intentionalStop) return;
  if (audioContext?.state === "suspended") audioContext.resume().catch(() => {});
  if (bridgeReady && socket?.readyState === WebSocket.OPEN && socket.bufferedAmount < MAX_BUFFERED_AUDIO_BYTES && Date.now() - lastAudioAt > 500) {
    socket.send(new Int16Array(TARGET_RATE / 10).buffer);
  }
  if (Date.now() - lastAudioAt > 15000 && Date.now() - lastPipelineResetAt > 15000) {
    enqueueLifecycle(rebuildAudioPipeline).catch(() => {});
  }
}

async function createAudioPipeline() {
  audioContext = new AudioContext();
  audioContext.onstatechange = () => {
    if (!intentionalStop && audioContext?.state === "suspended") audioContext.resume().catch(() => {});
  };
  await audioContext.audioWorklet.addModule(chrome.runtime.getURL("pcm-processor.js"));
  source = audioContext.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(audioContext, "pcm-processor");
  worklet.port.onmessage = event => {
    lastAudioAt = Date.now();
    if (!bridgeReady || socket?.readyState !== WebSocket.OPEN || socket.bufferedAmount >= MAX_BUFFERED_AUDIO_BYTES) return;
    const mono = new Float32Array(event.data);
    socket.send(floatToPcm16(resample(mono, audioContext.sampleRate)));
  };
  source.connect(worklet);
  worklet.connect(audioContext.destination);
  await audioContext.resume();
}

async function rebuildAudioPipeline() {
  if (intentionalStop || rebuildingPipeline || !stream?.active) return;
  rebuildingPipeline = true;
  lastPipelineResetAt = Date.now();
  try {
    worklet?.disconnect();
    source?.disconnect();
    await audioContext?.close();
    worklet = source = audioContext = null;
    if (intentionalStop || !stream?.active) return;
    await createAudioPipeline();
    lastAudioAt = Date.now();
  } finally {
    rebuildingPipeline = false;
  }
}

function connectBridge() {
  if (intentionalStop) return Promise.reject(new Error("翻译已停止。"));
  bridgeReady = false;
  const generation = ++bridgeGeneration;
  const connection = new WebSocket("ws://127.0.0.1:8765/ws/translate");
  socket = connection;
  const isCurrent = () => generation === bridgeGeneration && socket === connection;
  return new Promise((resolve, reject) => {
    let readySettled = false;
    const readyTimeout = setTimeout(() => {
      if (readySettled) return;
      readySettled = true;
      reject(new Error("本机服务已连接，但 Gemini 在 30 秒内没有就绪。"));
      connection.close();
    }, BRIDGE_READY_TIMEOUT_MS);

    connection.binaryType = "arraybuffer";
    connection.onopen = () => { if (isCurrent()) chrome.runtime.sendMessage({ type: "bridge-connected" }).catch(() => {}); };
    connection.onmessage = event => {
      if (!isCurrent()) return;
      const data = JSON.parse(event.data);
      if (data.type === "status" && data.status === "gemini-connected") {
        bridgeReady = true;
        clearTimeout(readyTimeout);
        if (!readySettled) {
          readySettled = true;
          resolve();
        }
        chrome.runtime.sendMessage({ type: "gemini-connected" }).catch(() => {});
        return;
      }
      if (data.type === "translation") {
        if (data.error) {
          chrome.runtime.sendMessage({ type: "capture-error", message: data.error }).catch(() => {});
          return;
        }
        chrome.runtime.sendMessage({ type: "subtitle", input: data.input, output: data.output }).catch(() => {});
        if (Date.now() - lastRenderAckAt > 5000 && Date.now() - lastUiRepairAt > 5000) {
          lastUiRepairAt = Date.now();
          chrome.runtime.sendMessage({ type: "repair-subtitle-ui" }).catch(() => {});
        }
      }
      if (data.type === "error") {
        chrome.runtime.sendMessage({ type: "capture-error", message: data.message }).catch(() => {});
      }
    };
    connection.onerror = () => {
      if (!isCurrent()) return;
      chrome.runtime.sendMessage({ type: "capture-error", message: "无法连接本机翻译服务。" }).catch(() => {});
      if (!readySettled) {
        readySettled = true;
        clearTimeout(readyTimeout);
        reject(new Error("无法连接本机翻译服务。"));
      }
    };
    connection.onclose = () => {
      const current = isCurrent();
      if (current) { bridgeReady = false; socket = null; }
      clearTimeout(readyTimeout);
      if (!readySettled) {
        readySettled = true;
        reject(new Error("本机翻译服务在 Gemini 就绪前断开。"));
      }
      if (current && !intentionalStop) {
        reconnectTimer = setTimeout(() => connectBridge().catch(() => {}), 2000);
      }
    };
  });
}

async function start(streamId) {
  await stop();
  intentionalStop = false;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } }, video: false
    });
    stream.getAudioTracks().forEach(track => {
      track.onended = () => {
        if (intentionalStop) return;
        chrome.runtime.sendMessage({ type: "capture-ended", message: "标签页音频采集已结束。" }).catch(() => {});
        enqueueLifecycle(stop).catch(() => {});
      };
    });
    await createAudioPipeline();
    await connectBridge();
    lastAudioAt = Date.now();
    lastRenderAckAt = Date.now();
    keepAliveTimer = setInterval(sendSilenceKeepAlive, 500);
  } catch (error) {
    chrome.runtime.sendMessage({ type: "capture-error", message: `无法捕获此标签页音频：${error.message}` }).catch(() => {});
    await stop();
    throw error;
  }
}

function enqueueLifecycle(action) {
  const result = lifecycle.then(action);
  lifecycle = result.catch(() => {});
  return result;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "start-capture") {
    enqueueLifecycle(() => start(message.streamId))
      .then(() => sendResponse({ ok: true }))
      .catch(error => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "stop-capture") {
    enqueueLifecycle(stop)
      .then(() => sendResponse({ ok: true }))
      .catch(error => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "subtitle-rendered") lastRenderAckAt = Date.now();
});
