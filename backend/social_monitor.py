# social_monitor.py — Twitter/X + Web scraping pour sentiment de marché
# Supporte : Twitter API v2 (Bearer Token) + fallback web scraping

import asyncio
import json
import logging
import re
import ssl
import time
import urllib.request
import urllib.parse
import threading

_ssl_ctx = ssl.create_default_context()
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("bot.social")

# ── Mots-clés par instrument ──────────────────────────────────────────────────
INSTRUMENT_KEYWORDS = {
    # Crypto
    "BTC":   ["#Bitcoin","#BTC","bitcoin","$BTC","Bitcoin price"],
    "ETH":   ["#Ethereum","#ETH","ethereum","$ETH"],
    "LTC":   ["#Litecoin","#LTC","litecoin"],
    # US Stocks
    "AAPL":  ["#AAPL","$AAPL","Apple stock","Apple earnings"],
    "MSFT":  ["#MSFT","$MSFT","Microsoft stock"],
    "NVDA":  ["#NVDA","$NVDA","Nvidia","NVDA stock"],
    "TSLA":  ["#TSLA","$TSLA","Tesla","Elon Musk"],
    "AMZN":  ["#AMZN","$AMZN","Amazon stock"],
    "META":  ["#META","$META","Meta Platforms"],
    "GOOG":  ["#GOOG","$GOOG","Alphabet","Google stock"],
    "AMD":   ["#AMD","$AMD","AMD stock"],
    "NFLX":  ["#NFLX","$NFLX","Netflix"],
    "JPM":   ["#JPM","$JPM","JPMorgan"],
    # ETF / Macro
    "SPY":   ["#SPY","$SPY","S&P500","#SP500","S&P 500"],
    "QQQ":   ["#QQQ","$QQQ","Nasdaq","#Nasdaq"],
    "GLD":   ["#Gold","#GLD","gold price","or prix"],
    # Futures / Macro
    "ES":    ["S&P futures","$ES_F","#SPfutures"],
    "NQ":    ["Nasdaq futures","$NQ_F"],
    "CL":    ["#OilPrice","crude oil","WTI","#OOTT"],
    "GC":    ["#Gold","gold futures","#XAUUSD"],
    # Forex
    "EUR":   ["#EURUSD","EUR/USD","euro dollar"],
    "GBP":   ["#GBPUSD","GBP/USD","pound dollar"],
    "USD":   ["#USDJPY","USD/JPY","yen dollar"],
    # Macro
    "FED":   ["#FederalReserve","#Fed","interest rates","#FOMC","Jerome Powell"],
    "MACRO": ["#StockMarket","#WallStreet","#trading","market crash","bull market","bear market"],
}

# Comptes Twitter influents par catégorie
INFLUENTIAL_ACCOUNTS = {
    "Crypto":   ["elonmusk","saylor","aantonop","cz_binance","VitalikButerin"],
    "Stocks":   ["jimcramer","Carl_C_Icahn","chamath","naval"],
    "Macro":    ["federalreserve","JeromePowell","RayDalio"],
    "Trading":  ["TruthGundlach","elerianm"],
}

# Comptes à surveiller (top influenceurs finance/crypto)
TOP_ACCOUNTS = [
    "elonmusk", "saylor", "VitalikButerin", "cz_binance",
    "jimcramer", "chamath", "naval", "elerianm",
    "federalreserve", "RayDalio", "TruthGundlach",
]

# ── Analyse de sentiment simple ───────────────────────────────────────────────
POS_WORDS = ["bullish","moon","pump","breakout","buy","long","surge","rally","ath","record",
             "gain","profit","up","rise","green","bull","hodl","accumulate","support",
             "🚀","📈","💚","✅","🔥","💎","hausse","montée","achat"]
NEG_WORDS = ["bearish","dump","crash","sell","short","drop","fall","decline","fear","panic",
             "loss","down","red","bear","correction","liquidation","rug","fud","concern",
             "📉","❌","🔴","💸","baisse","chute","vente","attention","risque"]

def analyze_sentiment(text: str) -> dict:
    """Analyse le sentiment d'un texte. Retourne score -1 à +1."""
    text_low = text.lower()
    pos = sum(1 for w in POS_WORDS if w in text_low)
    neg = sum(1 for w in NEG_WORDS if w in text_low)
    total = pos + neg
    if total == 0:
        score = 0.0
        label = "neutre"
    else:
        score = (pos - neg) / total
        label = "haussier" if score > 0.2 else "baissier" if score < -0.2 else "neutre"
    return {"score": round(score, 3), "label": label, "pos": pos, "neg": neg}

def detect_instruments(text: str) -> list[str]:
    """Détecte les instruments mentionnés dans un texte."""
    found = []
    text_up = text.upper()
    for sym, keywords in INSTRUMENT_KEYWORDS.items():
        for kw in keywords:
            if kw.upper() in text_up or kw in text:
                if sym not in found:
                    found.append(sym)
                break
    return found


# ── Classe principale SocialMonitor ──────────────────────────────────────────
class SocialMonitor:
    def __init__(self, bearer_token: str | None = None, on_update: Callable | None = None):
        self.bearer_token = bearer_token
        self.on_update    = on_update
        self._cache: list[dict] = []       # derniers tweets/news
        self._sentiment: dict[str,dict] = {} # sentiment par instrument
        self._running = False
        self._thread  = None
        self.has_twitter = bool(bearer_token)
        log.info(f"SocialMonitor init — Twitter API: {'✅' if self.has_twitter else '❌ (mode web scraping)'}")

    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="social-monitor")
        self._thread.start()
        log.info("SocialMonitor démarré")

    def stop(self):
        self._running = False

    def _loop(self):
        """Boucle principale — alterne Twitter API et web scraping."""
        while self._running:
            try:
                if self.has_twitter:
                    self._fetch_twitter()
                else:
                    self._fetch_web_news()
            except Exception as e:
                log.warning(f"SocialMonitor erreur: {e}")
            time.sleep(30)  # refresh toutes les 30 secondes

    # ── Twitter API v2 ────────────────────────────────────────────────────────
    def _fetch_twitter(self):
        """Fetch tweets via Twitter API v2 avec Bearer Token."""
        queries = [
            "(#Bitcoin OR $BTC OR #BTC) -is:retweet lang:en",
            "($NVDA OR #NVDA OR Nvidia) -is:retweet lang:en",
            "($TSLA OR #TSLA) -is:retweet lang:en",
            "(#FederalReserve OR #Fed OR #FOMC) -is:retweet",
            "(#SP500 OR $SPY OR S&P500) -is:retweet lang:en",
            "(#Gold OR $GLD OR gold price) -is:retweet lang:en",
        ]
        new_items = []
        for q in queries:
            try:
                url = "https://api.twitter.com/2/tweets/search/recent?" + urllib.parse.urlencode({
                    "query":       q,
                    "max_results": 10,
                    "tweet.fields":"created_at,public_metrics,author_id",
                    "expansions":  "author_id",
                    "user.fields": "username,public_metrics,verified",
                })
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {self.bearer_token}",
                    "User-Agent": "NINABot/3.0",
                })
                with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as r:
                    data = json.loads(r.read())

                users = {u["id"]: u for u in data.get("includes",{}).get("users",[])}
                for tweet in data.get("data", []):
                    author = users.get(tweet.get("author_id",""), {})
                    metrics = tweet.get("public_metrics", {})
                    text = tweet.get("text","")
                    sent = analyze_sentiment(text)
                    insts = detect_instruments(text)
                    item = {
                        "id":        tweet.get("id",""),
                        "source":    "twitter",
                        "author":    f"@{author.get('username','?')}",
                        "followers": author.get("public_metrics",{}).get("followers_count",0),
                        "verified":  author.get("verified", False),
                        "text":      text[:280],
                        "ts":        tweet.get("created_at", datetime.now(timezone.utc).isoformat()),
                        "likes":     metrics.get("like_count", 0),
                        "retweets":  metrics.get("retweet_count", 0),
                        "sentiment": sent,
                        "instruments": insts,
                        "url":       f"https://twitter.com/i/web/status/{tweet.get('id','')}",
                    }
                    new_items.append(item)
                    self._update_sentiment(insts, sent)
            except Exception as e:
                log.debug(f"Twitter query error: {e}")

        if new_items:
            self._cache = new_items[:50]
            self._push_update()

    # ── Web Scraping (fallback sans API key) ──────────────────────────────────
    def _fetch_web_news(self):
        """Scrape les dernières news financières depuis des sources publiques."""
        sources = [
            ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", ["BTC","ETH","Crypto"]),
            ("Reuters Biz","https://feeds.reuters.com/reuters/businessNews",  ["SPY","MACRO","FED"]),
            ("Yahoo Fin",  "https://finance.yahoo.com/news/rss",              ["AAPL","MSFT","SPY"]),
        ]
        new_items = []
        for source_name, url, default_insts in sources:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; NINABot/3.0)",
                    "Accept": "application/rss+xml,application/xml,text/xml",
                })
                with urllib.request.urlopen(req, timeout=8, context=_ssl_ctx) as r:
                    content = r.read().decode("utf-8", errors="ignore")

                # Parse RSS minimal
                items_raw = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
                for raw in items_raw[:5]:
                    title = re.search(r"<title><!\[CDATA\[(.*?)\]\]>|<title>(.*?)</title>", raw, re.DOTALL)
                    desc  = re.search(r"<description><!\[CDATA\[(.*?)\]\]>|<description>(.*?)</description>", raw, re.DOTALL)
                    link  = re.search(r"<link>(.*?)</link>", raw)
                    pub   = re.search(r"<pubDate>(.*?)</pubDate>", raw)

                    title_text = (title.group(1) or title.group(2) or "").strip() if title else ""
                    desc_text  = (desc.group(1) or desc.group(2) or "").strip() if desc else ""
                    # Clean HTML
                    full_text  = re.sub(r"<[^>]+>","", title_text + " " + desc_text)[:300]

                    sent  = analyze_sentiment(full_text)
                    insts = detect_instruments(full_text) or default_insts

                    new_items.append({
                        "id":       hash(full_text[:50]) & 0xFFFFFF,
                        "source":   source_name,
                        "author":   source_name,
                        "followers":0,
                        "verified": True,
                        "text":     title_text[:200],
                        "desc":     desc_text[:200],
                        "ts":       pub.group(1).strip() if pub else datetime.now().isoformat(),
                        "likes":    0,
                        "retweets": 0,
                        "sentiment": sent,
                        "instruments": insts,
                        "url":      link.group(1).strip() if link else "",
                    })
            except Exception as e:
                log.debug(f"Web scrape {source_name}: {e}")

        if new_items:
            self._cache = (new_items + self._cache)[:50]
            for item in new_items:
                self._update_sentiment(item["instruments"], item["sentiment"])
            self._push_update()

    def _update_sentiment(self, instruments: list, sent: dict):
        """Met à jour le sentiment agrégé par instrument."""
        for sym in instruments:
            if sym not in self._sentiment:
                self._sentiment[sym] = {"scores": [], "label": "neutre", "avg": 0.0, "count": 0}
            s = self._sentiment[sym]
            s["scores"].append(sent["score"])
            s["scores"] = s["scores"][-20:]  # garder 20 derniers
            s["avg"]    = round(sum(s["scores"]) / len(s["scores"]), 3)
            s["count"]  = s.get("count", 0) + 1
            s["label"]  = "haussier" if s["avg"] > 0.15 else "baissier" if s["avg"] < -0.15 else "neutre"
            s["updated"]= datetime.now().isoformat()

    def _push_update(self):
        if self.on_update:
            try:
                self.on_update({
                    "type":      "social_update",
                    "tweets":    self._cache[:20],
                    "sentiment": self._sentiment,
                })
            except Exception as e:
                log.debug(f"Social push error: {e}")

    # ── API publique ──────────────────────────────────────────────────────────
    def get_feed(self, limit: int = 20) -> list[dict]:
        return self._cache[:limit]

    def get_sentiment(self) -> dict:
        return self._sentiment

    def get_sentiment_for(self, sym: str) -> dict | None:
        return self._sentiment.get(sym)

    def get_summary(self) -> dict:
        return {
            "total_tweets": len(self._cache),
            "instruments_tracked": len(self._sentiment),
            "has_twitter_api": self.has_twitter,
            "sentiment": self._sentiment,
        }
