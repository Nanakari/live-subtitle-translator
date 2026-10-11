# Live Subtitle Translator

[![CI](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/Nanakari/live-subtitle-translator/actions/workflows/ci.yml)

A Windows desktop application that captures system playback audio, translates it
through Gemini Live, and displays an always-on-top subtitle window. It supports
bilingual, translation-only and original-only subtitles in compact or classic
layouts.

## Requirements

- Windows 10/11 with a stereo playback device.
- Python 3.10 or later for source installations; Python 3.10 and 3.12 are tested.
- Internet access and a Gemini API key with access to the configured Live model.

Selected playback audio is sent to Gemini; API usage may incur charges.

## Portable application

Download the Windows x64 ZIP from
[Releases](https://github.com/Nanakari/live-subtitle-translator/releases/latest),
extract the entire folder, and follow [PORTABLE-README.txt](PORTABLE-README.txt).
Python is included. The executable is unsigned.

## Run from source

```powershell
git clone https://github.com/Nanakari/live-subtitle-translator.git
cd live-subtitle-translator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "your-api-key"
python app.py
```

The package also supports `python -m live_subtitle_translator`. Double-click
`start_hidden.vbs` to launch the existing source installation without a console.
Press `Ctrl+Alt+Space` to pause or resume; right-click the subtitles for options;
use the system tray to show, hide or exit the application. `stop.bat` requests a
graceful exit from the desktop registered in that folder.

For private settings, copy `config.local.example.yaml` to `config.local.yaml`.
The application merges those settings over its bundled defaults. Existing
legacy `config.yaml` files remain readable. Window preferences are saved in
`desktop_state.json`; rotating logs default to `logs/app.log`.

## Documentation

- [User guide](docs/user-guide.md): configuration, subtitle timing, pause,
  connection recovery, logs and the optional WAV diagnostic.
- [Architecture](docs/architecture.md): package layout, component ownership
  and state boundaries.
- [Development](docs/development.md): offline checks, Windows packaging and
  release verification.
- [Release notes](docs/releases/v0.1.0.md): the v0.1.0 release history.
- [Contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md).

Run the same checks used by CI with `python scripts/check.py`. Tests do not
require an API key or capture real audio.

## Security

Never commit private configuration, API keys, captured audio, transcripts, logs
or virtual environments. Revoke any exposed key through the provider console.
Follow [SECURITY.md](SECURITY.md) when reporting sensitive findings.

## License

No open-source license is currently granted for this repository. Public
visibility does not by itself grant permission to copy, modify or distribute
the code.
