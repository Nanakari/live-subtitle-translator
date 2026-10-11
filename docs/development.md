# Development

## Setup and checks

Use Python 3.10 or 3.12 in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python scripts/check.py
```

`check.py` runs dependency consistency checks, compiles project Python files and
runs unittest discovery. It resolves the repository independently of the
current working directory. CI runs this entry point on Windows and Linux with
both supported test versions. The desktop itself targets Windows.

The suite uses synthetic text, audio and transport events. Windows integration
tests launch temporary Python processes to exercise the actual stop script;
they only control the processes they created. On Linux those tests are skipped.
The suite never reads private configuration, captures playback audio, or calls
Gemini. The file translation diagnostic is deliberately outside test discovery.

For a focused change, run the relevant module, for example:

```powershell
python -m unittest tests.test_review_fixes tests.test_audio_capture -v
```

Test changes in text-state behavior without constructing Tk widgets. Use the
frozen smoke test for integration between the package, Tk and bundled runtime.
Keep generated reports and diagnostics outside tracked source directories.

## Windows portable build

Build on Windows x64 with standard CPython 3.12 in a separate environment:

```powershell
python -m pip install -r requirements-build.txt
python scripts/check.py
python scripts/build_windows.py --version v0.2.0-rc.1
```

Version input must have the form `vX.Y.Z` or `vX.Y.Z-suffix`. Artifacts are written
under `dist/<version>/`:

- `LiveSubtitleTranslator-<version>-windows-x64.zip`
- `RELEASE_NOTES.txt`
- `release.json`
- `SHA256SUMS.txt`

The builder uses a temporary staging directory, collects package defaults and
required runtime data, and copies an explicit list of public files plus `docs/`.
It does not copy local configuration, state, logs, recordings or virtual
environments. Release notes come from `docs/releases/<version>.md` if present,
otherwise they identify the requested version and source commit.

Before archiving, the builder runs the frozen executable's `--self-test` from a
different working directory. The test loads default configuration, imports
runtime dependencies, converts synthetic audio, builds Gemini request options,
and renders a bilingual update in a hidden Tk window while switching layouts.
It does not open a network session or an audio device. A missing or failed
self-test report prevents creation of the archive.

Builds from modified or untracked source are marked `source_dirty: true` in
`release.json`. They are useful for local validation but cannot pass the release
publisher's clean-commit check. Portable builds do not require SciPy or SoundFile;
install `.[files]` only when using `scripts/translate_file.py`.

## Release workflow

1. Commit the intended source and public release notes.
2. Dispatch **Windows portable build** with the desired version.
3. Review the successful run and its `windows-x64-portable` artifact.
4. Dispatch **Publish verified portable release** with that build run's ID.

Publication verifies the workflow name, successful conclusion, artifact commit,
version syntax, filenames, notes and SHA-256 checksums. It uses the version and
notes from that build artifact. Versions with a suffix become prereleases;
normal versions become the latest release. An existing tag/release is not
silently overwritten by `gh release create`.

For a clean local build, verify its metadata without publishing:

```powershell
python scripts/release_artifacts.py dist/v0.2.0-rc.1 --expected-commit (git rev-parse HEAD)
```

Publication is a separate, manually triggered workflow. Running local checks
or the package builder does not create a tag, push a commit or publish a release.
