"""Read-only future entry-point previews the production dynamic tier."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from entrypoints import live  # noqa: E402
from bot.settings import Settings  # noqa: E402


class RiskPreviewTests(unittest.TestCase):
    def test_dynamic_preview_does_not_report_the_static_floor(self):
        settings = Settings(initial_balance=50_000.0)
        preview = live.risk_preview(settings)
        self.assertIn("$400 dynamic", preview)
        self.assertIn("floor $200", preview)

    def test_fixed_preview_is_explicit(self):
        settings = Settings(initial_balance=50_000.0,
                            dynamic_risk_enabled=False)
        self.assertEqual(live.risk_preview(settings), "$200 fixed")


if __name__ == "__main__":
    unittest.main()
