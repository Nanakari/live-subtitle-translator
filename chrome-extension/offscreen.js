let audioContext, source, worklet, stream, socket, reconnectTimer, keepAliveTimer;
let intentionalStop = false;
let lastAudioAt = 0;
let lastPipelineResetAt = 0;
let rebuildingPipeline = false;
let lastRenderAckAt = Date.now();
let lastUiRepairAt = 0;
const TARGET_RATE = 16000;

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
  clearTimeout(reconnectTimer);
  clearInterval(keepAliveTimer);
  worklet?.disconnect();
  source?.disconnect();
  stream?.getTracks().forEach(track => track.stop());
  socket?.close();
  worklet = source = stream = socket = null;
  await audioContext?.close();
  audioContext = null;
}

function sendSilenceKeepAlive() {
  if (intentionalStop) return;
  if (audioContext?.state === "suspended") audioContext.resume().catch(() => {});
  if (socket?.readyState === WebSocket.OPEN && Date.now() - lastAudioAt > 500) {
    socket.send(new Int16Array(TARGET_RATE / 10).buffer);
  }
  if (Date.now() - lastAudioAt > 15000 && Date.now() - lastPipelineResetAt > 15000) {
    rebuildAudioPipeline().catch(() => {});
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
    if (socket?.readyState !== WebSocket.OPEN) return;
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
    await createAudioPipeline();
    lastAudioAt = Date.now();
  } finally {
    rebuildingPipeline = false;
  }
}

function connectBridge() {
  if (intentionalStop) return;
  socket = new WebSocket("ws://127.0.0.1:8765/ws/translate");
  socket.binaryType = "arraybuffer";
  socket.onopen = () => chrome.runtime.sendMessage({ type: "bridge-connected" }).catch(() => {});
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === "translation") {
      chrome.runtime.sendMessage({ type: "subtitle", input: data.input, output: data.output, error: data.error }).catch(() => {});
      if (Date.now() - lastRenderAckAt > 5000 && Date.now() - lastUiRepairAt > 5000) {
        lastUiRepairAt = Date.now();
        chrome.runtime.sendMessage({ type: "repair-subtitle-ui" }).catch(() => {});
      }
    }
    if (data.type === "error") chrome.runtime.sendMessage({ type: "capture-error", message: data.message }).catch(() => {});
  };
  socket.onerror = () => chrome.runtime.sendMessage({ type: "capture-error", message: "无法连接本机翻译服务，正在重连…" }).catch(() => {});
  socket.onclose = () => {
    socket = null;
    if (!intentionalStop) reconnectTimer = setTimeout(connectBridge, 2000);
  };
}

async function start(streamId) {
  await stop();
  intentionalStop = false;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } }, video: false
    });
    stream.getAudioTracks().forEach(track => {
      track.onended = () => chrome.runtime.sendMessage({ type: "capture-error", message: "标签页音频采集已结束，请重新点击“开始翻译”。" }).catch(() => {});
    });
    connectBridge();
    await createAudioPipeline();
    lastAudioAt = Date.now();
    lastRenderAckAt = Date.now();
    keepAliveTimer = setInterval(sendSilenceKeepAlive, 500);
  } catch (error) {
    chrome.runtime.sendMessage({ type: "capture-error", message: `无法捕获此标签页音频：${error.message}` }).catch(() => {});
    await stop();
  }
}

chrome.runtime.onMessage.addListener(message => {
  if (message.type === "start-capture") start(message.streamId).catch(() => {});
  if (message.type === "stop-capture") stop().catch(() => {});
  if (message.type === "subtitle-rendered") lastRenderAckAt = Date.now();
});
