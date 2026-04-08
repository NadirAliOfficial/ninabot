# NINABOT v3 — IBKR Algorithmic Trading Bot

A full-stack automated trading bot for Interactive Brokers (IBKR) with a React dashboard and FastAPI backend. Supports stocks, crypto, futures, forex, and CFDs with 6 built-in trading modes.

## Features

- Live connection to TWS (Trader Workstation) via ibapi
- 6 automated trading modes: `scalp`, `daytrade`, `swing`, `trend`, `crypto_night`, `safe`
- Signal engine combining EMA, RSI, MACD, Bollinger Bands, ATR
- Auto position sizing (Kelly criterion)
- Automatic Stop Loss & Take Profit
- Risk management: kill switch, circuit breakers, drawdown limits
- Yahoo Finance price fallback when IBKR data is unavailable
- Optional Twitter/X social sentiment scoring
- Real-time React dashboard via WebSocket

## Stack

- **Backend:** Python 3.12, FastAPI, WebSocket, ibapi 9.81.1
- **Frontend:** React, Vite
- **Broker:** Interactive Brokers (TWS)

## Prerequisites

- [Python 3.12](https://www.python.org/downloads/)
- [Node.js 20](https://nodejs.org/)
- [TWS (Trader Workstation)](https://www.interactivebrokers.com/en/trading/tws.php) — must be running

## Setup

### 1. Configure your account

Open `backend/.env` and fill in:

```env
IBKR_ACCOUNT=UXXXXXXXX    # Your IBKR account number (starts with U)
TWS_PORT=7496              # 7496 = live | 7497 = paper trading
```

### 2. Configure TWS API

In TWS: `Edit → Global Configuration → API → Settings`

- Enable **ActiveX and Socket Clients**
- Set port to `7496`
- Enable **Allow connections from localhost only**

### 3. Install dependencies (first time only)

```bash
# Backend
cd backend
py -3.12 -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
```

### 4. Run

**Windows:** Double-click `LANCER_BOT.bat`

**Manual:**
```bash
# Terminal 1 — Backend
cd backend && venv\Scripts\activate && python main.py

# Terminal 2 — Frontend
cd frontend && npm run dev
```

**Stop:** Double-click `ARRETER_BOT.bat`

## URLs

| Service | URL |
|---|---|
| Dashboard | http://localhost:5173 |
| API Health | http://localhost:8000/health |
| Scan Status | http://localhost:8000/scan/status |

## Project Structure

```
ninabot/
├── backend/
│   ├── main.py              # FastAPI server + WebSocket
│   ├── ibkr_client.py       # IBKR TWS connection & order management
│   ├── auto_trader.py       # Automated trading engine
│   ├── price_feed.py        # Live prices (IBKR + Yahoo Finance fallback)
│   ├── risk_manager.py      # Circuit breakers & kill switch
│   ├── social_monitor.py    # Twitter/X sentiment analysis
│   ├── config.py            # Settings loader
│   ├── requirements.txt
│   └── .env                 # fill this in
├── frontend/
│   └── src/App.jsx          # React dashboard
├── LANCER_BOT.bat           # Start (Windows)
└── ARRETER_BOT.bat          # Stop (Windows)
```

## Trading Modes

| Mode | EMA | Risk % | Max Trades | Scan Interval |
|---|---|---|---|---|
| `scalp` | 9/21 | 0.5% | 20 | 5s |
| `daytrade` | 21/50 | 1.0% | 8 | 30s |
| `swing` | 50/200 | 1.5% | 3 | 120s |
| `trend` | 50/200 | 1.0% | 5 | 60s |
| `crypto_night` | 20/100 | 0.8% | 6 | 20s |
| `safe` | 34/150 | 0.3% | 2 | 120s |

## Risk Management

- **Max Drawdown:** 5% (configurable) — kills bot automatically
- **Max Position Size:** 2% of NAV per trade
- **Daily Loss Limit:** $10,000 (configurable)
- **Consecutive losses:** position size halved after 2 losses

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IBKR_ACCOUNT` | — | Your IBKR account number |
| `TWS_HOST` | `127.0.0.1` | TWS host |
| `TWS_PORT` | `7496` | 7496=live, 7497=paper |
| `TWS_CLIENT_ID` | `1` | API client ID |
| `MAX_DRAWDOWN_PCT` | `5.0` | Kill switch threshold (%) |
| `MAX_POSITION_PCT` | `2.0` | Max trade size (% of NAV) |
| `DAILY_LOSS_LIMIT` | `10000.0` | Daily loss limit |
| `TWITTER_BEARER_TOKEN` | — | Optional — social sentiment |
| `API_PORT` | `8000` | Backend port |
