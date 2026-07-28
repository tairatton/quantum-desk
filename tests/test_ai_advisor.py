import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from xau import ai_advisor
from xau.webapp import create_app


def payload(action="BUY"):
    return {
        "symbol": "BTCUSD", "timeframe": "M15",
        "broker_symbol": "BTCUSDm",
        "source": {"account": "SECRET", "server": "SECRET-SERVER"},
        "tick": {"bid": 1, "ask": 2},
        "bias": {"time": "2026-07-26 08:30:00", "price": 64300},
        "setup": {"ok": True, "action": action, "status": f"{action} ACTIVE",
                  "entry": 64300, "stop": 64100, "targets": []},
        "quantum": {"market_bias": "Bullish", "market_state": "INST. BUYING",
                    "structure": {"direction": "Bullish"}, "htf_timeframe": "H1",
                    "long_score": 7, "short_score": 2, "required_score": 6,
                    "atr": 100, "rsi": 55, "vwap": 64200, "cvd": 20,
                    "filters": [{"name": "HTF EMA", "ok": True}]},
        "stats": {"trades": 100, "tp_win_rates": {"TP1": 55},
                  "sl_hits": 45, "timeouts": 2},
    }


def response(action="BUY", confidence=80, flags=None):
    content = {"action": action, "confidence": confidence,
               "reasons": ["structure confirms"], "risk_flags": flags or [],
               "summary_th": "โครงสร้างสนับสนุนแผนปัจจุบัน"}
    return {"id": "mock-1", "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


class AdvisorTests(unittest.TestCase):
    def test_snapshot_excludes_account_metadata(self):
        raw = json.dumps(ai_advisor.market_snapshot(payload()))
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("broker_symbol", raw)
        self.assertNotIn("tick", raw)

    def test_matching_decision_approves_quantum(self):
        result = ai_advisor.advise(payload(), transport=lambda _: response(),
                                   force=True, persist=False)
        self.assertTrue(result["approved"])
        self.assertEqual(result["effective_action"], "BUY")

    def test_request_requires_schema_and_private_provider(self):
        captured = {}
        def transport(body):
            captured.update(body)
            return response()
        ai_advisor.advise(payload(), transport=transport, force=True, persist=False)
        self.assertEqual(captured["response_format"]["type"], "json_schema")
        self.assertTrue(captured["response_format"]["json_schema"]["strict"])
        self.assertTrue(captured["provider"]["require_parameters"])
        self.assertEqual(captured["provider"]["data_collection"], "deny")
        self.assertEqual(captured["max_tokens"], 1200)
        self.assertEqual(captured["reasoning"]["effort"], "low")
        self.assertTrue(captured["reasoning"]["exclude"])
        self.assertNotIn("seed", captured)
        prompt = json.dumps(captured["messages"])
        self.assertNotIn("SECRET", prompt)

    def test_data_collection_requires_explicit_opt_in(self):
        captured = {}
        with patch.dict(os.environ, {"OPENROUTER_ALLOW_DATA_COLLECTION": "1"}):
            ai_advisor.advise(payload(), transport=lambda body: captured.update(body) or response(),
                              force=True, persist=False)
        self.assertEqual(captured["provider"]["data_collection"], "allow")

    def test_opposite_decision_is_blocked_not_reversed(self):
        result = ai_advisor.advise(payload(), transport=lambda _: response("SELL", 90),
                                   force=True, persist=False)
        self.assertFalse(result["approved"])
        self.assertEqual(result["effective_action"], "WAIT")

    def test_risk_flag_blocks_matching_decision(self):
        result = ai_advisor.advise(payload(), transport=lambda _: response(flags=["conflict"]),
                                   force=True, persist=False)
        self.assertFalse(result["approved"])
        self.assertEqual(result["effective_action"], "WAIT")

    def test_invalid_schema_fails_closed(self):
        broken = response()
        broken["choices"][0]["message"]["content"] = '{"action":"BUY"}'
        with self.assertRaises(ai_advisor.AIAdvisorError):
            ai_advisor.advise(payload(), transport=lambda _: broken,
                              force=True, persist=False)

    def test_unconfigured_endpoint(self):
        app = create_app()
        with patch.dict(os.environ, {}, clear=True):
            client = app.test_client()
            status = client.get("/api/ai-status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.get_json()["configured"])
            result = client.post("/api/ai-analysis", json={"symbol": "BTCUSD", "tf": "M15"})
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.get_json()["kind"], "not_configured")

    def test_cached_backtest_uses_forward_decision_without_api_call(self):
        bar_time = pd.Timestamp("2026-07-26 08:30:00")
        frame = pd.DataFrame({"time": [bar_time], "spread": [10]})
        plan = {"signal_index": 0, "entry_fill_index": 0, "risk": 100,
                "resolved": ["tp", "sl", None]}
        quantum = {"data": frame, "plans": [plan]}
        record = {
            "recorded_at": 1,
            "snapshot": {"symbol": "BTCUSD", "timeframe": "M15",
                         "quantum_plan": {"signal_time": str(bar_time)}},
            "decision": {"approved": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            decision_dir = Path(tmp)
            (decision_dir / "openrouter_decisions.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8")
            with patch.object(ai_advisor.config, "AI_DECISION_DIR", decision_dir):
                report = ai_advisor.cached_backtest(frame, quantum, "BTCUSD", "M15")
        self.assertEqual(report["coverage"]["percent"], 100.0)
        self.assertEqual(report["ai_filter"]["plans"], 1)
        self.assertEqual(report["ai_filter"]["targets"]["TP1"]["win_rate"], 100.0)


if __name__ == "__main__":
    unittest.main()
