"""Create and validate versioned, commit-bound portable release metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def validate_version(version: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?", version):
        raise ValueError("Use a version such as v0.1.0 or v0.2.0-rc.1")
    return version


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_metadata(directory: Path, version: str, commit: str, dirty: bool, notes: str) -> dict:
    validate_version(version)
    archive = f"LiveSubtitleTranslator-{version}-windows-x64.zip"
    notes_name = "RELEASE_NOTES.txt"
    (directory / notes_name).write_text(notes, encoding="utf-8")
    metadata = {
        "format_version": 1, "version": version, "commit": commit, "source_dirty": dirty,
        "archive": archive, "notes": notes_name,
        "sha256": {name: sha256(directory / name) for name in (archive, notes_name)},
    }
    (directory / "release.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    files = [archive, notes_name, "release.json"]
    (directory / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(directory / name)}  {name}\n" for name in files), encoding="utf-8",
    )
    return metadata


def validate_artifacts(directory: Path, expected_commit: str) -> dict:
    metadata = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    version = validate_version(metadata["version"])
    if metadata.get("format_version") != 1 or metadata.get("source_dirty") is not False:
        raise ValueError("Only a clean build with supported metadata can be published")
    if not re.fullmatch(r"[a-f0-9]{40}", expected_commit) or metadata.get("commit") != expected_commit:
        raise ValueError("Artifact commit does not match the successful build run")
    archive = f"LiveSubtitleTranslator-{version}-windows-x64.zip"
    if metadata.get("archive") != archive or metadata.get("notes") != "RELEASE_NOTES.txt":
        raise ValueError("Unexpected archive or release-notes path")
    names = (archive, "RELEASE_NOTES.txt")
    if set(metadata.get("sha256", {})) != set(names):
        raise ValueError("Incomplete artifact checksums")
    for name in names:
        path = directory / name
        if path.is_symlink() or sha256(path) != metadata["sha256"][name]:
            raise ValueError(f"Artifact checksum mismatch: {name}")
    expected_checksums = "".join(
        f"{sha256(directory / name)}  {name}\n" for name in (*names, "release.json")
    )
    if (directory / "SHA256SUMS.txt").read_text(encoding="utf-8") != expected_checksums:
        raise ValueError("SHA256SUMS.txt does not describe this release")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    metadata = validate_artifacts(args.directory, args.expected_commit)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            for key in ("version", "archive", "notes"):
                stream.write(f"{key}={metadata[key]}\n")
            stream.write(f"prerelease={'true' if '-' in metadata['version'] else 'false'}\n")
    print(f"Verified {metadata['version']} at {metadata['commit']}")


if __name__ == "__main__":
    main()
