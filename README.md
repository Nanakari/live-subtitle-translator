# Live Subtitle Translator

[![CI](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml)

A Windows desktop live-subtitle translator. It captures system playback audio,
translates it through Gemini Live, and shows an always-on-top bilingual subtitle
window. A Chrome extension is also included for translating the audio of the
current browser tab.

## Requirements

- Windows 10 or later
- Python 3.9 or later
- A Gemini API key with access to the configured Live model

## Quick start: desktop app

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
- `bridge.allowed_extension_ids`: optional allowlist for unpacked Chrome
  extension IDs. Setting it prevents other browser extensions and ordinary
  webpages from opening translation sessions through the local bridge.
- `subtitle.layout_style`: `compact` (default) or `classic`.
- `subtitle.always_on_top`: keeps the subtitle window above other windows.

## Chrome extension

1. Start the local bridge by double-clicking `start_chrome_bridge_hidden.vbs`.
2. Open `chrome://extensions`, enable Developer mode, and choose **Load
   unpacked**.
3. Select the `chrome-extension` directory.
4. Open a page to translate, click the extension icon, and start translation.

The extension captures the current tab's audio. Its overlay is limited to the
captured tab by default. Choose **所有网页标签页** in the popup to explicitly
show it across web tabs; this puts subtitle text into those pages. Use the desktop
app when subtitles must stay visible across other applications. Closing the
captured tab stops its audio stream, bridge connection, and subtitle overlays.

## Subtitle responsiveness and diagnostics

- Sentence mode favors complete sentences. `subtitle.pair_commit_delay_ms`
  defaults to 600 ms for source/translation pairing.
- `subtitle.max_short_carry_ms` defaults to 1800 ms, measured from the first
  buffered short phrase; further fragments do not restart the deadline.
- `subtitle.stable_max_wait_ms` is a 6500 ms soft deadline: after it, a long
  unfinished translation may be released at a comma/clause boundary. A fragment
  without a suitable boundary continues waiting for its continuation.
- A pause of `subtitle.stable_pause_ms` (1800 ms) can release stable text early,
  unless a simple suffix heuristic considers it unfinished. These rules use
  punctuation and timing, not a separate semantic model.
- `subtitle.stable_hard_max_wait_ms` (12000 ms) bounds waiting when a sentence
  never finishes. `subtitle.max_pending_chars` (180) also bounds buffered text.
  Complete sentences can display sooner; this is not a fixed 12-second delay.
- Timeout prefixes and already-arrived continuations are paired together. If
  source/translation clause boundaries are uncertain, unfinished translations
  temporarily share source context (bounded by the hard deadline) rather than
  consuming the source before the continuation arrives. This is approximate
  alignment, not word-level alignment supplied by Gemini.
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

Configuration changes apply after restarting the desktop app or bridge.
Reload the unpacked extension to apply JavaScript changes. Existing values in
`config.local.yaml` take precedence over the shared defaults.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py chrome_server.py scripts src
.\.venv\Scripts\python.exe -m unittest discover -v
node --test tests/extension.test.cjs
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
