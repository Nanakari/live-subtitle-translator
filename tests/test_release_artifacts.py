from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.release_artifacts import validate_artifacts, validate_version, write_metadata


class ReleaseTests(unittest.TestCase):
    commit = "b" * 40

    def release(self, directory, version="v0.2.0", dirty=False):
        root = Path(directory)
        (root / f"LiveSubtitleTranslator-{version}-windows-x64.zip").write_bytes(b"offline artifact fixture")
        return write_metadata(root, version, self.commit, dirty, "Notes belonging to this build.\n")

    def test_future_versions_and_prereleases_publish_their_own_metadata(self):
        for version in ("v0.2.0", "v1.0.0-rc.1"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                self.release(directory, version)
                value = validate_artifacts(Path(directory), self.commit)
                self.assertEqual(value["version"], version)
                self.assertIn(version, value["archive"])

    def test_artifact_must_belong_to_the_selected_build_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.release(directory)
            with self.assertRaisesRegex(ValueError, "commit"):
                validate_artifacts(Path(directory), "a" * 40)

    def test_dirty_local_build_cannot_be_published_as_a_clean_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.release(directory, dirty=True)
            with self.assertRaisesRegex(ValueError, "clean build"):
                validate_artifacts(Path(directory), self.commit)

    def test_modified_archive_or_notes_are_rejected(self):
        for name in ("LiveSubtitleTranslator-v0.2.0-windows-x64.zip", "RELEASE_NOTES.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.release(directory)
                (Path(directory) / name).write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    validate_artifacts(Path(directory), self.commit)

    def test_unsafe_version_cannot_become_a_path_or_action_output(self):
        for version in ("../v0.2.0", "v0.2.0\narchive=other", "v1.0.0-..", "0.2.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_version(version)
