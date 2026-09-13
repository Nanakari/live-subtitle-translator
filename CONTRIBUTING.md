# Contributing

## Setup

1. Create and activate a Python 3.10 or newer virtual environment.
2. Install dependencies with `python -m pip install -r requirements-dev.txt`.
3. Copy `config.local.example.yaml` to `config.local.yaml` for machine-specific settings.

Never commit API keys, captured audio, transcripts, logs, or local configuration.

## Checks

Run these checks before opening a pull request:

```powershell
python -m compileall -q app.py scripts src
python -m unittest discover -v
```

Describe whether a change affects the desktop overlay, audio capture, or API usage.
