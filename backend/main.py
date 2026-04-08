"""
main.py — NINABOT v3 Backend · FastAPI + WebSocket
===================================================
Correction : connexion IBKR fiable, reconnexion auto 15s
"""

import asyncio
import json
import logging
import time
import threading
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config        import settings
from ibkr_client   import IBKRApp
from risk_manager  import RiskManager
from social_monitor import SocialMonitor
from auto_trader   import AutoTrader
from price_feed    import warm_up_prices, get_yf_prices_batch, is_market_open, cache_all

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(name)-14s] %(levelname)s — %(message)s",
    datefmt= "%H:%M:%S",
)
log = logging.getLogger("bot.api")

# ── Globals ──────────────────────────────────────────────────
risk:    RiskManager         = RiskManager()
ibkr:    IBKRApp  | None    = None
social:  SocialMonitor | None = None
trader:  AutoTrader | None  = None
clients: Set[WebSocket]     = set()

# ── Broadcast helpers ────────────────────────────────────────
_loop: asyncio.AbstractEventLoop | None = None

def on_ibkr_update(payload: dict) -> None:
    """Callback IBKR → broadcast WebSocket (thread-safe depuis ibapi thread)."""
    global _loop
    if _loop and not _loop.is_closed():
        msg = json.dumps(payload, default=str)
        asyncio.run_coroutine_threadsafe(_broadcast_raw(msg), _loop)

async def _broadcast_raw(msg: str) -> None:
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

async def _broadcast(payload: dict) -> None:
    await _broadcast_raw(json.dumps(payload, default=str))

# ── Reconnexion automatique (daemon thread) ───────────────────
# IF bot.disconnected THEN reconnect()  — toutes les 15 secondes
def _reconnect_loop() -> None:
    global ibkr
    while True:
        time.sleep(15)
        try:
            if ibkr and not ibkr.connected:
                log.warning("↻ Reconnexion automatique IBKR (15s)...")
                try:
                    ibkr.disconnect()
                except Exception:
                    pass
                time.sleep(2)
                ibkr.connect_and_run()
                time.sleep(3)
                if ibkr.connected:
                    log.info("✅ Reconnexion réussie")
                else:
                    log.warning("⚠ Reconnexion échouée — prochain essai dans 15s")
        except Exception as e:
            log.error(f"Reconnect error: {e}")

# ── Startup / Shutdown ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global ibkr, social, trader, _loop

    # Capturer la boucle asyncio pour les broadcasts thread-safe
    _loop = asyncio.get_event_loop()

    log.info("🚀 Démarrage NINABOT backend")

    # 1. IBKR en premier (priorité absolue)
    ibkr = IBKRApp(risk, on_ibkr_update)
    ibkr.connect_and_run()

    # 2. Attente connexion IBKR (max 8 secondes)
    for i in range(16):
        if ibkr.connected and ibkr.next_order_id is not None:
            break
        await asyncio.sleep(0.5)

    if ibkr.connected:
        log.info("✅ IBKR connecté — streaming actif")
    else:
        log.warning("⚠ IBKR non connecté au démarrage — reconnexion auto active")

    # 3. Social Monitor (non-bloquant)
    try:
        social = SocialMonitor()
        social.start()
    except Exception as e:
        log.warning(f"SocialMonitor start error: {e}")

    # 4. Auto Trader
    trader = AutoTrader(ibkr, risk)

    # 5. Thread reconnexion automatique
    t = threading.Thread(target=_reconnect_loop, daemon=True, name="reconnect")
    t.start()
    log.info("♻ Reconnexion automatique activée (toutes les 15s)")

    yield  # ← app tourne ici

    # Shutdown propre
    log.info("🛑 Arrêt")
    if trader and trader.active:
        trader.stop()
    if social:
        try: social.stop()
        except Exception: pass
    if ibkr:
        ibkr.disconnect_clean()


# ── FastAPI app ───────────────────────────────────────────────
app = FastAPI(
    title    = "NINABOT v3 API",
    version  = "3.0.0",
    lifespan = lifespan,
)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# MODÈLES PYDANTIC
# ═══════════════════════════════════════════════════════════════

class OrderRequest(BaseModel):
    symbol:       str
    sec_type:     str   = "STK"
    exchange:     str   = "SMART"
    currency:     str   = "USD"
    action:       str   = "BUY"
    qty:          float = 1.0
    order_type:   str   = "MKT"
    limit_price:  float = 0.0
    stop_price:   float = 0.0
    outside_rth:  bool  = True
    expiry:       str   = ""
    primary_exch: str   = ""
    tp_price:     float = 0.0
    sl_price:     float = 0.0

class AutoTradeRequest(BaseModel):
    mode:              str  = "trend"
    active:            bool = True
    instruments:       list = []
    active_indicators: dict = {}
    cfg:               dict = {}

class ClosePositionRequest(BaseModel):
    symbol:      str
    sec_type:    str   = "STK"
    exchange:    str   = "SMART"
    currency:    str   = "USD"
    qty:         float = 0.0
    primary_exch:str   = ""


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # Nettoyage des connexions mortes avant d'en ajouter une nouvelle
    dead = set()
    for c2 in list(clients):
        try:
            await c2.send_text('{"type":"ping"}')
        except Exception:
            dead.add(c2)
    clients.difference_update(dead)
    clients.add(ws)
    log.info(f"Client WS connecté ({len(clients)} actifs, {len(dead)} nettoyés)")

    # Snapshot immédiat
    if ibkr:
        try:
            await ws.send_text(json.dumps(ibkr.get_snapshot(), default=str))
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                cmd = json.loads(raw)
                action = cmd.get("action", "")

                if action == "subscribe_price" and ibkr and ibkr.connected:
                    ibkr.subscribe_price(
                        cmd["symbol"], cmd["sec_type"],
                        cmd.get("exchange", "SMART"), cmd["currency"],
                        cmd.get("primary_exch", "")
                    )
                elif action == "unsubscribe_price" and ibkr:
                    ibkr.unsubscribe_price(
                        cmd["symbol"], cmd["sec_type"], cmd["currency"]
                    )
                elif action == "ping":
                    await ws.send_text(json.dumps({"type":"pong","ts":time.time()}))

            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.debug(f"WS cmd error: {e}")

    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info(f"Client WS déconnecté ({len(clients)} restants)")


# ═══════════════════════════════════════════════════════════════
# REST ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "ok":      True,
        "ibkr":    ibkr.connected if ibkr else False,
        "account": settings.IBKR_ACCOUNT,
        "nav":     ibkr._nav if ibkr else 0,
        "auto":    trader.get_status() if trader else None,
    }

@app.get("/snapshot")
async def snapshot():
    if not ibkr:
        raise HTTPException(503, "IBKR non initialisé")
    return ibkr.get_snapshot()

@app.get("/instruments")
async def instruments():
    if not ibkr:
        return {"total":0,"available":0,"prices":{}}
    prices = ibkr.get_all_prices()
    nav    = ibkr._nav
    bp     = ibkr._buying_power
    avail  = sum(1 for p in prices.values() if p.get("price",0) > 0)
    margin_ok  = sum(1 for p in prices.values()
                     if p.get("price",0) > 0 and bp >= p.get("price",0))
    margin_nok = avail - margin_ok
    return {
        "total": len(prices), "available": avail,
        "margin_ok": margin_ok, "margin_nok": margin_nok,
        "nav": nav, "buying_power": bp, "prices": prices,
    }

# ── Ordres ────────────────────────────────────────────────────

@app.post("/order")
async def place_order(req: OrderRequest):
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    sym = req.symbol.upper()
    action = req.action.upper()

    # Bracket order
    if req.tp_price > 0 and req.sl_price > 0:
        r = ibkr.place_order(
            symbol=sym, sec_type=req.sec_type, exchange=req.exchange,
            currency=req.currency, action=action, qty=req.qty,
            order_type=req.order_type, limit_price=req.limit_price,
            outside_rth=req.outside_rth, primary_exch=req.primary_exch,
        )
        if not r.get("ok"):
            raise HTTPException(400, r.get("error"))
        # TP
        tp_action = "SELL" if action == "BUY" else "BUY"
        ibkr.place_order(symbol=sym, sec_type=req.sec_type, exchange=req.exchange,
            currency=req.currency, action=tp_action, qty=req.qty,
            order_type="LMT", limit_price=req.tp_price, outside_rth=req.outside_rth,
            primary_exch=req.primary_exch)
        # SL
        ibkr.place_order(symbol=sym, sec_type=req.sec_type, exchange=req.exchange,
            currency=req.currency, action=tp_action, qty=req.qty,
            order_type="STP", stop_price=req.sl_price, outside_rth=req.outside_rth,
            primary_exch=req.primary_exch)
        return {**r, "bracket": True}

    r = ibkr.place_order(
        symbol=sym, sec_type=req.sec_type, exchange=req.exchange,
        currency=req.currency, action=action, qty=req.qty,
        order_type=req.order_type, limit_price=req.limit_price,
        stop_price=req.stop_price, outside_rth=req.outside_rth,
        expiry=req.expiry, primary_exch=req.primary_exch,
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error","Ordre rejeté"))
    return r

@app.delete("/order/{order_id}")
async def cancel_order(order_id: int):
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    r = ibkr.cancel_order(order_id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error"))
    return r

@app.delete("/orders/all")
async def cancel_all():
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    active = [o for o in ibkr._orders.values()
              if o.status not in ("Filled","Cancelled","Inactive")]
    results = [ibkr.cancel_order(o.order_id) for o in active]
    return {"cancelled": len(results)}

@app.post("/position/close")
async def close_position(req: ClosePositionRequest):
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    sym = req.symbol.upper()
    pos = ibkr._positions.get(sym)
    if not pos or pos.position == 0:
        raise HTTPException(404, f"Pas de position pour {sym}")
    # Utiliser la quantité exacte de la position IBKR (pas celle envoyée par le frontend)
    qty    = abs(pos.position)  # toujours fermer la totalité proprement
    action = "SELL" if pos.position > 0 else "BUY"
    log.info(f"Fermeture position: {action} {qty} {sym} (position IBKR: {pos.position})")
    r = ibkr.place_order(symbol=sym, sec_type=pos.sec_type, exchange=req.exchange,
        currency=pos.currency, action=action, qty=qty,
        order_type="MKT", outside_rth=True, primary_exch=req.primary_exch)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error"))
    return {**r, "closed_qty": qty, "position_was": pos.position}

@app.post("/positions/close_all")
async def close_all_positions():
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    results = []
    already_sent = set()  # anti-doublons
    for sym, pos in list(ibkr._positions.items()):
        if pos.position == 0: continue
        if sym in already_sent: continue  # skip doublons
        already_sent.add(sym)
        qty    = abs(pos.position)
        action = "SELL" if pos.position > 0 else "BUY"
        log.info(f"Fermeture position: {action} {qty} {sym}")
        r = ibkr.place_order(symbol=sym, sec_type=pos.sec_type,
            exchange="SMART", currency=pos.currency,
            action=action, qty=qty,
            order_type="MKT", outside_rth=True)
        results.append({"symbol":sym,**r})
    return {"closed":len(results),"results":results}

# ── Auto-Trading ──────────────────────────────────────────────

@app.post("/auto/start")
async def auto_start(req: AutoTradeRequest):
    if not ibkr or not ibkr.connected:
        raise HTTPException(503, "IBKR non connecté")
    if not trader:
        raise HTTPException(503, "Trader non initialisé")
    if risk.killed:
        raise HTTPException(403, f"Kill switch: {risk.kill_reason}")
    sent = {}
    if social:
        try:
            raw = social.get_sentiment_scores()
            sent = {k: v.get("score",0) for k,v in raw.items()}
        except Exception: pass
    trader.start(mode=req.mode, cfg=req.cfg,
                 instruments=req.instruments,
                 active_inds=req.active_indicators,
                 sentiment=sent)
    return {"ok":True,"mode":req.mode,**trader.get_status()}

@app.post("/auto/stop")
async def auto_stop():
    if trader and trader.active:
        trader.stop()
    return {"ok":True,"active":False}

@app.get("/auto/status")
async def auto_status():
    return trader.get_status() if trader else {"active":False}

@app.post("/auto/close_all")
async def auto_close_all():
    if not trader:
        raise HTTPException(503, "Trader non initialisé")
    closed = []
    for oid, info in list(trader.open_auto_orders.items()):
        sym    = info.get("symbol","")
        action = "SELL" if info.get("action") == "BUY" else "BUY"
        r = ibkr.place_order(symbol=sym, sec_type=info.get("sec_type","STK"),
            exchange=info.get("exchange","SMART"), currency=info.get("currency","USD"),
            action=action, qty=info.get("qty",1),
            order_type="MKT", outside_rth=True)
        closed.append({"symbol":sym,**r})
    trader.open_auto_orders.clear()
    return {"closed":len(closed),"results":closed}

# ── Performance ───────────────────────────────────────────────

@app.get("/performance")
async def performance():
    if not ibkr:
        return {}
    orders = list(ibkr._orders.values())
    filled = [o for o in orders if o.status == "Filled"]
    last_3 = filled[-3:]
    sh2ma  = 0.0
    if len(last_3) >= 2:
        sh2ma = sum(o.avg_fill * o.qty for o in last_3) / len(last_3)
    wins  = sum(1 for o in filled if o.avg_fill > 0)
    total = len(filled)
    wr    = round(wins/total*100,1) if total > 0 else 0
    nav   = ibkr._nav
    unr   = ibkr._unrealized
    pct   = round(unr/nav*100,2) if nav > 0 else 0
    return {
        "nav": nav, "unrealized_pnl": unr, "pnl_pct": pct,
        "total_orders": total, "win_rate": wr,
        "wins": wins, "losses": total-wins,
        "sh2ma": round(sh2ma,2),
        "last_3": [o.to_dict() for o in last_3],
        "auto_perf": trader.performance if trader else {},
        "flash_info": {
            "pnl_change_pct": pct,
            "label": f"P&L {'+' if pct>=0 else ''}{pct:.2f}%",
            "color": "green" if pct >= 0 else "red",
        },
    }

# ── Social ────────────────────────────────────────────────────

@app.get("/social/feed")
async def social_feed(limit: int = 20):
    if not social:
        return {"feed":[],"sentiment":{}}
    try:
        feed = await asyncio.to_thread(social.get_feed, limit)
        sent = await asyncio.to_thread(social.get_sentiment_scores)
        if trader and sent:
            trader.update_sentiment({k:v.get("score",0) for k,v in sent.items()})
        return {"feed":feed,"sentiment":sent}
    except Exception as e:
        log.warning(f"Social feed error: {e}")
        return {"feed":[],"sentiment":{}}

# ── Scan manuel ──────────────────────────────────────────────────

@app.post("/warmup")
async def warmup():
    """Pré-charge les prix via Yahoo Finance pour tous les instruments configurés."""
    if not trader or not trader.instruments:
        # Warm-up sur les instruments IBKR abonnés
        inst_list = []
        if ibkr:
            for key, pdata in ibkr._prices.items():
                parts = key.split("_")
                if len(parts) >= 3:
                    inst_list.append({
                        "sym": parts[0], "type": parts[1], "cur": parts[2]
                    })
        if not inst_list:
            return {"ok": False, "msg": "Aucun instrument abonné"}
    else:
        inst_list = trader.instruments

    result = await asyncio.to_thread(
        warm_up_prices, inst_list,
        ibkr._prices if ibkr else {},
        None,
        ibkr._push if ibkr else None,
    )
    return {"ok": True, "prices_loaded": len(result),
            "sample": dict(list(result.items())[:5])}

@app.get("/ticker")
async def get_ticker():
    """
    Retourne les prix et variations réelles pour le ticker bar.
    Source prioritaire : IBKR live → YF si 0
    """
    # Les 20 symboles les plus importants pour le ticker
    TICKER_SYMS = [
        "NVDA","AAPL","MSFT","TSLA","AMZN","META","GOOG","AMD",
        "NFLX","JPM","BAC","INTC","ORCL","CRM","ADBE","PYPL",
        "BTC","ETH","SPY","QQQ",
    ]
    result = []
    need_yf  = []

    for sym in TICKER_SYMS:
        # Chercher dans IBKR d'abord
        ibkr_price = 0.0
        ibkr_chg   = 0.0
        if ibkr:
            for key, pdata in ibkr._prices.items():
                if pdata.get("sym") == sym or key.startswith(f"{sym}_"):
                    ibkr_price = pdata.get("price", 0)
                    ibkr_chg   = pdata.get("change_pct", 0)
                    break

        if ibkr_price > 0 and ibkr_chg != 0:
            result.append({"sym": sym, "price": ibkr_price, "chg": ibkr_chg, "src": "ibkr"})
        else:
            need_yf.append({"sym": sym, "ibkr_price": ibkr_price})

    # Yahoo Finance pour les manquants
    if need_yf:
        try:
            from price_feed import get_yf_prices_batch, YF_MAP
            import yfinance as yf

            yf_syms = {d["sym"]: YF_MAP.get(d["sym"], d["sym"]) for d in need_yf}
            yf_list  = list(set(yf_syms.values()))

            tickers_data = yf.Tickers(" ".join(yf_list))
            for d in need_yf:
                sym    = d["sym"]
                yf_sym = yf_syms.get(sym, sym)
                price  = d["ibkr_price"]
                chg    = 0.0
                try:
                    tk   = tickers_data.tickers.get(yf_sym) or yf.Ticker(yf_sym)
                    info = tk.fast_info
                    p    = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                    prev = getattr(info, "previous_close", None) or getattr(info, "regular_market_previous_close", None)
                    if p and p > 0:
                        price = float(p)
                    if price > 0 and prev and prev > 0:
                        chg = round((price - float(prev)) / float(prev) * 100, 2)
                except Exception:
                    pass
                result.append({"sym": sym, "price": price, "chg": chg,
                                "src": "yf" if price > 0 else "none"})
        except Exception as e:
            log.warning(f"Ticker YF error: {e}")
            for d in need_yf:
                result.append({"sym": d["sym"], "price": d["ibkr_price"], "chg": 0.0, "src": "cache"})

    return {"tickers": result, "count": len(result)}

@app.get("/market/status")
async def market_status():
    """Statut des marchés par session."""
    return {
        "US":     "OUVERT" if is_market_open("US")     else "FERMÉ",
        "EU":     "OUVERT" if is_market_open("EU")     else "FERMÉ",
        "CRYPTO": "OUVERT",
        "prices_cached": len(cache_all()),
    }

@app.post("/scan/trigger")
async def scan_trigger():
    """Force un scan immédiat sur tous les instruments configurés."""
    if not trader or not trader.active:
        # Démarrer un scan one-shot même si auto-trader inactif
        if not ibkr or not ibkr.connected:
            raise HTTPException(503, "IBKR non connecté")
        # Faire un scan one-shot
        from auto_trader import SignalEngine, PriceHistory, MODE_CFG
        results = []
        cfg = MODE_CFG.get("trend", {})
        engine = SignalEngine("trend", cfg, {}, {})
        for key, pdata in list(ibkr._prices.items())[:20]:
            price = pdata.get("price", 0)
            if price <= 0:
                continue
            sym = pdata.get("sym", key.split("_")[0])
            h = PriceHistory(50)
            h.seed(price, 50)
            result = engine.score(sym, h)
            results.append({"sym": sym, "price": price, **result})
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        await _broadcast({"type":"auto_log","entry":{
            "ts": __import__("time").strftime("%H:%M:%S"),
            "msg": f"Scan one-shot: {len(results)} instruments · top={results[0]['sym'] if results else '—'} score={results[0].get('score',0) if results else 0}",
            "level":"info"
        }})
        return {"ok": True, "signals": len([r for r in results if r.get("score",0)>=45]), "results": results[:10]}
    # Auto-trader actif: forcer un scan maintenant
    if trader._thread and trader._thread.is_alive():
        trader._stop_event.set()  # Interrompt le sleep
        trader._stop_event.clear()
    return {"ok": True, "signals": 0, "msg": "Scan forcé via auto-trader"}

@app.get("/scan/status")
async def scan_status():
    """Debug complet: état de tout le système."""
    prices_with_data = {k: v for k, v in ibkr._prices.items() if v.get("price", 0) > 0} if ibkr else {}
    return {
        "ibkr_connected":  ibkr.connected if ibkr else False,
        "nav":             ibkr._nav if ibkr else 0,
        "buying_power":    ibkr._buying_power if ibkr else 0,
        "prices_total":    len(ibkr._prices) if ibkr else 0,
        "prices_with_data":len(prices_with_data),
        "prices_sample":   dict(list(prices_with_data.items())[:5]),
        "auto_active":     trader.active if trader else False,
        "auto_mode":       trader.mode if trader else None,
        "auto_instruments":len(trader.instruments) if trader else 0,
        "auto_logs_last5": list(trader.logs)[-5:] if trader else [],
        "ws_clients":      len(clients),
        "retry_queue":     len(trader._retry_queue) if trader else 0,
    }

# ── Risk ──────────────────────────────────────────────────────

@app.post("/risk/kill")
async def kill():
    risk.manual_kill()
    if trader: trader.stop()
    return {"killed":True}

@app.post("/risk/reset")
async def reset(confirm:str=""):
    return {"ok": risk.manual_reset(confirm), "killed": risk.killed}

@app.get("/risk/status")
async def risk_status():
    return risk.to_dict()


# ── Démarrage serveur ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host   = settings.API_HOST,
        port   = settings.API_PORT,
        reload = False,
        log_level = "info",
    )
