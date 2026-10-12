"""Make sure the Python this session runs can import the project's dependencies.

Called by the SessionStart hook in `.claude/settings.json`. It exists because a
Claude Code cloud sandbox (checked 2026-10-06) has `python` and `python3` at
3.11 while its preinstalled pandas/numpy sit under 3.13, and no setup script is
configured, so a fresh session cannot run `python -m football_ai...` until
something installs them for the interpreter that will actually be used.

Silent and instant where the packages already import (the owner's machine);
installs from requirements.txt with `sys.executable -m pip` otherwise. It never
fails the session: a problem is reported on stderr and the exit code says so,
nothing more.
"""
import subprocess
import sys
from pathlib import Path


def main():
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        return 0
    except ImportError:
        pass
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "-r", str(requirements)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ensure_deps: pip install failed with {sys.executable}:\n"
              f"{r.stderr[-800:]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
