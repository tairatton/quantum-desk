"""Flask web server for the Pine HTF Quantum Adaptive dashboard.

Run it with:  python main.py serve

Flask (sync) rather than FastAPI (async) on purpose: the MetaTrader5 API is
blocking C-extension IPC, so an async framework would just push every call into
a threadpool and add ceremony for no gain.
"""
from __future__ import annotations

import json
from pathlib import Path

import plotly
import plotly.io as pio
from flask import Flask, Response, jsonify, render_template, request, send_file

from . import ai_advisor, config, equity_chart, mt5_source, quantum_chart, service

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _plotlyjs_source() -> Path | str | None:
    """Locate the plotly.js bundle.

    plotly 6.x dropped `plotly.io.get_plotlyjs()` but still ships the minified
    bundle as package data, so prefer the file and fall back to the old helper
    for plotly 5.x.
    """
    bundled = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    if bundled.exists():
        return bundled

    getter = getattr(pio, "get_plotlyjs", None)
    return getter() if callable(getter) else None


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["JSON_SORT_KEYS"] = False

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            timeframes=[
                {"key": tf, "label": config.TIMEFRAME_LABELS.get(tf, tf)}
                for tf in config.WEB_TIMEFRAMES
            ],
            symbols=[
                {"key": k, "label": v["label"]} for k, v in config.SYMBOLS.items()
            ],
            default_tf=config.DEFAULT_TIMEFRAME,
            default_symbol=config.DEFAULT_SYMBOL,
        )

    @app.get("/plotly.js")
    def plotly_js():
        """Serve plotly.js from the installed package - no CDN, works offline."""
        source = _plotlyjs_source()
        if source is None:
            return Response(
                "console.error('plotly.js bundle not found in the plotly package');",
                mimetype="application/javascript", status=500,
            )
        if isinstance(source, Path):
            return send_file(source, mimetype="application/javascript",
                             max_age=86400, conditional=True)
        return Response(source, mimetype="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/health")
    def api_health():
        return jsonify(service.health())

    @app.get("/api/ai-status")
    def api_ai_status():
        return jsonify(ai_advisor.status())

    @app.post("/api/ai-analysis")
    def api_ai_analysis():
        body = request.get_json(silent=True) or {}
        tf = str(body.get("tf", config.DEFAULT_TIMEFRAME)).upper()
        sym = str(body.get("symbol", config.DEFAULT_SYMBOL)).upper()
        if tf not in config.TIMEFRAMES or sym not in config.SYMBOLS:
            return jsonify({"error": "Invalid symbol or timeframe"}), 400
        if not ai_advisor.status()["configured"]:
            return jsonify({"error": "OPENROUTER_API_KEY is not configured",
                            "kind": "not_configured"}), 503
        try:
            result = service.analyse(symbol=sym, timeframe=tf, bars=max(config.DEFAULT_BARS, 1500),
                                     bars_shown=300, refresh=False)
            decision = ai_advisor.advise(service.to_json(result))
            return jsonify(decision)
        except ai_advisor.AIUnavailable as exc:
            return jsonify({"error": str(exc), "kind": "not_configured"}), 503
        except ai_advisor.AIAdvisorError as exc:
            return jsonify({"error": str(exc), "kind": "openrouter"}), 502
        except (mt5_source.MT5Error, FileNotFoundError) as exc:
            return jsonify({"error": str(exc), "kind": "data"}), 503

    @app.get("/api/analysis")
    def api_analysis():
        tf = request.args.get("tf", config.DEFAULT_TIMEFRAME).upper()
        sym = request.args.get("symbol", config.DEFAULT_SYMBOL).upper()
        view = request.args.get("view", "price").lower()
        if tf not in config.TIMEFRAMES:
            return jsonify({"error": f"Unknown timeframe '{tf}'"}), 400
        if sym not in config.SYMBOLS:
            return jsonify({"error": f"Unknown symbol '{sym}'"}), 400
        if view not in ("price", "equity"):
            return jsonify({"error": f"Unknown chart view '{view}'"}), 400

        try:
            bars_shown = max(60, min(1000, request.args.get("bars", 300, type=int)))
            refresh = request.args.get("refresh", "0") == "1"
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid query parameter"}), 400

        # Download enough history that indicators (EMA200) are warmed up.
        download = max(config.DEFAULT_BARS, bars_shown * 3)

        try:
            result = service.analyse(
                symbol=sym, timeframe=tf, bars=download, bars_shown=bars_shown,
                refresh=refresh,
            )
        except mt5_source.MT5Error as exc:
            return jsonify({"error": str(exc), "kind": "mt5"}), 503
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc), "kind": "data"}), 503

        payload = service.to_json(result)
        if view == "equity":
            fig = equity_chart.build(
                result["quantum"]["data"], result["quantum"],
                symbol=sym, timeframe=tf,
            )
        else:
            fig = quantum_chart.build(
                result["df"], result["quantum"], timeframe=tf,
                symbol=result["broker_symbol"],
            )
        payload["view"] = view
        payload["figure"] = json.loads(pio.to_json(fig))
        return jsonify(payload)

    @app.errorhandler(500)
    def on_error(exc):  # pragma: no cover - defensive
        return jsonify({"error": "Internal server error", "detail": str(exc)}), 500

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, debug: bool = False) -> None:
    app = create_app()
    print(f"\n  HTF Quantum Adaptive dashboard -> http://{host}:{port}\n")
    # threaded=True is safe: every MT5 call is serialised in service.py.
    app.run(host=host, port=port, debug=debug, threaded=True)
