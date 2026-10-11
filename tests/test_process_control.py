"""Exercise the Windows stop protocol against processes owned by these tests."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from live_subtitle_translator.process_control import DesktopProcessRegistration
done = threading.Event()
registration = DesktopProcessRegistration(Path(sys.argv[2]))
try:
    registration.start(done.set if sys.argv[3] == 'graceful' else lambda: None)
    if not done.wait(60):
        raise SystemExit(3)
finally:
    registration.close()
"""


@unittest.skipUnless(os.name == "nt", "Windows named-event and PowerShell integration")
class StopScriptTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="translator-stop-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        shutil.copy2(ROOT / "stop.ps1", self.root / "stop.ps1")
        self.record_path = self.root / ".runtime" / "desktop-process.json"

    def launch(self, command):
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)

        self.addCleanup(cleanup)
        return process

    def registered_process(self, mode="graceful"):
        process = self.launch([sys.executable, "-c", CHILD, str(ROOT / "src"), str(self.root), mode])
        deadline = time.monotonic() + 10
        while not self.record_path.exists():
            if process.poll() is not None:
                self.fail(f"Registration failed: {process.stderr.read().decode(errors='replace')}")
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for desktop registration")
            time.sleep(0.02)
        return process

    def stop(self, *extra):
        return subprocess.run([
            shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(self.root / "stop.ps1"),
            "-TimeoutSeconds", "1", *extra,
        ], capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_graceful_stop_leaves_other_python_process_running(self):
        unrelated = self.launch([sys.executable, "-c", "import time; time.sleep(60)"])
        desktop = self.registered_process()
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(desktop.wait(timeout=5), 0)
        self.assertFalse(self.record_path.exists())
        self.assertIsNone(unrelated.poll())

    def test_stale_identity_never_stops_process_even_with_force(self):
        desktop = self.registered_process()
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        for field, value in (
            ("created_filetime", str(int(record["created_filetime"]) + 1)),
            ("executable", str(self.root / "other.exe")),
            ("root", str(self.root / "other")),
        ):
            with self.subTest(field=field):
                self.record_path.write_text(json.dumps({**record, field: value}), encoding="utf-8")
                result = self.stop("-Force")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(desktop.poll())
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(desktop.wait(timeout=5), 0)

    def test_hung_desktop_requires_force_and_only_that_process_exits(self):
        unrelated = self.launch([sys.executable, "-c", "import time; time.sleep(60)"])
        desktop = self.registered_process(mode="ignore")
        result = self.stop()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIsNone(desktop.poll())
        result = self.stop("-Force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNotNone(desktop.poll())
        self.assertIsNone(unrelated.poll())
