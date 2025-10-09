import ccxt
import pandas as pd
import ta
from datetime import datetime, timedelta, timezone
import json
import os
import requests
from dateutil.parser import isoparse  # Robust ISO timestamp parser

# === CONFIGURATION ===
N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]
N8N_UPDATE_WEBHOOK_URL = os.environ["N8N_UPDATE_WEBHOOK_URL"]
SYMBOL = "BTC/USDT"
TIMEFRAMES = {"15m": 778, "1h": 490, "4h": 188}
TRADES_FILE = "trades.json"

exchange = ccxt.kucoin()


def get_ohlcv(symbol=SYMBOL, timeframe="15m", limit=500):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def add_ema(df, ema_periods=[20, 50]):
    for period in ema_periods:
        df[f"EMA{period}"] = ta.trend.ema_indicator(df["close"], window=period)
    return df


def analyze_trend(df):
    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"]:
        return "Bullish trend"
    elif last["EMA20"] < last["EMA50"]:
        return "Bearish trend"
    return "Neutral trend"


def detect_liquidity_zones(df, lookback=50):
    zones = []
    for i in range(lookback, len(df) - lookback):
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        vol = df["volume"].iloc[i]
        if high == max(df["high"].iloc[i - lookback:i + lookback + 1]):
            zones.append({"type": "supply", "level": high, "volume": vol})
        if low == min(df["low"].iloc[i - lookback:i + lookback + 1]):
            zones.append({"type": "demand", "level": low, "volume": vol})
    return zones


def create_trade_dict(t_type, entry, sl, tp1, tp2, tf, timestamp):
    return {
        "symbol": SYMBOL,
        "timeframe": tf,
        "type": t_type,
        "entry": round(float(entry), 2),
        "sl": round(float(sl), 2),
        "tp1": round(float(tp1), 2),
        "tp2": round(float(tp2), 2),
        "signal_time": timestamp.isoformat(),
        "status": "pending",
        "entry_time": None,
        "exit_time": None,
        "exit_reason": None,
        "refinements": 1,
        "sent_to_n8n": False
    }


def detect_setups(df, trend, zones, tf):
    last = df.iloc[-1]
    close = last["close"]
    ema20 = last["EMA20"]
    ema50 = last["EMA50"]
    setups = []

    if trend == "Neutral trend":
        return setups

    entry_price = ema20 + 0.8 * (ema50 - ema20)

    if trend == "Bearish trend":
        valid_zones = [z for z in zones if z["type"] == "demand" and z["level"] < close]
        if close < ema20 and valid_zones:
            nearest = sorted(valid_zones, key=lambda z: abs(z["level"] - close))[0]
            setups.append(create_trade_dict("Short", entry_price, ema50 * 1.003,
                                            close - (ema50 - ema20) * 2, nearest["level"], tf, last["timestamp"]))
    elif trend == "Bullish trend":
        valid_zones = [z for z in zones if z["type"] == "supply" and z["level"] > close]
        if close > ema20 and valid_zones:
            nearest = sorted(valid_zones, key=lambda z: abs(z["level"] - close))[0]
            setups.append(create_trade_dict("Long", entry_price, ema50 * 0.997,
                                            close + (ema20 - ema50) * 2, nearest["level"], tf, last["timestamp"]))

    return setups


def load_trades(filename=TRADES_FILE):
    if not os.path.exists(filename):
        return {}
    try:
        with open(filename, "r") as f:
            data = f.read().strip()
            return {} if not data else json.loads(data)
    except Exception:
        return {}


def save_trade(trade_id, trade_data, tf):
    trades = load_trades()
    trade_data["id"] = trade_id

    if tf == "15m":
        for t_id, t in list(trades.items()):
            if t["timeframe"] == tf and t["type"] == trade_data["type"] and t["status"] == "pending":
                if t.get("refinements", 1) < 3:
                    trade_data["refinements"] = t.get("refinements", 1) + 1
                    del trades[t_id]
                else:
                    return
    else:
        for t_id, t in list(trades.items()):
            if t["timeframe"] == tf and t["type"] == trade_data["type"] and t["status"] == "pending":
                return

    try:
        response = requests.post(N8N_WEBHOOK_URL, json=trade_data, timeout=5)
        if response.status_code == 200:
            trade_data["sent_to_n8n"] = True
    except Exception:
        trade_data["sent_to_n8n"] = False

    trades[trade_id] = trade_data
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=4)


def update_trade_status(trade_id, status, entry_time=None, exit_time=None, exit_reason=None):
    payload = {
        "id": trade_id,
        "status": status,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "exit_reason": exit_reason
    }
    try:
        response = requests.post(N8N_UPDATE_WEBHOOK_URL, json=payload, timeout=5)
        if response.status_code == 200:
            print(f"✅ Updated {trade_id}")
    except Exception as e:
        print(f"❌ Failed to update {trade_id}: {e}")


def process_trade_status(df):
    trades = load_trades()
    updated = False
    high = df["high"].iloc[-1]
    low = df["low"].iloc[-1]
    close = df["close"].iloc[-1]
    now = datetime.now(timezone.utc)

    expiry = {
        "15m": timedelta(hours=2),
        "1h": timedelta(hours=12),
        "4h": timedelta(days=3)
    }

    for trade_id, trade in trades.items():
        if trade["symbol"] != SYMBOL:
            continue

        try:
            signal_time = isoparse(trade["signal_time"])
        except Exception as e:
            print(f"⚠️ Invalid timestamp for trade {trade_id}: {e}")
            continue

        if trade["status"] == "pending":
            if signal_time and now - signal_time > expiry[trade["timeframe"]]:
                trade["status"] = "expired"
                trade["exit_reason"] = "Signal expired"
                trade["exit_time"] = now.isoformat()
                update_trade_status(trade_id, "expired", exit_time=trade["exit_time"], exit_reason="Signal expired")
                updated = True
                continue

            if trade["type"] == "Long" and low <= trade["entry"]:
                trade["status"] = "open"
                trade["entry_time"] = now.isoformat()
                update_trade_status(trade_id, "open", entry_time=trade["entry_time"])
                updated = True
                continue

            elif trade["type"] == "Short" and high >= trade["entry"]:
                trade["status"] = "open"
                trade["entry_time"] = now.isoformat()
                update_trade_status(trade_id, "open", entry_time=trade["entry_time"])
                updated = True
                continue

        elif trade["status"] == "open":
            if (trade["type"] == "Long" and close <= trade["sl"]) or \
               (trade["type"] == "Short" and close >= trade["sl"]):
                trade["status"] = "closed"
                trade["exit_time"] = now.isoformat()
                trade["exit_reason"] = "Stop Loss hit"
                update_trade_status(trade_id, "closed", exit_time=trade["exit_time"], exit_reason="Stop Loss hit")
                updated = True
                continue

            elif (trade["type"] == "Long" and close >= trade["tp2"]) or \
                 (trade["type"] == "Short" and close <= trade["tp2"]):
                trade["status"] = "closed"
                trade["exit_time"] = now.isoformat()
                trade["exit_reason"] = "Take Profit hit"
                update_trade_status(trade_id, "closed", exit_time=trade["exit_time"], exit_reason="Take Profit hit")
                updated = True
                continue

    if updated:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=4)


if __name__ == "__main__":
    for tf, limit in TIMEFRAMES.items():
        print(f"\n=== {tf.upper()} ===")
        df = get_ohlcv(SYMBOL, tf, limit)
        df = add_ema(df)
        trend = analyze_trend(df)
        zones = detect_liquidity_zones(df)
        setups = detect_setups(df, trend, zones, tf)
        print(f"→ Close: {df['close'].iloc[-1]:,.2f}")
        print(f"→ Trend: {trend}")
        for setup in setups:
            print(f"📊 Setup: {setup['type']} @ {setup['entry']}")
            trade_id = f"{SYMBOL.replace('/', '')}_{tf}_{df['timestamp'].iloc[-1].strftime('%Y%m%d_%H%M%S')}_{setup['type'][0]}"
            save_trade(trade_id, setup, tf)
        process_trade_status(df)
