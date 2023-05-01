<div align="center">

# 🤖 NINABOT v3
### IBKR Algorithmic Trading Bot

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://reactjs.org)
[![IBKR](https://img.shields.io/badge/Interactive%20Brokers-TWS%20API-red?style=for-the-badge)](https://www.interactivebrokers.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Fully automated trading bot for Interactive Brokers with a live React dashboard.**  
Supports stocks, crypto, futures, forex, and CFDs — with built-in risk management and 6 trading modes.

[Features](#-features) • [Setup](#-setup) • [Trading Modes](#-trading-modes) • [Architecture](#-architecture) • [Risk Management](#-risk-management)

</div>

---

## ✨ Features

| | Feature |
|---|---|
| 📡 | **Live TWS connection** via ibapi 9.81.1 with auto-reconnect every 15s |
| 🧠 | **Signal engine** — EMA, RSI, MACD, Bollinger Bands, ATR combined into a 0-100 score |
| ⚡ | **6 trading modes** — scalp, daytrade, swing, trend, crypto night, safe |
| 📐 | **Auto position sizing** using Kelly criterion based on NAV + conviction score |
| 🛡️ | **Risk management** — kill switch, circuit breakers, drawdown limits |
| 💹 | **Multi-asset** — stocks, crypto, futures, forex, CFDs, options |
| 📊 | **React dashboard** with real-time WebSocket updates (P&L, positions, logs) |
| 🐦 | **Social sentiment** — optional Twitter/X scoring per instrument |
| 🔁 | **Yahoo Finance fallback** when IBKR live data is unavailable |
| 🔒 | **Bracket orders** — automatic Stop Loss & Take Profit on every trade |

---

## 🚀 Setup

### Prerequisites

- [Python 3.12](https://www.python.org/downloads/)
- [Node.js 20](https://nodejs.org/)
- [TWS (Trader Workstation)](https://www.interactivebrokers.com/en/trading/tws.php) — must be running

---

### Step 1 — Configure TWS API

In TWS: `Edit → Global Configuration → API → Settings`

- ✅ Enable **ActiveX and Socket Clients**
- ✅ Port: `7496` (live) or `7497` (paper trading)
- ✅ Enable **Allow connections from localhost only**

---

### Step 2 — Configure `.env`

Open `backend/.env` and fill in your account number:

```env
IBKR_ACCOUNT=UXXXXXXXX    # Your IBKR account number (starts with U)
TWS_PORT=7496              # 7496 = live | 7497 = paper trading
```

---

### Step 3 — Install dependencies *(first time only)*

```bash
# Backend
cd backend
py -3.12 -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
```

---

### Step 4 — Run

**Windows** — double-click:
```
LANCER_BOT.bat    ← Start
ARRETER_BOT.bat   ← Stop
```

**Manual:**
```bash
# Terminal 1 — Backend
cd backend && venv\Scripts\activate && python main.py

# Terminal 2 — Frontend
cd frontend && npm run dev
```

---

### Live URLs

| Service | URL |
|---|---|
| 🖥️ Dashboard | http://localhost:5173 |
| 🔌 API Health | http://localhost:8000/health |
| 📡 Scan Status | http://localhost:8000/scan/status |

---

## 📊 Trading Modes

| Mode | EMA | RSI Band | Risk % | Max Trades | Scan | Description |
|---|---|---|---|---|---|---|
| `scalp` | 9/21 | 35–65 | 0.5% | 20 | 5s | Flash profit, max reactivity |
| `daytrade` | 21/50 | 35–65 | 1.0% | 8 | 30s | Short sessions, regular profits |
| `swing` | 50/200 | 30–70 | 1.5% | 3 | 120s | Max profit, high TP |
| `trend` | 50/200 | 35–65 | 1.0% | 5 | 60s | Pure trend following |
| `crypto_night` | 20/100 | 28–72 | 0.8% | 6 | 20s | BTC/ETH 24/7, high volatility |
| `safe` | 34/150 | 40–60 | 0.3% | 2 | 120s | Capital preservation, minimal risk |

---

## 🧠 Signal Engine

Each instrument is scored **0–100** by combining:

```
EMA crossover   →  25 pts   (trend direction)
RSI             →  20 pts   (overbought / oversold)
MACD            →  15 pts   (momentum)
Bollinger Bands →  15 pts   (volatility squeeze)
ATR             →  10 pts   (volatility filter)
Sentiment       →   5 pts   (Twitter/X — optional)
```

> Signal fires when score ≥ `min_conviction` threshold (configurable per mode)

---

## 🛡️ Risk Management

- **Kill switch** — auto-stops bot if drawdown exceeds threshold
- **Max drawdown:** 5% of NAV (configurable)
- **Max position size:** 2% of NAV per trade
- **Daily loss limit:** $10,000 (configurable)
- **Consecutive losses:** position size halved after every 2 losses (anti-martingale)
- **Retry queue:** failed orders retried up to 3x with backoff (2s → 5s → 15s)

---

## 🏗️ Architecture

```
ninabot/
├── backend/
│   ├── main.py              # FastAPI server + WebSocket hub
│   ├── ibkr_client.py       # TWS connection, orders, positions, prices
│   ├── auto_trader.py       # Trading loop, signal engine, position manager
│   ├── price_feed.py        # IBKR live + Yahoo Finance fallback
│   ├── risk_manager.py      # Circuit breakers, kill switch, drawdown
│   ├── social_monitor.py    # Twitter/X sentiment scoring
│   ├── config.py            # Pydantic settings from .env
│   ├── requirements.txt
│   └── .env                 # ← configure this
├── frontend/
│   └── src/App.jsx          # React real-time dashboard
├── LANCER_BOT.bat           # One-click start (Windows)
└── ARRETER_BOT.bat          # One-click stop (Windows)
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IBKR_ACCOUNT` | *(required)* | IBKR account number (starts with U) |
| `TWS_HOST` | `127.0.0.1` | TWS host |
| `TWS_PORT` | `7496` | `7496` = live · `7497` = paper |
| `TWS_CLIENT_ID` | `1` | API client ID |
| `MAX_DRAWDOWN_PCT` | `5.0` | Kill switch threshold (% of NAV) |
| `MAX_POSITION_PCT` | `2.0` | Max trade size (% of NAV) |
| `DAILY_LOSS_LIMIT` | `10000.0` | Daily loss cap |
| `TWITTER_BEARER_TOKEN` | *(optional)* | Twitter/X API for sentiment |
| `API_HOST` | `0.0.0.0` | Backend bind address |
| `API_PORT` | `8000` | Backend port |

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Bot status + IBKR connection |
| `GET` | `/snapshot` | Full account snapshot |
| `POST` | `/order` | Place an order |
| `DELETE` | `/order/{id}` | Cancel an order |
| `POST` | `/position/close` | Close a position |
| `POST` | `/positions/close_all` | Close all positions |
| `POST` | `/auto/start` | Start auto trader |
| `POST` | `/auto/stop` | Stop auto trader |
| `GET` | `/auto/status` | Auto trader status |
| `GET` | `/performance` | P&L + win rate |
| `GET` | `/risk/status` | Risk manager state |
| `POST` | `/risk/kill` | Emergency kill switch |
| `WS` | `/ws` | Real-time WebSocket stream |

---

<div align="center">

Built with ❤️ for algorithmic traders · [Interactive Brokers TWS API](https://www.interactivebrokers.com/en/trading/tws.php)

</div>
<!-- updated: 2023-05-01-r01 -->
