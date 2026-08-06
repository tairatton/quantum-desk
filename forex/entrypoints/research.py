"""HTF Quantum Adaptive analysis (XAUUSD / BTCUSD) - CLI entry point.

    python -m entrypoints.research fetch    H1 --symbol BTCUSD --bars 6000
    python -m entrypoints.research analyze  H1 --symbol XAUUSD
    python -m entrypoints.research plot     H4 --symbol BTCUSD
    python -m entrypoints.research run      H1  # fetch + analyze + plot
    python -m entrypoints.research serve       # web dashboard on :8000
    python -m entrypoints.research backtest    # measure every symbol x timeframe
    python -m entrypoints.research symbols     # what the broker offers
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ and __package__ != "entrypoints":
    raise SystemExit(
        "Run this from inside the Forex tree:\n"
        "    cd forex && python -m entrypoints.research"
    )
if not __package__:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from strategy import config, mt5_source, quantum


def _slice(df, n: int):
    """Take the last n bars and reset the index so pattern indices align."""
    return df.tail(n).reset_index(drop=True)


def cmd_fetch(args) -> int:
    if args.mock:
        df, meta = mt5_source.generate_mock(
            symbol=args.symbol, timeframe=args.timeframe, bars=args.bars)
    else:
        df, meta = mt5_source.fetch(
            symbol=args.symbol, timeframe=args.timeframe, bars=args.bars)
    mt5_source.save(df, meta)
    print(df.tail(3).to_string(index=False))
    return 0


def cmd_symbols(args) -> int:
    with mt5_source.connection() as mt:
        info = mt.account_info()
        print(f"Account {info.login} @ {info.server}  ({info.currency})")
        print("\nConfigured symbols:")
        for key, cfg in config.SYMBOLS.items():
            try:
                resolved = mt5_source.resolve_symbol(mt, key)
                si = mt.symbol_info(resolved)
                hours = "24/5" if cfg["weekend_gap"] else "24/7"
                print(f"  {key:<8} {cfg['label']:<9} -> {resolved:<12} "
                      f"digits={si.digits}  spread={si.spread:<6} {hours}")
            except mt5_source.MT5Error:
                print(f"  {key:<8} {cfg['label']:<9} -> UNAVAILABLE")
    return 0


def _analyze(args):
    df, meta = mt5_source.get_data(
        args.symbol, args.timeframe, args.bars, refresh=args.refresh)
    window = _slice(df, args.bars_shown)

    symbol = meta.symbol if meta else args.symbol
    result = quantum.analyse(df.tail(args.bars).reset_index(drop=True), args.timeframe)
    public = quantum.public(result)
    setup = public["setup"]
    print("\n" + "=" * 74)
    print(f" {symbol} {args.timeframe.upper()} | HTF QUANTUM ADAPTIVE ".center(74))
    print("=" * 74)
    print(f" Price ${public['price']:,.2f} | VWAP ${public['vwap']:,.2f} | "
          f"{public['market_bias']} | {public['market_state']}")
    flow_action = "BUY" if "BUY" in public["market_state"] else "SELL" if "SELL" in public["market_state"] else "WAIT"
    print(f" MARKET FLOW : {flow_action}  (context only, not an entry order)")
    print(f" Filters: LONG {public['long_score']}/9  SHORT {public['short_score']}/9  "
          f"need {public['required_score']}")
    if setup:
        print(f" TRADE PLAN  : {setup['action']} / {setup['position']} / {setup['lifecycle']}")
        print(f" {setup['status']}  Entry ${setup['entry']:,.2f}  SL ${setup['stop']:,.2f}")
        for target in setup["targets"]:
            print(f" {target['label']} ${target['price']:,.2f}  {target['rr']:.1f}R  "
                  f"win {target['win_rate']}%")
    else:
        print(" WAIT - no accepted Adaptive Structure plan")
    print(f" Filled {public['counts']['filled']} | rates {public['rates']}")
    print("=" * 74 + "\n")
    return window, result, symbol


def cmd_analyze(args) -> int:
    _analyze(args)
    return 0


def cmd_plot(args) -> int:
    from strategy import quantum_chart
    window, result, symbol = _analyze(args)
    fig = quantum_chart.build(window, result, args.timeframe.upper(), symbol)
    config.ensure_dirs()
    out = config.CHART_DIR / f"{args.symbol.lower()}_{args.timeframe.lower()}_quantum.html"
    fig.write_html(out, include_plotlyjs=True)
    print(f"[SAVED] {out}")
    return 0


def cmd_backtest(args) -> int:
    """Run the Pine counters on cached history for each symbol/timeframe."""
    import json

    symbols = [args.symbol] if args.symbol_only else list(config.SYMBOLS)
    frames = [args.timeframe] if args.timeframe_only else config.WEB_TIMEFRAMES

    results = []
    for sym in symbols:
        for tf in frames:
            try:
                df, _ = mt5_source.get_data(sym, tf, args.bars, refresh=args.refresh)
            except (mt5_source.MT5Error, FileNotFoundError) as exc:
                print(f"[SKIP] {sym} {tf}: {exc}")
                continue
            q = quantum.public(quantum.analyse(df.tail(args.bars).reset_index(drop=True), tf))
            results.append({"symbol": sym, "timeframe": tf, "filled": q["counts"]["filled"],
                            "rates": q["rates"], "counts": q["counts"]})

    if not results:
        print("No data to test.")
        return 1

    print("\nHTF QUANTUM ADAPTIVE - PINE COUNTERS")
    for row in results:
        print(f"{row['symbol']:<8} {row['timeframe']:<4} filled {row['filled']:>4}  "
              f"TP1 {row['rates']['TP1']}%  TP2 {row['rates']['TP2']}%  TP3 {row['rates']['TP3']}%")
    config.ensure_dirs()
    out = config.QUANTUM_REPORT_DIR / "summary.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"[SAVED] {out}")
    return 0


def cmd_equity(args) -> int:
    """Render spread-adjusted equity and drawdown from filled Quantum plans."""
    from strategy import equity_chart

    df, _ = mt5_source.get_data(args.symbol, args.timeframe, args.bars,
                                refresh=args.refresh)
    data = df.tail(args.bars).reset_index(drop=True)
    result = quantum.analyse(data, args.timeframe)
    fig = equity_chart.build(data, result, args.symbol, args.timeframe.upper())
    config.ensure_dirs()
    out = config.CHART_DIR / f"{args.symbol.lower()}_{args.timeframe.lower()}_equity.html"
    fig.write_html(out, include_plotlyjs=True,
                   config={"scrollZoom": True, "displaylogo": False})
    print(f"[SAVED] {out}")
    print(f" Filled plans: {result['counts']['filled']} | curves: TP1 / TP2 / TP3")
    return 0


def cmd_ai_backtest(args) -> int:
    """Compare Quantum with persisted forward OpenRouter decisions only."""
    import json
    from strategy import ai_advisor

    df, _ = mt5_source.get_data(args.symbol, args.timeframe, args.bars,
                                refresh=False)
    data = df.tail(args.bars).reset_index(drop=True)
    result = quantum.analyse(data, args.timeframe)
    report = ai_advisor.cached_backtest(data, result, args.symbol, args.timeframe)
    config.ensure_dirs()
    out = (
        config.AI_REPORT_DIR / "cached" / args.symbol.upper()
        / args.timeframe.upper() / "report.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    coverage = report["coverage"]
    print(f"AI decision coverage: {coverage['ai_reviewed']}/{coverage['filled_plans']} "
          f"({coverage['percent']}%) | approved {coverage['ai_approved']}")
    print(f"[SAVED] {out}")
    return 0


def cmd_ai_run_backtest(args) -> int:
    """Run paid, point-in-time OpenRouter review for one symbol/timeframe."""
    import json
    from strategy import ai_historical

    df, _ = mt5_source.get_data(args.symbol, args.timeframe, args.bars, refresh=False)
    data = df.tail(args.bars).reset_index(drop=True)
    result = quantum.analyse(data, args.timeframe)

    def progress(done, total, record):
        if "error" in record:
            print(f"[{done}/{total}] ERROR {record['error']}", flush=True)
        else:
            decision = record["decision"]
            cost = (decision.get("usage") or {}).get("cost")
            print(f"[{done}/{total}] {record['signal_time']} -> {decision['effective_action']} "
                  f"({decision['confidence']}%) cost={cost}", flush=True)

    report = ai_historical.run(data, result, args.symbol, args.timeframe,
                               limit=args.limit, workers=args.workers, progress=progress)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 4


def cmd_technique_lab(args) -> int:
    """Evaluate deterministic exit techniques on a locked chronological holdout."""
    from strategy import technique_lab

    for symbol in args.symbols:
        df, _ = mt5_source.get_data(symbol, args.timeframe, args.bars, refresh=False)
        data = df.tail(args.bars).reset_index(drop=True)
        result = quantum.analyse(data, args.timeframe)
        report = technique_lab.evaluate(data, result, symbol, args.timeframe)
        path = technique_lab.save(report)
        best = report["holdout_ranking"][0]
        print(f"{symbol} {args.timeframe}: best holdout={best['technique']} "
              f"net={best['net_r']}R trades={best['trades']} win={best['win_rate']}%")
        print(f"[SAVED] {path}")
    return 0


def cmd_run(args) -> int:
    args.refresh = True
    return cmd_plot(args)


def cmd_serve(args) -> int:
    from strategy import webapp  # imported lazily so the CLI works without Flask

    webapp.serve(host=args.host, port=args.port, debug=args.debug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m entrypoints.research",
        description="HTF Quantum Adaptive analysis (MetaTrader 5)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp, *, analysis: bool) -> None:
        sp.add_argument("timeframe", nargs="?", default=config.DEFAULT_TIMEFRAME,
                        choices=list(config.TIMEFRAMES), help="chart timeframe")
        sp.add_argument("--symbol", default=config.DEFAULT_SYMBOL,
                        choices=list(config.SYMBOLS), help="instrument")
        sp.add_argument("--bars", type=int, default=config.DEFAULT_BARS,
                        help="bars to download")
        if analysis:
            sp.add_argument("--bars-shown", type=int, default=config.DEFAULT_PLOT_BARS,
                            help="bars to analyse and draw")
            sp.add_argument("--refresh", action="store_true",
                            help="re-download from MT5 instead of using the cache")

    sp = sub.add_parser("fetch", help="download bars from MT5")
    add_common(sp, analysis=False)
    sp.add_argument("--mock", action="store_true",
                    help="generate synthetic bars for offline testing (NOT real prices)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("analyze", help="run HTF Quantum Adaptive analysis")
    add_common(sp, analysis=True)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("plot", help="report + render the chart")
    add_common(sp, analysis=True)
    sp.set_defaults(func=cmd_plot)

    sp = sub.add_parser("run", help="refresh from MT5, then report + chart")
    add_common(sp, analysis=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("symbols", help="list configured instruments")
    sp.set_defaults(func=cmd_symbols)

    sp = sub.add_parser("backtest", help="measure Pine signal performance on history")
    sp.add_argument("timeframe", nargs="?", default=config.DEFAULT_TIMEFRAME,
                    choices=list(config.TIMEFRAMES))
    sp.add_argument("--symbol", default=config.DEFAULT_SYMBOL,
                    choices=list(config.SYMBOLS))
    sp.add_argument("--symbol-only", action="store_true",
                    help="test just --symbol instead of every instrument")
    sp.add_argument("--timeframe-only", action="store_true",
                    help="test just the given timeframe instead of all of them")
    sp.add_argument("--bars", type=int, default=6000, help="history depth")
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("equity", help="render spread-adjusted equity and drawdown")
    sp.add_argument("timeframe", nargs="?", default=config.DEFAULT_TIMEFRAME,
                    choices=list(config.TIMEFRAMES))
    sp.add_argument("--symbol", default=config.DEFAULT_SYMBOL,
                    choices=list(config.SYMBOLS))
    sp.add_argument("--bars", type=int, default=6000, help="history depth")
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_equity)

    sp = sub.add_parser("ai-backtest", help="compare cached forward AI decisions with Quantum")
    sp.add_argument("timeframe", nargs="?", default=config.DEFAULT_TIMEFRAME,
                    choices=list(config.TIMEFRAMES))
    sp.add_argument("--symbol", default=config.DEFAULT_SYMBOL,
                    choices=list(config.SYMBOLS))
    sp.add_argument("--bars", type=int, default=6000, help="history depth")
    sp.set_defaults(func=cmd_ai_backtest)

    sp = sub.add_parser("ai-run-backtest", help="paid point-in-time OpenRouter replay")
    sp.add_argument("timeframe", choices=list(config.TIMEFRAMES))
    sp.add_argument("--symbol", default=config.DEFAULT_SYMBOL, choices=list(config.SYMBOLS))
    sp.add_argument("--bars", type=int, default=6000)
    sp.add_argument("--limit", type=int, default=None, help="review only the first N filled plans")
    sp.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    sp.set_defaults(func=cmd_ai_run_backtest)

    sp = sub.add_parser("technique-lab", help="walk-forward deterministic exit-technique test")
    sp.add_argument("timeframe", choices=list(config.TIMEFRAMES))
    sp.add_argument("--symbols", nargs="+", default=list(config.SYMBOLS),
                    choices=list(config.SYMBOLS))
    sp.add_argument("--bars", type=int, default=50000)
    sp.set_defaults(func=cmd_technique_lab)

    sp = sub.add_parser("serve", help="start the web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--debug", action="store_true")
    sp.set_defaults(func=cmd_serve)

    return p


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args()
    try:
        return args.func(args)
    except mt5_source.MT5Error as exc:
        print(f"\n[MT5 ERROR] {exc}\n", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
