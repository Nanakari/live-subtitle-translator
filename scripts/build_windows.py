"""Build a portable Windows release from an explicit, private-file-free manifest."""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .release_artifacts import validate_version, write_metadata
else:
    from release_artifacts import validate_version, write_metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        validate_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Build this release on Windows x64")
    root = Path(__file__).resolve().parents[1]
    dist = root / "dist" / args.version
    dist.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
    name = "LiveSubtitleTranslator"
    with tempfile.TemporaryDirectory(prefix="translator-build-") as scratch:
        staging = Path(scratch)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--windowed", "--onedir", "--name", name,
            "--paths", str(root / "src"), "--distpath", str(staging / "dist"),
            "--workpath", str(staging / "work"), "--specpath", str(staging),
            "--collect-all", "soundcard", "--collect-data", "live_subtitle_translator",
            "--collect-all", "google.genai", "--collect-submodules", "websockets",
            "--hidden-import", "pystray._win32",
            "--copy-metadata", "google-genai",
            str(root / "scripts" / "desktop_entry.py"),
        ], cwd=root, check=True)
        package = staging / "dist" / name
        for filename in ("config.local.example.yaml", "README.md", "PORTABLE-README.txt", "stop.ps1", "stop.bat", "CONTRIBUTING.md", "SECURITY.md"):
            shutil.copy2(root / filename, package / filename)
        shutil.copytree(root / "docs", package / "docs")
        report = staging / "smoke-test.json"
        result = subprocess.run([str(package / f"{name}.exe"), "--self-test", str(report)],
                                cwd=staging, timeout=60)
        if not report.exists():
            raise RuntimeError(f"Frozen smoke test did not report a result: {result.returncode}")
        data = json.loads(report.read_text(encoding="utf-8"))
        if result.returncode or not data.get("ok") or not data.get("frozen"):
            raise RuntimeError(f"Frozen smoke test failed: {data}")
        if not Path(data["config_root"]).samefile(package):
            raise RuntimeError("Frozen app loaded configuration from the wrong directory")
        basename = f"LiveSubtitleTranslator-{args.version}-windows-x64"
        archive = Path(shutil.make_archive(str(dist / basename), "zip", package.parent, name))
        notes_path = root / "docs" / "releases" / f"{args.version}.md"
        notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else (
            f"Live Subtitle Translator {args.version}\n\nPortable Windows x64 build from {commit}.\n"
        )
        write_metadata(dist, args.version, commit, dirty, notes)
        print(f"Offline frozen smoke test passed. Release: {archive}")


if __name__ == "__main__":
    main()
