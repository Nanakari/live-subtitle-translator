# Contributing

## Setup

1. Create and activate a Python 3.10 or newer virtual environment.
2. Install dependencies with `python -m pip install -r requirements-dev.txt`.
3. Copy `config.local.example.yaml` to `config.local.yaml` for machine-specific settings.

Never commit API keys, captured audio, transcripts, logs, or local configuration.

## Checks

Run these checks before opening a pull request:

```powershell
python scripts/check.py
```

Describe whether a change affects the desktop overlay, audio capture, or API usage.

Application modules belong in `src/live_subtitle_translator/`, automated tests in
`tests/`, development commands in `scripts/`, and detailed documentation in
`docs/`. Update `defaults.yaml` when changing a default; private configuration
must remain in local overrides. Dependencies belong in `pyproject.toml`.

See [architecture](docs/architecture.md) for component boundaries and
[development](docs/development.md) for Windows packaging and release validation.
