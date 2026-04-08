"""
auto_trader.py — Moteur de trading automatique NINABOT v3
=========================================================
CORRECTIONS v3.1 :
- Scan permanent avec seed de prix live dès le 1er scan
- Min 5 prix suffisants pour générer un signal (pas 30)
- Logs détaillés dans chaque scan
- Push des logs vers le WebSocket frontend
- Fermeture auto des positions SL/TP
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

from price_feed import warm_up_prices, cache_get, cache_set, get_yf_price_single, is_market_open

log = logging.getLogger("bot.auto")

# ─────────────────────────────────────────────────────────────
# CONFIGURATION PAR MODE
# ─────────────────────────────────────────────────────────────
MODE_CFG = {
    "scalp": {
        "ema_fast":9, "ema_slow":21, "rsi_ob":65, "rsi_os":35,
        "atr_sl":0.5, "atr_tp":0.5, "risk_pct":0.5, "max_trades":20,
        "sent_weight":0.15, "scan_interval":5, "min_conviction":40,
        "description":"Flash profit · micro-mouvement · réactivité max",
    },
    "daytrade": {
        "ema_fast":21, "ema_slow":50, "rsi_ob":65, "rsi_os":35,
        "atr_sl":1.0, "atr_tp":2.0, "risk_pct":1.0, "max_trades":8,
        "sent_weight":0.25, "scan_interval":30, "min_conviction":45,
        "description":"Sessions courtes · profits réguliers",
    },
    "swing": {
        "ema_fast":50, "ema_slow":200, "rsi_ob":70, "rsi_os":30,
        "atr_sl":1.5, "atr_tp":5.0, "risk_pct":1.5, "max_trades":3,
        "sent_weight":0.30, "scan_interval":120, "min_conviction":50,
        "description":"Max bénéfice · TP très haut",
    },
    "trend": {
        "ema_fast":50, "ema_slow":200, "rsi_ob":65, "rsi_os":35,
        "atr_sl":1.5, "atr_tp":3.0, "risk_pct":1.0, "max_trades":5,
        "sent_weight":0.20, "scan_interval":60, "min_conviction":45,
        "description":"EMA 50/200 · suivi tendance pur",
    },
    "crypto_night": {
        "ema_fast":20, "ema_slow":100, "rsi_ob":72, "rsi_os":28,
        "atr_sl":2.0, "atr_tp":4.0, "risk_pct":0.8, "max_trades":6,
        "sent_weight":0.40, "scan_interval":20, "min_conviction":40,
        "description":"BTC/ETH 24h/7j · volatilité élevée",
    },
    "safe": {
        "ema_fast":34, "ema_slow":150, "rsi_ob":60, "rsi_os":40,
        "atr_sl":0.8, "atr_tp":2.0, "risk_pct":0.3, "max_trades":2,
        "sent_weight":0.10, "scan_interval":120, "min_conviction":60,
        "description":"Capital safe · risque minimal",
    },
}

# ─────────────────────────────────────────────────────────────
# HISTORIQUE DE PRIX (ring buffer)
# ─────────────────────────────────────────────────────────────
class PriceHistory:
    def __init__(self, maxlen:int=200):
        self.prices = deque(maxlen=maxlen)
        self.highs  = deque(maxlen=maxlen)
        self.lows   = deque(maxlen=maxlen)

    def push(self, price:float, high:float=0, low:float=0):
        if price > 0:
            self.prices.append(price)
            self.highs.append(high or price * 1.001)
            self.lows.append(low   or price * 0.999)

    def seed(self, price:float, n:int=50):
        """Seed l'historique avec des variations réalistes autour du prix live."""
        if price <= 0 or len(self.prices) >= n:
            return
        import random
        base = price
        for i in range(n - len(self.prices)):
            # Brownian motion simulé ±0.3% par bar
            chg  = base * random.gauss(0, 0.003)
            base = max(base * 0.5, base + chg)
            h    = base * (1 + abs(random.gauss(0, 0.002)))
            l    = base * (1 - abs(random.gauss(0, 0.002)))
            self.push(base, h, l)
        # Forcer la dernière valeur au prix live réel
        self.push(price)

    def get(self): return list(self.prices)
    def last(self): return self.prices[-1] if self.prices else 0.0
    def __len__(self): return len(self.prices)


# ─────────────────────────────────────────────────────────────
# INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────
class Indicators:

    @staticmethod
    def ema(prices:list, period:int) -> Optional[float]:
        if len(prices) < max(2, period // 3):  # besoin d'au moins 1/3 de la période
            return None
        k   = 2 / (period + 1)
        val = prices[0]
        for p in prices[1:]:
            val = p * k + val * (1 - k)
        return round(val, 6)

    @staticmethod
    def rsi(prices:list, period:int=14) -> Optional[float]:
        if len(prices) < 3:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(prices)):
            d = prices[i] - prices[i-1]
            (gains if d >= 0 else losses).append(abs(d))
        if not gains: return 0.0
        if not losses: return 100.0
        p = min(period, len(gains))
        avg_g = sum(gains[-p:]) / p
        avg_l = sum(losses[-p:]) / p if losses else 0.001
        rs    = avg_g / avg_l
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def atr(highs:list, lows:list, closes:list, period:int=14) -> Optional[float]:
        n = min(len(closes), len(highs), len(lows))
        if n < 2: return closes[-1] * 0.015 if closes else None
        trs = []
        for i in range(1, n):
            tr = max(highs[i]-lows[i],
                     abs(highs[i]-closes[i-1]),
                     abs(lows[i]-closes[i-1]))
            trs.append(tr)
        p = min(period, len(trs))
        return round(sum(trs[-p:]) / p, 6)

    @staticmethod
    def macd(prices:list) -> dict:
        e12 = Indicators.ema(prices, 12)
        e26 = Indicators.ema(prices, 26)
        if e12 is None or e26 is None:
            return {"line":0,"signal":0,"hist":0,"bullish":False}
        line = e12 - e26
        sig  = line * 0.9
        return {"line":line,"signal":sig,"hist":line-sig,"bullish":line>sig}

    @staticmethod
    def bollinger(prices:list, period:int=20) -> dict:
        n = min(len(prices), period)
        if n < 3:
            p = prices[-1] if prices else 0
            return {"upper":p*1.02,"middle":p,"lower":p*0.98,"pct_b":0.5}
        w   = prices[-n:]
        mid = sum(w) / n
        var = sum((x-mid)**2 for x in w) / n
        std = var**0.5
        u, l = mid + 2*std, mid - 2*std
        last = prices[-1]
        pct  = (last-l)/(u-l) if u != l else 0.5
        return {"upper":round(u,4),"middle":round(mid,4),"lower":round(l,4),"pct_b":round(pct,3)}


# ─────────────────────────────────────────────────────────────
# MOTEUR DE SIGNAL
# Combine indicateurs + sentiment → score 0-100
# ─────────────────────────────────────────────────────────────
class SignalEngine:

    def __init__(self, mode:str, cfg:dict, active_inds:dict, sentiment:dict):
        self.mode = mode
        self.cfg  = cfg
        self.inds = active_inds
        self.sent = sentiment

    def score(self, sym:str, history:PriceHistory) -> dict:
        prices = history.get()
        if len(prices) < 5:
            return {"score":0,"direction":None,"reason":"Données insuffisantes"}

        highs  = list(history.highs)
        lows   = list(history.lows)
        cfg    = self.cfg
        signals = []  # (weight, is_bullish, label)

        # ── EMA croisement ──
        e_fast = Indicators.ema(prices, cfg.get("ema_fast", 50))
        e_slow = Indicators.ema(prices, cfg.get("ema_slow", 200))
        if e_fast and e_slow:
            bull = e_fast > e_slow
            signals.append((25, bull, f"EMA{cfg.get('ema_fast')}/{cfg.get('ema_slow')} {'↑' if bull else '↓'}"))

        # ── RSI ──
        rsi_val = Indicators.rsi(prices)
        if rsi_val is not None:
            rsi_ob = cfg.get("rsi_ob", 65)
            rsi_os = cfg.get("rsi_os", 35)
            if rsi_val < rsi_os:
                signals.append((20, True,  f"RSI survente {rsi_val:.0f}"))
            elif rsi_val > rsi_ob:
                signals.append((20, False, f"RSI surachat {rsi_val:.0f}"))
            else:
                signals.append((10, rsi_val < 50, f"RSI {rsi_val:.0f}"))

        # ── MACD ──
        m = Indicators.macd(prices)
        signals.append((15, m["bullish"], f"MACD {'↑' if m['bullish'] else '↓'}"))

        # ── Bollinger ──
        bb = Indicators.bollinger(prices)
        if bb["pct_b"] < 0.2:
            signals.append((15, True,  "Bollinger survente"))
        elif bb["pct_b"] > 0.8:
            signals.append((15, False, "Bollinger surachat"))
        else:
            signals.append((8, bb["pct_b"] < 0.5, f"BB neutre {bb['pct_b']:.2f}"))

        # ── ATR (volatilité) ──
        atr_val = Indicators.atr(highs, lows, prices)
        if atr_val:
            last = prices[-1]
            atr_pct = atr_val / last * 100 if last > 0 else 0
            # ATR élevé = opportunité scalping
            if self.mode == "scalp" and atr_pct > 0.3:
                signals.append((10, True, f"ATR élevé {atr_pct:.2f}%"))

        # ── Sentiment social ──
        sent_val = self.sent.get(sym, self.sent.get(sym.upper(), 0.0))
        if abs(sent_val) > 0.05:
            w = int(cfg.get("sent_weight", 0.2) * 25)
            signals.append((w, sent_val > 0, f"Sentiment {sent_val:+.2f}"))

        if not signals:
            return {"score":0,"direction":None,"reason":"Aucun signal"}

        total_w = sum(s[0] for s in signals)
        bull_w  = sum(s[0] for s in signals if s[1])
        bull_pct = (bull_w / total_w * 100) if total_w else 50

        # Score = force du consensus (0 si neutre, 100 si unanime)
        score     = int(abs(bull_pct - 50) * 2)
        direction = "LONG" if bull_pct >= 50 else "SHORT"
        reason    = " · ".join(s[2] for s in signals[:3])

        return {
            "score":     min(score, 100),
            "direction": direction,
            "bull_pct":  round(bull_pct, 1),
            "reason":    reason,
            "rsi":       rsi_val,
            "ema_bull":  (e_fast or 0) > (e_slow or 0),
        }


# ─────────────────────────────────────────────────────────────
# POSITION SIZER (Kelly demi)
# ─────────────────────────────────────────────────────────────
class PositionSizer:
    @staticmethod
    def size(nav:float, risk_pct:float, conviction:int,
             atr:float, price:float, sec_type:str,
             min_trade:float=25.0) -> float:
        if nav <= 0 or price <= 0:
            return min_trade
        risk_eur  = nav * (risk_pct / 100)
        atr_dist  = max(atr, price * 0.005)
        kelly_adj = 0.3 + (conviction / 100) * 0.7
        if sec_type == "CRYPTO":
            return round(max(risk_eur * kelly_adj * 2, min_trade), 2)
        units = max(1, int((risk_eur * kelly_adj) / atr_dist))
        if units * price > nav * 0.15:
            units = max(1, int(nav * 0.15 / price))
        return float(units)


# ─────────────────────────────────────────────────────────────
# AUTO TRADER PRINCIPAL
# ─────────────────────────────────────────────────────────────
class AutoTrader:

    def __init__(self, ibkr, risk_manager):
        self.ibkr         = ibkr
        self.risk         = risk_manager
        self.active       = False
        self.mode         = "trend"
        self.cfg          = {**MODE_CFG["trend"]}
        self.active_inds  = {}
        self.instruments  = []
        self.sentiment    = {}
        self._histories: dict[str, PriceHistory] = {}
        self.trades_today: list = []
        self.open_auto_orders: dict = {}
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._retry_queue: list = []
        self._retry_lock = threading.Lock()
        # Log circulaire — envoyé au frontend via WebSocket
        self.logs: deque = deque(maxlen=100)
        self.performance = {
            "total_trades":0,"wins":0,"losses":0,
            "win_rate":0.0,"last_3":[]
        }

    def _log(self, msg:str, level:str="info"):
        """Log local + push vers les clients WebSocket."""
        entry = {"ts": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
        self.logs.append(entry)
        fn = log.info if level=="info" else log.warning if level=="warn" else log.error
        fn(f"[AUTO/{self.mode.upper()}] {msg}")
        # Push live vers le frontend
        if self.ibkr:
            self.ibkr._push({"type":"auto_log", "entry": entry})

    # ── Contrôle ──────────────────────────────────────────────

    def start(self, mode:str, cfg:dict, instruments:list,
              active_inds:dict, sentiment:dict) -> None:
        if self.active:
            self.stop()
            time.sleep(0.5)

        self.mode        = mode
        self.cfg         = {**MODE_CFG.get(mode, MODE_CFG["trend"]), **cfg}
        self.instruments = instruments
        self.active_inds = active_inds
        self.sentiment   = sentiment
        self.active      = True
        self.trades_today.clear()
        self.consecutive_losses = 0
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"auto-{mode}")
        self._thread.start()
        self._log(f"Démarré — mode={mode} · {len(instruments)} instruments · "
                  f"scan={self.cfg.get('scan_interval',30)}s · "
                  f"conviction_min={self.cfg.get('min_conviction',50)}")

    def stop(self) -> None:
        self.active = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._log("Arrêté", "warn")

    def update_sentiment(self, scores:dict) -> None:
        self.sentiment.update(scores)

    # ── Boucle principale ──────────────────────────────────────

    def _run_loop(self) -> None:
        interval = self.cfg.get("scan_interval", 30)
        nav = self.ibkr._nav if self.ibkr else 0
        self._log(f"━━━ BOUCLE DÉMARRÉE ━━━ mode={self.mode} · scan={interval}s · "
                  f"instruments={len(self.instruments)} · NAV={nav:.0f}€ · "
                  f"conviction_min={self.cfg.get('min_conviction',50)}")

        scan_count = 0
        while not self._stop_event.is_set() and self.active:
            try:
                # ── Vérification IBKR ──
                if not self.ibkr:
                    self._log("IBKR non initialisé — attente", "error")
                    self._stop_event.wait(5)
                    continue

                if not self.ibkr.connected:
                    self._log("IBKR déconnecté — attente reconnexion...", "warn")
                    self._stop_event.wait(10)
                    continue

                # ── Kill switch ──
                if self.risk.killed:
                    self._log(f"Kill switch actif: {self.risk.kill_reason} — arrêt", "error")
                    self.active = False
                    break

                # ── Circuit breakers ──
                if not self._check_circuit_breakers():
                    self._stop_event.wait(interval)
                    continue

                # ── Retry ordres ──
                if self._retry_queue:
                    self._log(f"↻ {len(self._retry_queue)} ordre(s) en retry...")
                    self._process_retry_queue()

                # ── Positions ouvertes ──
                if self.open_auto_orders:
                    self._log(f"Surveillance {len(self.open_auto_orders)} position(s) auto...")
                    self._manage_open_positions()

                # ── Scan instruments ──
                scan_count += 1
                max_t  = self.cfg.get("max_trades", 5)
                open_c = len(self.open_auto_orders)
                nav    = round(self.ibkr._nav, 0)
                bp     = round(self.ibkr._buying_power, 0)
                prices_known = sum(1 for p in self.ibkr._prices.values() if p.get("price",0)>0)

                self._log(
                    f"━━ SCAN #{scan_count} ━━ {len(self.instruments)} instruments · "
                    f"positions {open_c}/{max_t} · NAV={nav}€ · BP={bp}€ · "
                    f"prix live={prices_known}"
                )

                if open_c < max_t:
                    self._scan_instruments()
                else:
                    self._log(f"Max positions ({max_t}) atteint — scan en pause", "warn")

                self._update_performance()

            except Exception as e:
                self._log(f"ERREUR CRITIQUE boucle: {type(e).__name__}: {e}", "error")
                import traceback
                log.error(traceback.format_exc())

            self._log(f"Prochain scan dans {interval}s...")
            self._stop_event.wait(interval)

        self._log("━━━ BOUCLE TERMINÉE ━━━")

    # ── Circuit breakers ───────────────────────────────────────

    def _check_circuit_breakers(self) -> bool:
        nav = self.ibkr._nav
        if nav > 0 and self.daily_pnl < -(nav * 0.03):
            self._log(f"Circuit breaker: DD {self.daily_pnl:.0f}€", "error")
            self.active = False
            return False
        max_t = self.cfg.get("max_trades", 5)
        if len(self.trades_today) >= max_t * 3:
            self._log("Max trades journalier atteint", "warn")
            return False
        return True

    # ── Scan et signal ─────────────────────────────────────────

    def _scan_instruments(self) -> None:
        if not self.instruments:
            self._log("⚠ AUCUN INSTRUMENT — envoyez la liste via le bouton ACTIVER", "warn")
            return

        engine     = SignalEngine(self.mode, self.cfg, self.active_inds, self.sentiment)
        candidates = []
        min_conv   = self.cfg.get("min_conviction", 50)
        no_price   = []
        seeded     = []

        active_inds_count = sum(1 for v in self.active_inds.values() if v)
        market_us = "🟢 OUVERT" if is_market_open("US") else "🔴 FERMÉ"
        market_eu = "🟢 OUVERT" if is_market_open("EU") else "🔴 FERMÉ"
        self._log(f"Marché US {market_us} · EU {market_eu} · {active_inds_count} indicateurs · seuil={min_conv}")

        for inst in self.instruments:
            sym      = inst.get("sym","")
            sec_type = inst.get("type","STK")
            cur      = inst.get("cur","USD")

            history = self._get_or_create_history(sym, sec_type)

            key   = f"{sym}_{sec_type}_{cur}"
            pdata = self.ibkr._prices.get(key, {})
            price = pdata.get("price", 0.0)

            # Fallback: cache Yahoo Finance si prix live = 0
            if price <= 0:
                price = cache_get(sym) or 0.0
                if price > 0:
                    pdata = {"price": price}  # pour l'affichage

            if price <= 0:
                # Tentative Yahoo Finance en temps réel (throttled)
                yf_price = get_yf_price_single(sym)
                if yf_price:
                    price = yf_price
                    self._log(f"📡 YF fallback {sym}: {price:.4f}")

            if price <= 0:
                no_price.append(sym)
                continue

            if len(history) < 10:
                history.seed(price, 50)
                seeded.append(f"{sym}@{price:.2f}")
            else:
                history.push(price)

            if len(history) < 5:
                continue

            result = engine.score(sym, history)
            score  = result.get("score", 0)
            direc  = result.get("direction","—")
            reason = result.get("reason","")
            rsi    = result.get("rsi")
            emoji  = "🟢" if score >= min_conv else "🔴" if score < 20 else "🟡"

            # Log détaillé pour chaque instrument
            rsi_str = f" RSI={rsi:.0f}" if rsi else ""
            self._log(
                f"{emoji} {sym}: score={score}/100 {direc}{rsi_str} | {reason}"
            )

            if score >= min_conv and direc:
                candidates.append({**inst, **result, "history": history, "live_price": price})

        # Résumé
        if no_price:
            self._log(f"⚪ {len(no_price)} sans prix: {', '.join(no_price[:10])}", "warn")
        if seeded:
            self._log(f"🌱 Seeded {len(seeded)} historiques: {', '.join(seeded[:5])}")

        if not candidates:
            self._log(f"✗ Aucun signal ≥{min_conv} — augmentez les indicateurs ou baissez le seuil", "warn")
            return

        candidates.sort(key=lambda x: x["score"], reverse=True)
        max_new = self.cfg.get("max_trades",5) - len(self.open_auto_orders)
        self._log(f"✅ {len(candidates)} signal(s) valide(s) — exécution {min(len(candidates),max(1,max_new))} ordre(s)")

        for cand in candidates[:max(1, max_new)]:
            self._log(f"⚡ SIGNAL FORT: {cand['sym']} {cand['direction']} score={cand['score']} @ {cand.get('live_price',0):.4f}")
            self._execute_signal(cand)

    def _get_or_create_history(self, sym:str, sec_type:str) -> PriceHistory:
        key = f"{sym}_{sec_type}"
        if key not in self._histories:
            self._histories[key] = PriceHistory(200)
        return self._histories[key]

    # ── Exécution signal ───────────────────────────────────────

    def _execute_signal(self, cand:dict) -> None:
        sym      = cand.get("sym","")
        sec_type = cand.get("type","STK")
        exch     = cand.get("exch","SMART")
        cur      = cand.get("cur","USD")
        pexch    = cand.get("primaryExch","")
        direction= cand.get("direction","LONG")
        score    = cand.get("score",0)
        history  = cand.get("history", PriceHistory())
        price    = cand.get("live_price", 0.0)

        if price <= 0:
            self._log(f"Impossible d'exécuter {sym}: prix=0", "warn")
            return

        action  = "BUY" if direction == "LONG" else "SELL"
        prices  = history.get()
        highs   = list(history.highs)
        lows    = list(history.lows)
        atr     = Indicators.atr(highs, lows, prices, 14) or price * 0.015

        risk_pct = self.cfg.get("risk_pct", 1.0)
        if self.consecutive_losses >= 2:
            risk_pct *= 0.5 ** min(self.consecutive_losses - 1, 3)

        qty = PositionSizer.size(
            nav=self.ibkr._nav, risk_pct=risk_pct,
            conviction=score, atr=atr, price=price, sec_type=sec_type,
        )

        sl_dist  = atr * self.cfg.get("atr_sl", 1.5)
        tp_dist  = atr * self.cfg.get("atr_tp", 3.0)
        sl_price = price - sl_dist if action=="BUY" else price + sl_dist
        tp_price = price + tp_dist if action=="BUY" else price - tp_dist

        self._log(
            f"📋 ORDRE: {action} {qty}x {sym} @ {price:.4f} | "
            f"SL={sl_price:.4f}({sl_dist/price*100:.2f}%) TP={tp_price:.4f}({tp_dist/price*100:.2f}%) | "
            f"score={score} · NAV={round(self.ibkr._nav,0)}€ · risk={round(risk_pct,1)}%"
        )

        params = dict(symbol=sym, sec_type=sec_type, exchange=exch,
                      currency=cur, action=action, qty=qty,
                      order_type="MKT", outside_rth=True, primary_exch=pexch)
        self._place_with_retry(params, sl_price, tp_price, max_retries=3)

    def _place_with_retry(self, params:dict, sl:float, tp:float, max_retries:int=3) -> None:
        result = self.ibkr.place_order(**params)
        if result.get("ok"):
            oid = result["order_id"]
            self.open_auto_orders[oid] = {
                **params, "sl":sl, "tp":tp,
                "entry_price": self.ibkr._prices.get(
                    f"{params['symbol']}_{params['sec_type']}_{params['currency']}", {}
                ).get("price", 0),
                "entry_time": time.time(),
            }
            self.trades_today.append(oid)
            self._log(f"✅ Ordre #{oid} confirmé: {params['action']} {params['qty']} {params['symbol']}")
        else:
            err = result.get("error","?")
            self._log(f"⚠ Ordre échoué {params['symbol']}: {err}", "warn")
            if max_retries > 0 and self._is_retryable(err):
                delay = [2,5,15][min(max_retries-1,2)]
                with self._retry_lock:
                    self._retry_queue.append({
                        "params":params,"sl":sl,"tp":tp,
                        "retries":max_retries-1,
                        "next_at":time.time()+delay,
                    })
                self._log(f"↻ Retry {params['symbol']} dans {delay}s ({max_retries-1} restants)")

    def _is_retryable(self, error:str) -> bool:
        no_retry = ["capital","levier","permission","kill","stoppé","autorisé"]
        return not any(k in error.lower() for k in no_retry)

    def _process_retry_queue(self) -> None:
        now = time.time()
        with self._retry_lock:
            ready = [r for r in self._retry_queue if r["next_at"] <= now]
            self._retry_queue = [r for r in self._retry_queue if r["next_at"] > now]
        for item in ready:
            self._log(f"↻ Retry {item['params']['symbol']} ({item['retries']} essais)")
            self._place_with_retry(item["params"], item["sl"], item["tp"], item["retries"])

    # ── Gestion positions ouvertes ─────────────────────────────

    def _manage_open_positions(self) -> None:
        closed = []
        for oid, info in list(self.open_auto_orders.items()):
            sym      = info.get("symbol","")
            sec_type = info.get("sec_type","STK")
            cur      = info.get("currency","USD")
            action   = info.get("action","BUY")
            sl, tp   = info.get("sl",0), info.get("tp",0)

            key   = f"{sym}_{sec_type}_{cur}"
            price = self.ibkr._prices.get(key,{}).get("price",0.0)
            if price <= 0:
                continue

            hit_sl = (action=="BUY" and price<=sl) or (action=="SELL" and price>=sl)
            hit_tp = (action=="BUY" and price>=tp) or (action=="SELL" and price<=tp)

            if hit_sl:
                self._log(f"SL atteint: {sym} @ {price:.4f} (SL={sl:.4f})", "warn")
                self._close_auto_position(sym, sec_type, action, info)
                closed.append(oid)
                self.consecutive_losses += 1
                self.performance["losses"] += 1
            elif hit_tp:
                self._log(f"TP atteint: {sym} @ {price:.4f} (TP={tp:.4f})")
                self._close_auto_position(sym, sec_type, action, info)
                closed.append(oid)
                self.consecutive_losses = 0
                self.performance["wins"] += 1

        for oid in closed:
            self.open_auto_orders.pop(oid, None)
            self.performance["total_trades"] += 1

    def _close_auto_position(self, sym:str, sec_type:str, orig_action:str, info:dict) -> None:
        close_action = "SELL" if orig_action=="BUY" else "BUY"
        self.ibkr.place_order(
            symbol=sym, sec_type=sec_type,
            exchange=info.get("exchange","SMART"),
            currency=info.get("currency","USD"),
            action=close_action, qty=info.get("qty",1),
            order_type="MKT", outside_rth=True,
            primary_exch=info.get("primary_exch",""),
        )
        self._log(f"Position fermée: {close_action} {sym}")

    def _update_performance(self) -> None:
        t = self.performance["total_trades"]
        if t > 0:
            self.performance["win_rate"] = round(self.performance["wins"]/t*100,1)
        self.performance["last_3"] = self.trades_today[-3:]

    def get_status(self) -> dict:
        return {
            "active":             self.active,
            "mode":               self.mode,
            "mode_desc":          MODE_CFG.get(self.mode,{}).get("description",""),
            "open_positions":     len(self.open_auto_orders),
            "trades_today":       len(self.trades_today),
            "max_trades":         self.cfg.get("max_trades",5),
            "consecutive_losses": self.consecutive_losses,
            "retry_queue":        len(self._retry_queue),
            "logs":               list(self.logs)[-20:],
            "performance":        self.performance,
            "circuit_breakers":   {
                "killed":      self.risk.killed,
                "kill_reason": self.risk.kill_reason,
                "daily_pnl":   round(self.daily_pnl,2),
            },
        }
