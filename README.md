# Live Subtitle Translator

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

## Chrome extension

1. Start the local bridge by double-clicking `start_chrome_bridge_hidden.vbs`.
2. Open `chrome://extensions`, enable Developer mode, and choose **Load
   unpacked**.
3. Select the `chrome-extension` directory.
4. Open a page to translate, click the extension icon, and start translation.

The extension captures the current tab's audio. Its overlay is limited to the
current page; use the desktop app when subtitles must stay visible across other
applications.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py chrome_server.py scripts src
```

## Security

- Never commit `config.local.yaml`, `.env`, API keys, audio recordings, logs,
  or the `.venv` directory.
- If a key is exposed, revoke it in the provider console and create a new one.
