# Live Subtitle Translator

[![CI](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml)

A Windows desktop live-subtitle translator. It captures system playback audio,
translates it through Gemini Live, and shows an always-on-top bilingual subtitle
window.

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
- `subtitle.layout_style`: `compact` (default) or `classic`.
- `subtitle.always_on_top`: keeps the subtitle window above other windows.

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
  still shown. Source pairing is reset at each `turn_complete` boundary and
  when a Gemini session reconnects, so the next turn cannot inherit the
  previous turn's source buffer.
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

## Development checks

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
