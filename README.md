# Live Subtitle Translator

[![CI](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml)

A Windows desktop live-subtitle translator. It captures system playback audio,
translates it through Gemini Live, and shows an always-on-top bilingual subtitle
window.

## Requirements

- Windows 10 or later
- Python 3.10 or later
- A Gemini API key with access to the configured Live model

## Quick start: desktop app

For Windows x64, download the portable ZIP from
[Releases](https://github.com/Nanakari/live-subtitle-translator/releases/latest).
Extract the whole folder and follow `PORTABLE-README.txt`. The portable build
includes Python; configure your own Gemini API key before starting the executable.
The executable is unsigned.

To run from source:

```powershell
git clone https://github.com/Nanakari/live-subtitle-translator.git
cd live-subtitle-translator
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Set your API key for the current PowerShell session:

```powershell
$env:GEMINI_API_KEY="your-api-key"
```

Then either run the app directly:

```powershell
python app.py
```

Or double-click `start_hidden.vbs` to launch it without a visible command
prompt. The compact two-line subtitle window is the default style and remains
on top of other windows.

Desktop controls:

- Press `Ctrl+Alt+Space` anywhere in Windows to pause or resume translation.
- Right-click the subtitle window to select a playback device or change subtitle options.
- Use the system tray icon to pause, show or hide subtitles, or exit.
- Window geometry, subtitle options, and the selected playback device are restored next time.
- Starting the app again shows a notice instead of opening a second desktop copy.
- After 20 seconds of silence, the desktop app disconnects Gemini and keeps
  listening locally. Playback reconnects Gemini automatically.
- The approximately 10-minute Live WebSocket rollover is handled proactively
  with session resumption, so it does not surface as a `1008` subtitle error.

## Configuration

`config.yaml` contains safe, shared defaults and is committed to the
repository. It contains no API key and uses no proxy by default.

For machine-specific settings, copy `config.local.example.yaml` to
`config.local.yaml`. The local file is ignored by Git and overrides matching
values in `config.yaml`.

```powershell
Copy-Item config.local.example.yaml config.local.yaml
```

Use `config.local.yaml` only for private values such as an API key or local
proxy. Do not commit, share, or paste those values into issues.

Key settings:

- `gemini.api_key_env`: environment-variable name for the API key; defaults to
  `GEMINI_API_KEY`.
- `network.proxy_url`: optional proxy URL. Keep it empty for a direct
  connection.
- `subtitle.layout_style`: `compact` (default) or `classic`.
- `subtitle.always_on_top`: keeps the subtitle window above other windows.
- `audio.channels`: `null` selects the playback device's native channel count.
  Audio is captured with at least two channels and downmixed to mono before
  sending to Gemini, avoiding SoundCard's Windows single-channel recording issue.
  Existing local `channels: 1` settings also select native multichannel capture.
  A mono-only playback device produces an actionable error; select a stereo
  device instead. An explicit value of 2 or more selects that many channels,
  provided the playback device supports them.
- `gemini.connect_timeout_seconds`: maximum wait for the WebSocket connection
  and Gemini setup response (15 seconds by default).
- `gemini.send_timeout_seconds`: maximum wait for an audio send (5 seconds by
  default); a timeout drops the connection so automatic reconnection can recover.
- `gemini.cleanup_timeout_seconds`: timeout for each asynchronous socket/client
  cleanup operation (2 seconds by default). These timeouts must be finite and
  positive. Cancellation cleanup may take additional time after an I/O timeout.

Pause and exit cancel in-flight connection/send tasks before closing the session.
Resume waits for the previous disconnect to finish before starting a new session.

Connection recovery distinguishes permanent request/authentication errors from
network failures. HTTP 400/401/403/404 stop translation and show the error; HTTP
408/429 and server errors remain retryable. This policy also applies after an
established connection fails. An explicitly rejected session-resumption handle
is discarded and tried once as a fresh session. Network outages retain a valid
handle; authentication failures do not trigger a fresh-session fallback.

Subtitle and translation-output watchdogs require evidence that a translation
is expected. With `echo_target_language: false`, target-language input and input
without language metadata do not arm these timers. The pinned SDK can omit that
metadata, so output-only stall detection is intentionally conservative; connection,
send, and server-data timeouts remain active. Enabling echo or receiving a confirmed
non-target-language code allows the content watchdogs to detect stalled output.

## Subtitle responsiveness and diagnostics

- Sentence mode favors complete sentences. `subtitle.pair_commit_delay_ms`
  defaults to 300 ms for source/translation pairing.
- `subtitle.max_short_carry_ms` defaults to 1000 ms, measured from the first
  buffered short phrase; further fragments do not restart the deadline.
- `subtitle.stable_max_wait_ms` is a 4000 ms soft deadline: after it, a long
  unfinished translation may be released at a comma/clause boundary. A fragment
  without a suitable boundary continues waiting for its continuation.
- A pause of `subtitle.stable_pause_ms` (1000 ms) can release stable text early,
  unless a simple suffix heuristic considers it unfinished. These rules use
  punctuation and timing, not a separate semantic model.
- Timeout-split fragments use an additional `subtitle.timeout_commit_grace_ms`
  (700 ms by default) before display so a near-term continuation can merge with
  them. Complete sentences keep the normal pair delay, and `turn_complete`
  bypasses the grace window.
- `subtitle.stable_hard_max_wait_ms` (8000 ms) bounds waiting when a sentence
  never finishes. `subtitle.max_pending_chars` (140) also bounds buffered text.
  Complete sentences can display sooner; this is not a fixed 12-second delay.
- Timeout prefixes and already-arrived continuations are paired together. If
  source/translation clause boundaries are uncertain, unfinished translations
  temporarily share source context (bounded by the hard deadline) rather than
  consuming the source before the continuation arrives. This is approximate
  alignment, not word-level alignment supplied by Gemini.
- A source block whose length is implausibly large for the current translation
  is treated as stale context and hidden for that block; the translation is
  still shown. Source pairing is reset at each `turn_complete` boundary.
  A fresh Gemini session clears pending source/translation fragments, short-phrase
  carry, displayed history and dedup state. Successful resumption of the same
  session preserves that state. Session IDs on subtitle packets ensure stale
  packets cannot contaminate a new session, even if the UI queue drops a boundary
  notification under load.
- Standalone hesitation fragments wait for continuation and expire at the hard
  deadline. Meaningful text retains a bounded display deadline; a dangling tail
  after a comma is kept for the next fragment when possible.
- Session-close logs distinguish shutdown, intentional disconnect, server
  rotation, send/receive errors, and watchdog expiry. Desktop sleep logs also
  distinguish user pause from detected silence.
- Source fragments retain English word spaces and sentence periods. New source
  sentences can repeat earlier topics without losing their prefix. Compact
  single-line display still crops long text; use the classic layout when the
  entire source/translation sentence needs to stay visible and wrap.
- `audio.silence_preroll_ms` defaults to 500 ms. The desktop keeps a bounded local
  buffer while asleep and sends it when sound wakes translation, including the
  first chunks used to detect that sound.
- Set `app.log_latency_metrics: true` temporarily for content-free monotonic
  timestamps at audio send completion, transcription reception, UI enqueue,
  sentence enqueue, and UI update. These identify local waiting but do not map a
  model response to a specific audio chunk or measure physical screen refresh.
  Detailed timing is disabled by default because audio-stage logs are frequent.

Configuration changes apply after restarting the desktop app. Existing values in
`config.local.yaml` take precedence over the shared defaults.

Original only commits source text independently of translation: punctuation,
turn completion, or the configured stable deadline can publish it even when
Gemini suppresses target-language output. Switching into or out of Original only
clears unfinished pairing state while retaining displayed history.

Translation packets are appended as text deltas, preserving word-boundary
whitespace and repeated letters. Committed utterances are not discarded just
because they repeat or contain an earlier sentence; the stream does not provide
a segment ID that would reliably distinguish a replay from real repetition.

Both `gemini.log_transcriptions` and `subtitle.log_rendered_subtitles` default to
false. They independently enable transcript and subtitle/pairing text in logs;
local overrides can still enable either. Log formatting redacts registered API
keys and recognized key formats from messages, exception chains and stack text.
Failure to save window settings during exit is logged and does not prevent
stopping the translator or destroying the window.

## Development checks

Build a Windows x64 portable package on Windows with
`python -m pip install -r requirements-build.txt`, then
`python scripts/build_windows.py --version v0.1.0`. The builder runs an offline
smoke test of the frozen executable and writes the ZIP and SHA-256 checksum to
`dist/`. It copies only the public configuration and documentation into the package.
Use a standard CPython environment for release builds. The manually triggered
`Windows portable build` GitHub Actions workflow uses Windows x64 and Python 3.12,
runs the tests, and uploads the verified package as a workflow artifact.

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py scripts src
.\.venv\Scripts\python.exe -m unittest discover -v
```

## Security

- Never commit `config.local.yaml`, `.env`, API keys, audio recordings, logs,
  or the `.venv` directory.
- If a key is exposed, revoke it in the provider console and create a new one.
- Report vulnerabilities and sensitive findings according to
  [SECURITY.md](SECURITY.md), not through a public issue.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and the checks
required before opening a pull request.

## License

No open-source license is currently granted for this repository. Public
visibility does not by itself grant permission to copy, modify, or distribute
the code.
