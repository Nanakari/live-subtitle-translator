# Contributing

## Setup

1. Create and activate a Python 3.10 or newer virtual environment.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Copy `config.local.example.yaml` to `config.local.yaml` for machine-specific settings.

Never commit API keys, captured audio, transcripts, logs, or local configuration.

## Checks

Run these checks before opening a pull request:

```powershell
python -m compileall -q app.py chrome_server.py scripts src
python -m json.tool chrome-extension/manifest.json
node --check chrome-extension/content.js
node --check chrome-extension/offscreen.js
node --check chrome-extension/service-worker.js
```

Describe whether a change affects the desktop overlay, Chrome extension, local bridge, audio capture, or API usage.
