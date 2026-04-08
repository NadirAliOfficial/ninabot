"""
backtester.py — Per-category backtesting engine (F-04)
=======================================================
Replays signal engine on historical Yahoo Finance data.
Returns win rate, P&L, drawdown, Sharpe ratio per category.
"""

import logging
import time
from typing import Optional

log = logging.getLogger("bot.backtest")


def run_backtest(
    symbols: list,          # [{"sym":"AAPL","type":"STK","cur":"USD"}, ...]
    mode: str = "trend",
    period: str = "6mo",    # yfinance period: 1mo 3mo 6mo 1y 2y
    interval: str = "1d",   # yfinance interval: 1d 1h 30m
    initial_capital: float = 10_000.0,
) -> dict:
    """
    Run backtest for a list of instruments.
    Returns aggregated stats + per-symbol breakdown.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance non installé — pip install yfinance"}

    from auto_trader import (
        SignalEngine, PriceHistory, Indicators, PositionSizer,
        MODE_CFG, CATEGORY_CFG, get_category, get_category_cfg
    )
    from price_feed import YF_MAP

    mode_cfg = {**MODE_CFG.get(mode, MODE_CFG["trend"])}
    results  = []
    total_pnl = 0.0
    total_trades = 0
    total_wins   = 0

    for inst in symbols:
        sym      = inst.get("sym", "")
        sec_type = inst.get("type", "STK")
        cur      = inst.get("cur", "USD")
        category = get_category(sec_type)
        cat_cfg  = get_category_cfg(sec_type, mode_cfg)

        yf_sym = YF_MAP.get(sym, sym)
        try:
            df = yf.Ticker(yf_sym).history(period=period, interval=interval)
            if df.empty or len(df) < 10:
                results.append({"sym": sym, "error": "Données insuffisantes"})
                continue
        except Exception as e:
            results.append({"sym": sym, "error": str(e)})
            continue

        closes = list(df["Close"])
        highs  = list(df["High"])
        lows   = list(df["Low"])

        engine   = SignalEngine(mode, cat_cfg, {}, {})
        capital  = initial_capital
        position = 0.0
        entry_px = 0.0
        trades   = []
        equity   = [capital]
        min_conv = cat_cfg.get("min_conviction", 45)

        # Replay bar by bar
        for i in range(20, len(closes)):
            h    = PriceHistory(200)
            for j in range(max(0, i-100), i):
                h.push(closes[j], highs[j], lows[j])

            sig   = engine.score(sym, h)
            score = sig.get("score", 0)
            direc = sig.get("direction")
            price = closes[i]

            if position == 0 and score >= min_conv and direc == "LONG":
                atr = Indicators.atr(highs[max(0,i-15):i], lows[max(0,i-15):i], closes[max(0,i-15):i]) or price * 0.01
                qty = PositionSizer.size(
                    nav=capital, risk_pct=cat_cfg.get("risk_pct",1.0),
                    conviction=score, atr=atr, price=price, sec_type=sec_type
                )
                cost = qty * price if sec_type != "CRYPTO" else qty
                if cost <= capital:
                    position  = qty
                    entry_px  = price
                    capital  -= cost

            elif position > 0:
                atr    = Indicators.atr(highs[max(0,i-15):i], lows[max(0,i-15):i], closes[max(0,i-15):i]) or entry_px * 0.01
                sl     = entry_px - atr * cat_cfg.get("atr_sl", 1.5)
                tp     = entry_px + atr * cat_cfg.get("atr_tp", 3.0)
                exit_  = False
                reason = ""
                if price <= sl:
                    exit_, reason = True, "SL"
                elif price >= tp:
                    exit_, reason = True, "TP"
                elif score >= min_conv and direc == "SHORT":
                    exit_, reason = True, "signal_rev"

                if exit_:
                    proceeds = position * price if sec_type != "CRYPTO" else position
                    pnl      = proceeds - (position * entry_px if sec_type != "CRYPTO" else position)
                    capital += proceeds
                    trades.append({
                        "entry": round(entry_px, 4),
                        "exit":  round(price, 4),
                        "pnl":   round(pnl, 2),
                        "win":   pnl > 0,
                        "reason": reason,
                    })
                    position = 0.0
                    entry_px = 0.0

            equity.append(capital + (position * closes[i] if position > 0 else 0))

        # Close any open position at end
        if position > 0:
            last_px  = closes[-1]
            proceeds = position * last_px if sec_type != "CRYPTO" else position
            pnl      = proceeds - (position * entry_px if sec_type != "CRYPTO" else position)
            capital += proceeds
            trades.append({"entry": round(entry_px,4), "exit": round(last_px,4),
                           "pnl": round(pnl,2), "win": pnl > 0, "reason": "end"})
            equity.append(capital)

        n_trades = len(trades)
        n_wins   = sum(1 for t in trades if t["win"])
        sym_pnl  = sum(t["pnl"] for t in trades)
        win_rate = round(n_wins / n_trades * 100, 1) if n_trades > 0 else 0.0

        # Max drawdown
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak: peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        # Sharpe (simplified daily)
        pnls = [t["pnl"] for t in trades]
        if len(pnls) > 1:
            avg  = sum(pnls) / len(pnls)
            var  = sum((p - avg)**2 for p in pnls) / len(pnls)
            std  = var**0.5
            sharpe = round((avg / std) * (252**0.5), 2) if std > 0 else 0.0
        else:
            sharpe = 0.0

        total_pnl    += sym_pnl
        total_trades += n_trades
        total_wins   += n_wins

        results.append({
            "sym":       sym,
            "category":  category,
            "trades":    n_trades,
            "wins":      n_wins,
            "win_rate":  win_rate,
            "pnl":       round(sym_pnl, 2),
            "pnl_pct":   round(sym_pnl / initial_capital * 100, 2),
            "max_dd_pct":round(max_dd, 2),
            "sharpe":    sharpe,
            "last_3":    trades[-3:],
        })

    overall_wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    # Group by category
    by_category = {}
    for r in results:
        cat = r.get("category", "UNKNOWN")
        if "error" in r:
            continue
        c = by_category.setdefault(cat, {"symbols":0,"trades":0,"wins":0,"pnl":0.0})
        c["symbols"] += 1
        c["trades"]  += r["trades"]
        c["wins"]    += r["wins"]
        c["pnl"]     += r["pnl"]
    for cat, c in by_category.items():
        c["win_rate"] = round(c["wins"] / c["trades"] * 100, 1) if c["trades"] > 0 else 0.0
        c["pnl"] = round(c["pnl"], 2)

    return {
        "mode":            mode,
        "period":          period,
        "interval":        interval,
        "initial_capital": initial_capital,
        "total_trades":    total_trades,
        "total_wins":      total_wins,
        "overall_win_rate":overall_wr,
        "total_pnl":       round(total_pnl, 2),
        "total_pnl_pct":   round(total_pnl / initial_capital * 100, 2),
        "by_category":     by_category,
        "symbols":         results,
        "generated_at":    time.strftime("%Y-%m-%d %H:%M:%S"),
    }
