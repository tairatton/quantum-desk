"""Double-click friendly entry point for the FUTURES tree.

    python main.py            interactive terminal
    python main.py --status   one status screen, no menu (cron-friendly)

The real implementation lives in core/entrypoints/main.py -- this file only
puts core/ on sys.path so bot, engine and strategy resolve as top-level
packages, then hands off.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from entrypoints.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
