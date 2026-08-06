"""Multi-symbol HTF Quantum Adaptive toolkit for metals, crypto, and FX."""

import sys as _sys

# The project path contains non-ASCII characters, so any print touching it
# raises UnicodeEncodeError on a cp874/cp1252 console. Fix it once, here,
# rather than in every entry point.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

__version__ = "2.0.0"

from . import config, indicators, mt5_source, quantum

__all__ = [
    "config", "indicators", "mt5_source", "quantum",
]
