# Architecture

## Repository layout

```text
app.py                              Source launcher kept for compatibility
pyproject.toml                      Package metadata and dependency groups
requirements*.txt                   Install shortcuts for the package/extras
config.local.example.yaml           Public example for private overrides
start.bat / start_hidden.vbs         Windows source launchers
stop.bat / stop.ps1                  Verified desktop stop protocol
src/live_subtitle_translator/
  __main__.py                       python -m entry point
  app.py                            Desktop startup and translation lifecycle
  config.py / defaults.yaml         Configuration loading and shared defaults
  paths.py                          Application-relative storage paths
  audio_capture.py                  WASAPI loopback capture and downmixing
  silence_detector.py               Local silence sleep/wake decisions
  gemini_live_translate.py           Gemini session, transport and watchdogs
  connection_errors.py              Retry and resumption error classification
  recovery_notice.py                Delayed notices for continuous outages
  transport_diagnostics.py          Safe exception chains and rotation classification
  desktop_runtime.py                Settings, controls, hotkey, mutex and tray
  process_control.py                Current desktop identity and stop event
  subtitle_config.py                Typed subtitle settings
  subtitle_state.py                 Text deltas, timing, pairing and history
  subtitle_window.py                Tk widgets, rendering and UI event queue
  logger.py                         Redaction and shared rotating log handlers
scripts/
  check.py                          Unified offline checks
  translate_file.py                 Optional WAV-to-Gemini diagnostic
  desktop_entry.py                   Frozen entry point and offline smoke test
  build_windows.py                  Portable assembly from public files
  release_artifacts.py               Release metadata and integrity checks
tests/                              Unit and Windows process integration tests
docs/                               User, development and architecture guides
  releases/                         Versioned release notes
.github/workflows/                   CI, portable build and verified publication
```

`assets/` may contain locally supplied icon resources. Build scripts use an
explicit public manifest; the presence of a local file does not include it in
a release. `dist/`, `.runtime/`, `logs/`, `.venv/`, private configuration and
desktop state are generated or machine-specific data, not source modules.

## Runtime ownership

The Tk main thread owns the window and text state. A worker thread runs the
asynchronous Gemini lifecycle; a capture thread yields mono audio chunks.
Bounded queues connect capture to transmission and model events to the UI.
Tray and hotkey callbacks enqueue UI actions instead of drawing from other
threads. Shutdown cancels network tasks and releases the recorder and desktop
services.

Manual pause increments an audio generation, clears queued audio and sets a
sticky recorder reset. The sender rejects chunks from older generations; the
recorder drops an in-flight read and closes before waiting. Automatic silence
sleep instead retains a bounded local preroll for the next spoken phrase.

The Windows stop record contains the desktop PID, actual executable, creation
time, application root and named event. `stop.ps1` holds a process handle while
validating that identity, requests a graceful stop, and only force-terminates
that same process when `-Force` was supplied.

## Subtitle state

`SubtitleState` has no Tk or native-window dependencies. Source and translation
events are deltas: concatenation preserves packet-boundary spaces, repeated
letters and spoken repetitions. Similar text is not sufficient evidence to
discard a new utterance.

Original-only mode owns a source buffer with an absolute start timestamp. It
commits on punctuation, turn completion or the configured stable deadline.
Pruning the time window used for bilingual pairing cannot discard this buffer.
Bilingual mode retains timestamped source fragments while Chinese sentences
wait for pairing, including bounded continuation handling. A new session resets
unfinished state; successful resumption retains the same logical session ID.

`SubtitleWindow` consumes these state transitions, renders compact tracks or
classic history, and owns window geometry, fonts, menus and Windows regions.
`SubtitleWindowConfig` derives its defaults from the same bundled YAML used by
the application. Persisted display choices override only the corresponding
window settings.

## Configuration and packaging

The package includes `defaults.yaml`; application code never depends on a
`config.yaml` in the launch directory. Legacy and private override files are
read relative to the application root. Configuration loading does not rewrite
them. Retired keys are explicitly ignored with a migration warning.

`pyproject.toml` owns pinned direct dependencies. Requirements files only select
the normal package or build extra; file conversion dependencies are optional.
The Windows builder collects package data, copies public documents, and tests
the frozen executable from an unrelated working directory. Release metadata
binds the requested version, source commit, notes and archive checksums.
