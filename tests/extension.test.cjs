const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../chrome-extension', name), 'utf8');

test('right channel speech is included and playback stays stereo', () => {
  let Processor, sent;
  vm.runInNewContext(source('pcm-processor.js'), {
    sampleRate: 16000, Float32Array,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: b => sent = new Float32Array(b) }; } },
    registerProcessor: (_, cls) => Processor = cls,
  });
  const output = [new Float32Array(1600), new Float32Array(1600)];
  new Processor().process([[new Float32Array(1600), new Float32Array(1600).fill(0.5)]], [output]);
  assert.equal(Math.max(...sent), 0.25);
  assert.equal(output[0][0], 0);
  assert.equal(output[1][0], 0.5);
});

function offscreenContext() {
  const sockets = [], messages = [], timers = new Map();
  let timerId = 0;
  class Socket {
    static OPEN = 1;
    constructor() { this.readyState = 1; this.bufferedAmount = 0; sockets.push(this); }
    close() { this.closed = true; }
    send(data) { this.sent = data; }
    ready() { this.onmessage({ data: JSON.stringify({ type: 'status', status: 'gemini-connected' }) }); }
  }
  const context = vm.createContext({
    WebSocket: Socket, Date, Float32Array, Int16Array,
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: fn => { timers.set(++timerId, fn); return timerId; },
    clearInterval: id => timers.delete(id),
    chrome: { runtime: { getURL: x => x, sendMessage: async m => messages.push(m), onMessage: { addListener() {} } } },
  });
  vm.runInContext(source('offscreen.js'), context);
  return { context, sockets, messages, timers };
}

test('a late close from an old socket cannot clear the replacement', async () => {
  const { context, sockets, timers } = offscreenContext();
  const first = context.connectBridge(); sockets[0].ready(); await first;
  const second = context.connectBridge(); sockets[1].ready(); await second;
  sockets[0].onclose();
  assert.equal(vm.runInContext('socket', context), sockets[1]);
  assert.equal(vm.runInContext('bridgeReady', context), true);
  assert.equal(timers.size, 0);
  await context.stop();
  sockets[1].onclose();
  assert.equal(timers.size, 0);
});

test('ended capture releases audio, websocket and keepalive timers', async () => {
  const { context, sockets, messages, timers } = offscreenContext();
  const track = { stop() { this.stopped = true; } };
  const media = { active: true, getTracks: () => [track], getAudioTracks: () => [track] };
  const node = { connect() {}, disconnect() {} };
  let closed = 0;
  context.navigator = { mediaDevices: { getUserMedia: async () => media } };
  context.AudioContext = class {
    constructor() { this.audioWorklet = { addModule: async () => {} }; this.sampleRate = 16000; }
    createMediaStreamSource() { return node; }
    async resume() {}
    async close() { closed++; }
  };
  context.AudioWorkletNode = class { constructor() { this.port = {}; } connect() {} disconnect() {} };
  const started = context.start('test-stream');
  for (let i = 0; i < 20 && !sockets.length; i++) await Promise.resolve();
  sockets[0].ready(); await started;
  track.onended();
  await vm.runInContext('lifecycle', context);
  assert.equal(track.stopped, true);
  assert.equal(sockets[0].closed, true);
  assert.equal(timers.size, 0);
  assert.equal(closed, 1);
  assert.ok(messages.some(m => m.type === 'capture-ended'));
});

test('subtitle content targets captured tab unless all-tabs is explicitly selected', async () => {
  const sent = [], injected = [];
  const state = { activeTabId: 1, translationActive: true };
  const local = { subtitleSettings: { displayMode: 'sentence', subtitleLanguage: 'bilingual' } };
  const event = { addListener() {} };
  const storage = data => ({ get: async () => data, set: async x => Object.assign(data, x), remove: async () => {} });
  const context = vm.createContext({
    chrome: {
      storage: { session: storage(state), local: storage(local) },
      runtime: { onMessage: event },
      tabs: { query: async () => [{ id: 1, url: 'https://one.test' }, { id: 2, url: 'https://two.test' }],
        sendMessage: async (id, message) => sent.push([id, message.type]), onActivated: event, onUpdated: event },
      scripting: { insertCSS: async ({ target }) => injected.push(target.tabId), executeScript: async () => {} },
    },
  });
  vm.runInContext(source('service-worker.js'), context);
  await context.showSubtitleInAllTabs();
  assert.deepEqual(injected, [1]);
  sent.length = 0;
  await context.broadcastToSubtitleTabs({ type: 'subtitle', output: 'private speech' });
  assert.deepEqual(sent, [[1, 'subtitle']]);
  local.subtitleSettings.overlayScope = 'all';
  sent.length = 0;
  await context.broadcastToSubtitleTabs({ type: 'subtitle' });
  assert.deepEqual(sent, [[1, 'subtitle'], [2, 'subtitle']]);
  local.subtitleSettings.overlayScope = 'captured';
  sent.length = 0;
  await context.broadcastToSubtitleTabs({ type: 'subtitle-stop' });
  assert.deepEqual(sent, [[1, 'subtitle-stop'], [2, 'subtitle-stop']]);
});
