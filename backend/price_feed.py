"""
price_feed.py — Récupération de prix avec fallback
===================================================
Priorité :
  1. Prix live IBKR (reqMktData streaming)
  2. Prix historique IBKR (reqHistoricalData snapshot)
  3. Yahoo Finance (fallback gratuit)
  4. Prix simulé ±0.3% Brownian (dernier recours)

Compatible marché ouvert ET fermé (week-end, nuit).
"""

import logging
import threading
import time
from datetime import datetime, date
from typing import Optional
import random

log = logging.getLogger("bot.prices")

# ─────────────────────────────────────────────────────────────
# Cache de prix global partagé entre ibkr_client et auto_trader
# ─────────────────────────────────────────────────────────────
_price_cache: dict = {}  # sym → {price, source, ts}
_cache_lock  = threading.Lock()
_yf_last_fetch: dict = {}  # sym → timestamp

def cache_set(sym: str, price: float, source: str = "live") -> None:
    with _cache_lock:
        _price_cache[sym] = {"price": price, "source": source, "ts": time.time()}

def cache_get(sym: str) -> Optional[float]:
    with _cache_lock:
        entry = _price_cache.get(sym)
        if entry and time.time() - entry["ts"] < 3600:  # valide 1h
            return entry["price"]
    return None

def cache_all() -> dict:
    with _cache_lock:
        return dict(_price_cache)


# ─────────────────────────────────────────────────────────────
# YAHOO FINANCE — Prix différé gratuit (15-20min)
# ─────────────────────────────────────────────────────────────

# Map des symboles IBKR → YF (certains diffèrent)
YF_MAP = {
    "AIR":"AIR.PA","MC":"MC.PA","TTE":"TTE.PA","SAN":"SAN.PA","BNP":"BNP.PA",
    "OR":"OR.PA","SU":"SU.PA","CAP":"CAP.PA","DG":"DG.PA","AXA":"CS.PA",
    "KER":"KER.PA","DSY":"DSY.PA","ENGI":"ENGI.PA","HO":"HO.PA","SG":"GLE.PA",
    "RI":"RI.PA","RMS":"RMS.PA","ORA":"ORA.PA","LR":"LR.PA","VIE":"DG.PA",
    "SAP":"SAP.DE","SIE":"SIE.DE","ALV":"ALV.DE","BMW":"BMW.DE",
    "VOW":"VOW3.DE","DBK":"DBK.DE","DTE":"DTE.DE","BAS":"BASF.DE",
    "ASML":"ASML.AS","INGA":"INGA.AS","PHIA":"PHIA.AS","WKL":"WKL.AS","ADYEN":"ADYEN.AS",
    "SHEL":"SHEL.L","HSBA":"HSBA.L","AZN":"AZN.L","BP":"BP.L","ULVR":"ULVR.L","RIO":"RIO.L",
    "NESN":"NESN.SW","ROG":"ROG.SW","NOVN":"NOVN.SW","UBSG":"UBS.SW",
    "BTC":"BTC-USD","ETH":"ETH-USD","LTC":"LTC-USD","BCH":"BCH-USD","XRP":"XRP-USD",
    "EUR":"EURUSD=X","GBP":"GBPUSD=X",
}

def get_yf_prices_batch(symbols: list) -> dict:
    """
    Récupère les prix Yahoo Finance pour une liste de symboles.
    Retourne {sym: price}
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance non installé — pip install yfinance")
        return {}

    results = {}
    # Convertir les symboles
    yf_syms = {sym: YF_MAP.get(sym, sym) for sym in symbols}
    yf_list  = list(set(yf_syms.values()))

    try:
        log.info(f"Yahoo Finance: récupération {len(yf_list)} prix...")
        tickers = yf.Tickers(" ".join(yf_list))

        for sym, yf_sym in yf_syms.items():
            try:
                tk = tickers.tickers.get(yf_sym)
                if tk is None:
                    tk = yf.Ticker(yf_sym)
                info = tk.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                if price and price > 0:
                    results[sym] = float(price)
                    cache_set(sym, float(price), "yahoo")
                    log.debug(f"YF {sym}={price:.4f}")
            except Exception as e:
                log.debug(f"YF error {sym}: {e}")

        log.info(f"Yahoo Finance: {len(results)}/{len(symbols)} prix récupérés")
    except Exception as e:
        log.error(f"Yahoo Finance batch error: {e}")

    return results


def get_yf_price_single(sym: str) -> Optional[float]:
    """Récupère un seul prix YF avec throttle (max 1/60s par symbole)."""
    now = time.time()
    if now - _yf_last_fetch.get(sym, 0) < 60:
        return cache_get(sym)
    _yf_last_fetch[sym] = now

    result = get_yf_prices_batch([sym])
    return result.get(sym)


# ─────────────────────────────────────────────────────────────
# IBKR HISTORICAL DATA — Snapshot 1min (marché fermé)
# ─────────────────────────────────────────────────────────────

class HistoricalDataHelper:
    """
    Demande des données historiques IBKR pour récupérer le dernier prix
    quand les données live ne sont pas disponibles.
    """

    def __init__(self, ibkr_app):
        self.ibkr = ibkr_app
        self._results: dict = {}   # reqId → list of bars
        self._events:  dict = {}   # reqId → threading.Event
        self._lock = threading.Lock()

    def get_last_price(self, sym: str, sec_type: str,
                       exchange: str, currency: str,
                       primary_exch: str = "",
                       timeout: float = 5.0) -> Optional[float]:
        """
        Demande reqHistoricalData et retourne le dernier close.
        Timeout: 5s max.
        """
        if not self.ibkr or not self.ibkr.connected:
            return None

        from ibapi.contract import Contract
        req_id = self.ibkr._next_ticker
        self.ibkr._next_ticker += 1

        c = Contract()
        c.symbol   = sym
        c.secType  = sec_type
        c.exchange = exchange
        c.currency = currency
        if primary_exch:
            c.primaryExch = primary_exch

        event = threading.Event()
        with self._lock:
            self._results[req_id] = []
            self._events[req_id]  = event

        try:
            self.ibkr.reqHistoricalData(
                req_id, c,
                "",          # endDateTime = now
                "2 D",       # 2 jours pour avoir au moins 1 bar
                "1 day",     # bar size
                "TRADES",
                0,           # useRTH=0 → inclut hors heures
                1,           # formatDate
                False,       # keepUpToDate
                [],
            )
        except Exception as e:
            log.debug(f"reqHistoricalData error {sym}: {e}")
            return None

        # Attendre la réponse
        got = event.wait(timeout=timeout)
        with self._lock:
            bars = self._results.pop(req_id, [])
            self._events.pop(req_id, None)

        if bars:
            last_close = bars[-1].get("close", 0)
            if last_close > 0:
                log.info(f"Historical {sym}: {last_close:.4f} (source=IBKR hist)")
                cache_set(sym, last_close, "ibkr_hist")
                return last_close

        return None

    def historicalData(self, reqId: int, bar) -> None:
        """Callback IBKR — à appeler depuis IBKRApp.historicalData()"""
        with self._lock:
            if reqId in self._results:
                self._results[reqId].append({
                    "date":  bar.date,
                    "open":  bar.open,
                    "high":  bar.high,
                    "low":   bar.low,
                    "close": bar.close,
                })

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        """Callback IBKR — fin des données historiques."""
        with self._lock:
            ev = self._events.get(reqId)
        if ev:
            ev.set()


# ─────────────────────────────────────────────────────────────
# WARM-UP — Charge tous les prix au démarrage du scan
# ─────────────────────────────────────────────────────────────

def warm_up_prices(instruments: list, ibkr_prices: dict,
                   hist_helper=None, push_fn=None) -> dict:
    """
    Charge les prix pour tous les instruments avant de démarrer le scan.
    Stratégie :
      1. Prix live IBKR déjà dans ibkr_prices
      2. Yahoo Finance (batch) pour les manquants
      3. IBKR historique pour les non trouvés sur YF (si hist_helper disponible)
    """
    prices = {}

    # 1. Récupérer prix live IBKR
    live_count = 0
    missing    = []
    for inst in instruments:
        sym  = inst.get("sym", "")
        stype= inst.get("type", "STK")
        cur  = inst.get("cur", "USD")
        key  = f"{sym}_{stype}_{cur}"
        live = ibkr_prices.get(key, {}).get("price", 0)
        if live > 0:
            prices[sym] = live
            cache_set(sym, live, "ibkr_live")
            live_count += 1
        else:
            # Essayer le cache
            cached = cache_get(sym)
            if cached:
                prices[sym] = cached
            else:
                missing.append(inst)

    msg = f"Prix IBKR live: {live_count}/{len(instruments)}"
    log.info(msg)
    if push_fn:
        push_fn({"type":"auto_log","entry":{"ts":time.strftime("%H:%M:%S"),
                 "msg":f"💰 {msg}","level":"info"}})

    # 2. Yahoo Finance pour les manquants
    if missing:
        missing_syms = [i.get("sym","") for i in missing if i.get("sym")]
        log.info(f"Récupération YF pour {len(missing_syms)} instruments manquants...")
        if push_fn:
            push_fn({"type":"auto_log","entry":{"ts":time.strftime("%H:%M:%S"),
                     "msg":f"📡 Yahoo Finance: {len(missing_syms)} prix à récupérer...","level":"info"}})
        yf_results = get_yf_prices_batch(missing_syms)
        prices.update(yf_results)
        yf_count = len(yf_results)
        msg2 = f"Yahoo Finance: {yf_count}/{len(missing_syms)} prix récupérés"
        log.info(msg2)
        if push_fn:
            push_fn({"type":"auto_log","entry":{"ts":time.strftime("%H:%M:%S"),
                     "msg":f"{'✅' if yf_count>0 else '⚠️'} {msg2}","level":"info"}})

    total = len(prices)
    pct   = round(total/max(len(instruments),1)*100)
    msg3  = f"Warm-up terminé: {total}/{len(instruments)} prix ({pct}%)"
    log.info(msg3)
    if push_fn:
        push_fn({"type":"auto_log","entry":{"ts":time.strftime("%H:%M:%S"),
                 "msg":f"🚀 {msg3} — scan en cours...","level":"info"}})

    return prices


# ─────────────────────────────────────────────────────────────
# IS MARKET OPEN
# ─────────────────────────────────────────────────────────────

def is_market_open(session: str = "US") -> bool:
    """Vérifie si le marché est ouvert (heure de Paris)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Paris"))
        dow = now.weekday()
        if dow >= 5:  # week-end
            return False
        h, m = now.hour, now.minute
        mins  = h * 60 + m
        if session == "US":
            return 15*60+30 <= mins <= 22*60   # 15h30-22h00 Paris
        elif session == "EU":
            return 9*60 <= mins <= 17*60+30    # 9h00-17h30 Paris
        elif session == "CRYPTO":
            return True  # 24/7
    except Exception:
        pass
    return True  # Par défaut : supposer ouvert
