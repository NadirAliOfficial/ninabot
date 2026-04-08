# ibkr_client.py — Compatible ibapi 9.81.1
# Prix temps réel, positions, ordres, contract details

import logging
import threading
from typing import Callable

from ibapi.client   import EClient
from ibapi.common   import TickerId, OrderId
from ibapi.contract import Contract
from ibapi.order    import Order
from ibapi.wrapper  import EWrapper

from config       import settings
from risk_manager import RiskManager

log = logging.getLogger("bot.ibkr")

# ── Taille de lot minimum par type ──────────────────────────
MIN_QTY = {
    "STK":   1,
    "FUT":   1,
    "CASH":  20000,
    "CRYPTO":0.001,
    "OPT":   1,
    "CFD":   1,
}

# Taille minimum en USD par instrument connu
MIN_USD = {
    "AAPL":170,"MSFT":380,"NVDA":100,"TSLA":200,"AMZN":185,"META":480,
    "GOOG":165,"GOOGL":165,"AMD":120,"NFLX":600,"JPM":200,"BAC":40,
    "SPY":520,"QQQ":440,"GLD":220,"TLT":90,"IWM":220,"VIX":20,
    "AIR":155,"MC":670,"TTE":60,"SAP":190,"ASML":690,"SIE":185,
    "ES":500,"NQ":500,"YM":500,"CL":78,"GC":500,"SI":500,"NG":50,
    "EUR":20,"GBP":20,"USD":20,"AUD":20,"CHF":20,"CAD":20,
    "BTC":80,"ETH":25,"LTC":8,"BCH":30,"XRP":1,
}


class Position:
    def __init__(self, symbol, sec_type, currency, position,
                 avg_cost, market_price, market_value,
                 unrealized_pnl, realized_pnl):
        self.symbol         = symbol
        self.sec_type       = sec_type
        self.currency       = currency
        self.position       = position
        self.avg_cost       = avg_cost
        self.market_price   = market_price
        self.market_value   = market_value
        self.unrealized_pnl = unrealized_pnl
        self.realized_pnl   = realized_pnl

    def to_dict(self):
        total = self.unrealized_pnl + self.realized_pnl
        pct   = 0.0
        if self.position and self.avg_cost:
            pct = (total / (abs(self.position) * self.avg_cost)) * 100
        return {
            "sym":            self.symbol,
            "sec_type":       self.sec_type,
            "currency":       self.currency,
            "qty":            self.position,
            "avg_cost":       round(self.avg_cost,       4),
            "market_price":   round(self.market_price,   4),
            "val":            round(self.market_value,   2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl":   round(self.realized_pnl,   2),
            "pnl":            round(total,               2),
            "pct":            round(pct,                 2),
        }


class OrderStatus:
    def __init__(self, order_id, symbol, action, qty, order_type, status="Submitted"):
        self.order_id   = order_id
        self.symbol     = symbol
        self.action     = action
        self.qty        = qty
        self.order_type = order_type
        self.status     = status
        self.filled     = 0.0
        self.avg_fill   = 0.0

    def to_dict(self):
        return {
            "order_id":   self.order_id,
            "symbol":     self.symbol,
            "action":     self.action,
            "qty":        self.qty,
            "order_type": self.order_type,
            "status":     self.status,
            "filled":     self.filled,
            "avg_fill":   self.avg_fill,
        }


class IBKRApp(EWrapper, EClient):

    def __init__(self, risk: RiskManager, on_update: Callable[[dict], None]):
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.risk      = risk
        self.on_update = on_update
        self.connected = False
        self.account   = settings.IBKR_ACCOUNT

        self._positions:   dict[str, Position]    = {}
        self._orders:      dict[int, OrderStatus] = {}
        self._prices:      dict[str, dict]        = {}  # sym → {price, bid, ask, change}
        self._ticker_map:  dict[int, str]         = {}  # reqId → sym
        self._ticker_rev:  dict[str, int]         = {}  # sym → reqId
        self._next_ticker  = 1000

        self._nav          = 0.0
        self._cash         = 0.0
        self._buying_power = 0.0
        self._unrealized   = 0.0
        self.next_order_id: int | None = None
        self._lock = threading.Lock()
        self._hist_helper = None  # Initialisé par price_feed si nécessaire

    # ── Connexion ─────────────────────────────────────────────

    def connect_and_run(self) -> None:
        log.info(f"Connexion TWS → {settings.TWS_HOST}:{settings.TWS_PORT}")
        self.connect(settings.TWS_HOST, settings.TWS_PORT, settings.TWS_CLIENT_ID)
        t = threading.Thread(target=self.run, daemon=True, name="ibkr-reader")
        t.start()

    def disconnect_clean(self) -> None:
        log.warning("Déconnexion propre de TWS")
        self.connected = False
        self.disconnect()

    # ── Callbacks système ─────────────────────────────────────

    def connectAck(self):
        log.info("✅ Connecté à TWS")
        self.connected = True
        self._push({"type": "status", "connected": True, "account": self.account})

    def connectionClosed(self):
        log.warning("❌ Connexion TWS perdue")
        self.connected = False
        self._push({"type": "status", "connected": False, "account": self.account})

    def nextValidId(self, orderId: OrderId):
        self.next_order_id = orderId
        log.info(f"Next valid order ID : {orderId}")
        self._subscribe()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Codes info (pas des erreurs réelles)
        INFO_CODES = {2104, 2106, 2158, 2103, 2105, 2107, 2108, 2109, 2110}
        if errorCode in INFO_CODES:
            log.debug(f"TWS [{errorCode}] {errorString[:60]}")
            return
        lvl = logging.WARNING if errorCode >= 2100 else logging.ERROR
        log.log(lvl, f"TWS [{errorCode}] req={reqId} : {errorString[:120]}")
        # Ordre rejeté : notifier le frontend avec raison claire
        if errorCode == 201 and reqId > 0:
            clean = errorString.replace("<br>", " ").replace("\n", " ")
            self._push({"type": "order_rejected", "order_id": reqId, "reason": clean})
            if reqId in self._orders:
                self._orders[reqId].status = "Rejected"
                self._push({"type": "order_update", "order": self._orders[reqId].to_dict()})
        elif errorCode < 2100:
            self._push({"type": "error", "code": errorCode, "message": errorString})

    # ── Souscriptions ─────────────────────────────────────────

    def _subscribe(self) -> None:
        # Type 3 = données différées gratuites (si pas d'abonnement live)
        # Type 1 = live (nécessite abonnement payant IBKR)
        self.reqMarketDataType(3)
        tags = "NetLiquidation,TotalCashValue,BuyingPower,UnrealizedPnL,RealizedPnL,InitMarginReq,MaintMarginReq"
        self.reqAccountSummary(9001, "All", tags)
        self.reqAccountUpdates(True, self.account)
        self.reqOpenOrders()

    def subscribe_price(self, symbol: str, sec_type: str,
                        exchange: str, currency: str,
                        primary_exch: str = "") -> int:
        """Demande le flux de prix temps réel pour un instrument."""
        key = f"{symbol}_{sec_type}_{currency}"
        if key in self._ticker_rev:
            return self._ticker_rev[key]

        with self._lock:
            req_id = self._next_ticker
            self._next_ticker += 1

        c = Contract()
        c.symbol      = symbol
        c.secType     = sec_type
        c.exchange    = exchange
        c.currency    = currency
        # primaryExch aide IBKR à trouver le bon instrument (ex: SBF pour actions FR)
        if primary_exch:
            c.primaryExch = primary_exch

        # Pour les futures on doit mettre SMART ou l'exchange direct
        if sec_type == "FUT":
            c.exchange = exchange

        self._ticker_map[req_id] = key
        self._ticker_rev[key]    = req_id
        self._prices[key]        = {
            "sym": symbol, "sec_type": sec_type, "currency": currency,
            "price": 0.0, "bid": 0.0, "ask": 0.0,
            "change": 0.0, "change_pct": 0.0,
            "min_qty": MIN_QTY.get(sec_type, 1),
            "min_usd": MIN_USD.get(symbol, 10),
        }

        # Generic ticks : 233=RTVolume, 221=avgPrice
        self.reqMktData(req_id, c, "233,221", False, False, [])
        log.info(f"Souscription prix {symbol} (reqId={req_id})")
        return req_id

    def unsubscribe_price(self, symbol: str, sec_type: str, currency: str) -> None:
        key = f"{symbol}_{sec_type}_{currency}"
        if key in self._ticker_rev:
            self.cancelMktData(self._ticker_rev[key])
            del self._ticker_map[self._ticker_rev[key]]
            del self._ticker_rev[key]

    # ── Callbacks prix ────────────────────────────────────────

    def tickPrice(self, reqId, tickType, price, attrib):
        key = self._ticker_map.get(reqId)
        if not key or price <= 0:
            return
        p = self._prices.setdefault(key, {})
        # tickType: 1=bid, 2=ask, 4=last, 6=high, 7=low, 9=close, 14=open
        if tickType == 4:    # last price
            p["price"] = round(price, 6)
            # Recalcule change_pct si on a le close précédent
            prev = p.get("prev_close", 0)
            if prev > 0:
                chg = price - prev
                p["change"]     = round(chg, 6)
                p["change_pct"] = round(chg / prev * 100, 3)
        elif tickType == 2:  # ask
            p["ask"] = round(price, 6)
        elif tickType == 1:  # bid
            p["bid"] = round(price, 6)
        elif tickType == 9:  # close veille
            p["prev_close"] = round(price, 6)
            # Calcule change_pct immédiatement si on a déjà le dernier prix
            last = p.get("price", 0)
            if last > 0 and price > 0:
                chg = last - price
                p["change"]     = round(chg, 6)
                p["change_pct"] = round(chg / price * 100, 3)
        elif tickType == 14: # open
            p["open"] = round(price, 6)

        # Broadcast dès qu'on a un prix
        if p.get("price", 0) > 0:
            self._push({"type": "price_update", "key": key, "data": p})

    def tickSize(self, reqId, tickType, size):
        pass  # volume — non utilisé pour l'instant

    def tickGeneric(self, reqId, tickType, value):
        pass

    def tickString(self, reqId, tickType, value):
        pass

    # ── Callbacks données historiques ─────────────────────────

    def historicalData(self, reqId, bar):
        """Reçoit les bars historiques — transmis au HistoricalDataHelper."""
        if hasattr(self, '_hist_helper') and self._hist_helper:
            self._hist_helper.historicalData(reqId, bar)

    def historicalDataEnd(self, reqId, start, end):
        """Fin des données historiques."""
        if hasattr(self, '_hist_helper') and self._hist_helper:
            self._hist_helper.historicalDataEnd(reqId, start, end)
        log.debug(f"historicalDataEnd reqId={reqId}")

    # ── Callbacks compte ──────────────────────────────────────

    def accountSummary(self, reqId, account, tag, value, currency):
        try: v = float(value)
        except: return
        if   tag == "NetLiquidation": self._nav          = v; self.risk.update_nav(v)
        elif tag == "TotalCashValue":  self._cash         = v
        elif tag == "BuyingPower":     self._buying_power = v
        elif tag == "UnrealizedPnL":   self._unrealized   = v
        self._push_account()

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        sym = contract.symbol
        self._positions[sym] = Position(
            sym, contract.secType, contract.currency,
            position, averageCost, marketPrice, marketValue,
            unrealizedPNL, realizedPNL,
        )
        self._push_positions()

    def updateAccountValue(self, key, val, currency, accountName):
        try: v = float(val)
        except: return
        if key == "NetLiquidation":
            self._nav = v; self.risk.update_nav(v)
            self._push_account()

    def accountDownloadEnd(self, accountName):
        log.info(f"Snapshot compte complet : {accountName}")
        self._push_positions()

    # ── Callbacks ordres ──────────────────────────────────────

    def orderStatus(self, orderId, status, filled, remaining,
                    avgFillPrice, permId, parentId, lastFillPrice,
                    clientId, whyHeld, mktCapPrice):
        if orderId in self._orders:
            self._orders[orderId].status   = status
            self._orders[orderId].filled   = filled
            self._orders[orderId].avg_fill = avgFillPrice
            self._push({"type": "order_update", "order": self._orders[orderId].to_dict()})

    def openOrder(self, orderId, contract, order, orderState):
        if orderId not in self._orders:
            qty = order.cashQty if (contract.secType == "CRYPTO" and order.totalQuantity == 0) else order.totalQuantity
            self._orders[orderId] = OrderStatus(
                orderId, contract.symbol, order.action,
                qty, order.orderType, orderState.status)
        self._push({"type": "open_orders",
                    "orders": [o.to_dict() for o in self._orders.values()]})

    # ── Placement d'ordre ──────────────────────────────────────

    def place_order(self, symbol, sec_type, exchange, currency,
                    action, qty, order_type="MKT", limit_price=0.0,
                    stop_price=0.0, outside_rth=True, expiry="",
                    primary_exch="") -> dict:

        if self.risk.killed:
            return {"ok": False, "error": f"Bot stoppé : {self.risk.kill_reason}"}

        # Estimation valeur en EUR pour risk check
        # Pour crypto : qty = USD direct. Pour actions : qty * prix
        est_val = qty if sec_type == "CRYPTO" else qty * max(limit_price or 0, 10)
        ok, msg = self.risk.check_order(symbol, est_val)
        if not ok:
            return {"ok": False, "error": msg}

        if self.next_order_id is None:
            return {"ok": False, "error": "Order ID non disponible — reconnectez TWS"}

        with self._lock:
            oid = self.next_order_id
            self.next_order_id += 1

        c = Contract()
        c.symbol   = symbol
        c.secType  = sec_type
        c.exchange = exchange
        c.currency = currency
        if primary_exch:
            c.primaryExch = primary_exch
        if expiry:
            c.lastTradeDateOrContractMonth = expiry

        o = Order()
        o.action      = action
        o.orderType   = order_type
        o.tif         = "DAY"
        o.outsideRth  = outside_rth
        o.transmit    = True   # transmet immédiatement sans bouton manuel
        o.eTradeOnly  = False   # évite warning [10268]
        o.firmQuoteOnly = False # évite warning similaire

        # IBKR PAXOS crypto : cashQty en USD obligatoire (pas totalQuantity)
        if sec_type == "CRYPTO":
            o.cashQty       = float(qty)   # montant en USD
            o.totalQuantity = 0
            log.info(f"Ordre CRYPTO cashQty={qty} USD")
        else:
            o.totalQuantity = float(qty)

        if order_type in ("LMT", "STP LMT") and limit_price:
            o.lmtPrice = limit_price
        if order_type in ("STP", "STP LMT") and stop_price:
            o.auxPrice = stop_price

        self._orders[oid] = OrderStatus(oid, symbol, action, qty, order_type)
        self.placeOrder(oid, c, o)
        log.info(f"Ordre {oid} envoyé : {action} {qty} {symbol} @ {order_type}")
        self._push({"type": "order_placed", "order": self._orders[oid].to_dict()})
        return {"ok": True, "order_id": oid}

    def cancel_order(self, order_id: int) -> dict:
        if order_id not in self._orders:
            return {"ok": False, "error": "Ordre introuvable"}
        self.cancelOrder(order_id)  # ibapi 9.81 = 1 seul argument
        return {"ok": True, "order_id": order_id}

    # ── Push helpers ──────────────────────────────────────────

    def _push(self, payload: dict) -> None:
        try: self.on_update(payload)
        except Exception as e: log.debug(f"Broadcast ignoré : {e}")

    def _push_account(self) -> None:
        self._push({
            "type":           "account",
            "connected":      self.connected,
            "account":        self.account,
            "nav":            round(self._nav,          2),
            "cash":           round(self._cash,         2),
            "buying_power":   round(self._buying_power, 2),
            "unrealized_pnl": round(self._unrealized,   2),
            "risk":           self.risk.to_dict(),
        })

    def _push_positions(self) -> None:
        self._push({
            "type":      "positions",
            "positions": [p.to_dict() for p in self._positions.values()
                          if p.position != 0],
        })

    def get_snapshot(self) -> dict:
        return {
            "type":           "snapshot",
            "connected":      self.connected,
            "account":        self.account,
            "nav":            round(self._nav,          2),
            "cash":           round(self._cash,         2),
            "buying_power":   round(self._buying_power, 2),
            "unrealized_pnl": round(self._unrealized,   2),
            "positions":      [p.to_dict() for p in self._positions.values()
                               if p.position != 0],
            "orders":         [o.to_dict() for o in self._orders.values()],
            "prices":         self._prices,
            "risk":           self.risk.to_dict(),
        }

    def get_price(self, symbol: str, sec_type: str, currency: str) -> dict | None:
        key = f"{symbol}_{sec_type}_{currency}"
        return self._prices.get(key)

    def get_all_prices(self) -> dict:
        return self._prices
