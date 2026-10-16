# User guide

## Desktop controls

- Press `Ctrl+Alt+Space` anywhere in Windows to pause or resume translation.
- Right-click the subtitle window to select a playback device or change subtitle options.
- Use the system tray icon to pause, show or hide subtitles, or exit.
- Window geometry, subtitle options, and the selected playback device are restored next time.
- Starting the app again shows a notice instead of opening a second desktop copy.
- After 20 seconds of silence, the desktop app disconnects Gemini and keeps
  listening locally. Playback reconnects Gemini automatically.
- The approximately 10-minute Live WebSocket rollover is handled proactively
  with session resumption, so it does not surface as a `1008` subtitle error.

Manual pause drops queued audio and closes the capture recorder. Resuming opens a
fresh recorder; audio from before or during the pause is not replayed. Automatic
silence sleep keeps its short local preroll so the next phrase starts intact.

To request a graceful exit from the application's folder, run `stop.bat` or
`powershell -NoProfile -File .\stop.ps1`. If the desktop is unresponsive, use
`powershell -NoProfile -File .\stop.ps1 -Force`. The script verifies the recorded
PID, executable, creation time and application folder before signalling or
terminating that desktop process. Other Python, test and build processes are
not selected by their location. Instances started before this stop protocol was
introduced must be closed through their window or tray.

## Configuration and migration

Shared defaults live in `src/live_subtitle_translator/defaults.yaml` in a source
checkout and are included in the installed package. Copy `config.local.example.yaml` to
`config.local.yaml` for private overrides. Files are read in this order:

1. Bundled defaults.
2. An existing legacy `config.yaml`, if present in the application folder.
3. `config.local.yaml` in that folder.

The application reads existing files without rewriting them. Old `config.yaml`
files remain supported, but new installations only need `config.local.yaml`.
The retired options `gemini.source_language`, `subtitle.max_chars` and
`subtitle.dedup` are ignored with a warning naming the option. They never
controlled the Live model or current subtitle pipeline. Remove them from
your own override file when convenient. Source language remains model-detected.

The application folder is the repository root when running from a checkout, or
the executable's folder for a portable build. A separately installed wheel uses
`%LOCALAPPDATA%/LiveSubtitleTranslator` on Windows. The current working directory
does not determine configuration, settings or log locations.

Key settings:

- `gemini.api_key_env`: environment-variable name for the API key; defaults to
  `GEMINI_API_KEY`.
- `network.proxy_url`: optional proxy URL. Keep it empty for a direct
  connection, even if the process or operating system has a proxy configured.
  A configured URL takes precedence over inherited proxy variables and respects
  `network.no_proxy`.
- `app.error_display_delay_seconds`: show a recoverable connection error only
  after it has persisted for 10 seconds (default). Short outages are logged and
  retried without replacing subtitles. Repeated failures share the same deadline;
  recovery clears the notice even before new subtitles arrive. Pause and silence
  sleep cancel any pending notice. Set to 0 for immediate notices. Permanent
  errors and failures with automatic reconnection disabled still show immediately.
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
Holding the pause hotkey does not repeatedly toggle pause and resume. Audio-device
failures stop translation and display an immediate status in every language mode.

Connection recovery distinguishes permanent request/authentication errors from
network failures. HTTP 400/401/403/404 stop translation and show the error; HTTP
408/429 and server errors remain retryable. This policy also applies after an
established connection fails. An explicitly rejected session-resumption handle
is discarded and tried once as a fresh session. Network outages retain a valid
handle; authentication failures do not trigger a fresh-session fallback.
Normal WebSocket closures, including a Close frame without a status code, remain
eligible for automatic reconnection.

Subtitle and translation-output watchdogs require evidence that a translation
is expected. With `echo_target_language: false`, target-language input and input
without language metadata do not arm these timers. The pinned SDK can omit that
metadata, so output-only stall detection is intentionally conservative; connection,
send, and server-data timeouts remain active. Enabling echo or receiving a confirmed
non-target-language code allows the content watchdogs to detect stalled output.

## Subtitle responsiveness and diagnostics

- Sentence mode favors complete sentences. `subtitle.pair_commit_delay_ms`
  defaults to 300 ms for source/translation pairing.
- Compact streaming keeps completed text visible while it waits for pairing or
  short-phrase merging. Sentence boundaries use a wider two-space display gap;
  short numeric sentences retain their separation rather than becoming decimals.
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
  carry, displayed history and continuation matching state. Successful resumption of the same
  session preserves that state. Session IDs on subtitle packets ensure stale
  packets cannot contaminate a new session, even if the UI queue drops a boundary
  notification under load.
- Standalone hesitation fragments wait for continuation and expire at the hard
  deadline. Meaningful text retains a bounded display deadline; a dangling tail
  after a comma is kept for the next fragment when possible.
- Session-close logs distinguish shutdown, intentional disconnect, server
  rotation, send/receive errors, and watchdog expiry. Desktop sleep logs also
  distinguish user pause from detected silence.
- Abnormal WebSocket EOF is logged as a warning even when automatic recovery
  succeeds; the delayed subtitle-notice policy still applies. Transport-failure
  records include bounded exception types, numeric OS/close codes, connection ID,
  and recent audio/server activity ages without exception-message payloads.
  Matching Ping/Pong timing is recorded even when Pong timeouts are disabled;
  absence of older Pong logs alone was not evidence of a missing server reply.
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

Source and translation packets are appended as text deltas, preserving word-boundary
whitespace and repeated letters. Committed utterances are not discarded just
because they repeat or contain an earlier sentence; the stream does not provide
a segment ID that would reliably distinguish a replay from real repetition.

Both `gemini.log_transcriptions` and `subtitle.log_rendered_subtitles` default to
false. They independently enable transcript and subtitle/pairing text in logs;
local overrides can still enable either. Log formatting redacts registered API
keys and recognized key formats from messages, exception chains and stack text.
Failure to save window settings during exit is logged and does not prevent
stopping the translator or destroying the window.

## Logs and local files

Logs default to `logs/app.log` in the application folder. Each log is limited to
5 MiB with three rotated backups (`app.log.1` through `app.log.3`). Adjust
`app.log_max_bytes` and `app.log_backup_count` to change retention. The app,
desktop and Gemini components share one rotating file handler. If the log
cannot be opened, startup continues with console logging.

An existing `app.log_file: app.log` override remains valid and now resolves
against the application folder. Existing log files are not moved or deleted.
`desktop_state.json` stores window and device preferences; `.runtime/` contains
the current desktop's temporary stop registration. These files and private
configuration are ignored by Git and excluded from release packages.

## File translation diagnostic

For a recorded WAV file, install the optional file dependencies, then run:

```powershell
python -m pip install ".[files]"
python scripts/translate_file.py path/to/audio.wav
```

This command sends audio to Gemini and prints transcript text. It requires your
API key and is separate from the offline test suite. SciPy and SoundFile are not
needed by the live desktop application.
