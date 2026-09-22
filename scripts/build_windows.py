"""Build a portable Windows release from an explicit, private-file-free manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build this release on Windows x64")
    root = Path(__file__).resolve().parents[1]
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    name = "LiveSubtitleTranslator"
    with tempfile.TemporaryDirectory(prefix="translator-build-") as scratch:
        staging = Path(scratch)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--windowed", "--onedir", "--name", name,
            "--paths", str(root), "--distpath", str(staging / "dist"),
            "--workpath", str(staging / "work"), "--specpath", str(staging),
            "--collect-all", "soundcard", "--collect-all", "soundfile",
            "--collect-all", "google.genai", "--collect-submodules", "websockets",
            "--hidden-import", "pystray._win32",
            "--copy-metadata", "google-genai",
            str(root / "scripts" / "desktop_entry.py"),
        ], cwd=root, check=True)
        package = staging / "dist" / name
        for filename in ("config.yaml", "config.local.example.yaml", "README.md", "PORTABLE-README.txt"):
            shutil.copy2(root / filename, package / filename)
        report = staging / "smoke-test.json"
        result = subprocess.run([str(package / f"{name}.exe"), "--self-test", str(report)],
                                cwd=staging, timeout=60)
        if not report.exists():
            raise RuntimeError(f"Frozen smoke test did not report a result: {result.returncode}")
        data = json.loads(report.read_text(encoding="utf-8"))
        if result.returncode or not data.get("ok") or not data.get("frozen"):
            raise RuntimeError(f"Frozen smoke test failed: {data}")
        if Path(data["config_root"]) != package:
            raise RuntimeError("Frozen app loaded configuration from the wrong directory")
        basename = f"LiveSubtitleTranslator-{args.version}-windows-x64"
        archive = Path(shutil.make_archive(str(dist / basename), "zip", package.parent, name))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (dist / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        print(f"Offline frozen smoke test passed. Release: {archive}")


if __name__ == "__main__":
    main()
