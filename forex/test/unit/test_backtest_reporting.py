import unittest

from strategy import backtest_reporting
from strategy import config


def sample_report(symbol="EURUSD", timeframe="M30", nets=(1.0, 2.0, 3.0)):
    technique = "fixed_tp1"
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "holdout_ranking": [{"technique": technique, "net_r": nets[2]}],
        "techniques": {
            technique: {
                split: {"net_r": net}
                for split, net in zip(("train", "validation", "holdout"), nets)
            }
        },
    }


class BacktestReportingTests(unittest.TestCase):
    def test_symbol_decimal_config_matches_mt5_quote_precision(self):
        self.assertEqual(config.symbol_cfg("XAUUSD")["decimals"], 3)
        self.assertEqual(config.symbol_cfg("EURUSD")["decimals"], 5)
        self.assertEqual(config.symbol_cfg("USDJPY")["decimals"], 3)

    def test_consistency_requires_every_split_to_be_profitable(self):
        self.assertTrue(backtest_reporting.is_consistent(sample_report()))
        self.assertFalse(backtest_reporting.is_consistent(sample_report(nets=(-1, 2, 3))))

    def test_sort_reports_uses_configured_symbol_and_timeframe_order(self):
        reports = [
            sample_report("USDJPY", "H1"),
            sample_report("BTCUSD", "M15"),
            sample_report("BTCUSD", "M5"),
        ]
        ordered = backtest_reporting.sort_reports(reports)
        self.assertEqual(
            [(report["symbol"], report["timeframe"]) for report in ordered],
            [("BTCUSD", "M5"), ("BTCUSD", "M15"), ("USDJPY", "H1")],
        )


if __name__ == "__main__":
    unittest.main()
