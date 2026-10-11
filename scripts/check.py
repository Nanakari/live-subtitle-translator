"""Run the same offline checks locally and in CI, from any working directory."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = (
        [sys.executable, "-m", "pip", "check"],
        [sys.executable, "-m", "compileall", "-q", "app.py", "scripts", "src", "tests"],
        [sys.executable, "-m", "unittest", "discover", "-v"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
