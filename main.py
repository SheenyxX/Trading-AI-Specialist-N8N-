# main_n8n.py

import ccxt
import pandas as pd
import ta
from datetime import datetime, timedelta, timezone
import json
import os
import requests

# === CONFIGURATION ===
N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]
SYMBOL = "BTC/USDT"
TIMEFRAMES = {"15m": 778, "1h": 490, "4h": 188}
TRADES_FILE = "trades.json"

# === 1. Setup Exchange ===
exchange = ccxt.kucoin()

# === 2. Fetch OHLCV ===
def get_ohlcv(symbol=SYMBOL, timeframe="15m", limit=500):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

# === 3. Add EMAs ===
def add_ema(df, ema_periods=[20, 50]):
    for period in ema_periods:
        df[f"EMA{period}"] = ta.trend.ema_indicator(df["close"], window=period)
    return df

# === 4. Analyze Trend ===
def analyze_trend(df):
    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"]:
        return "Bullish trend"
    elif last["EMA20"] < last["EMA50"]:
        return "Bearish trend"
    else:
        return "Neutral trend"

# === 5. Detect Liquidity Zones ===
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

# === 6. Detect Setups ===
def detect_setups(df, trend, zones, tf):
    last = df.iloc[-1]
    close = last["close"]
    ema20 = last["EMA20"]
    ema50 = last["EMA50"]
    setups = []

    if trend == "Neutral trend":
        print(f"🔍 No signal: Trend is neutral on {tf}")
        return setups

    entry_price = ema20 + 0.8 * (ema50 - ema20)

    if trend == "Bearish trend":
        valid_zones = [z for z in zones if z["type"] == "demand" and z["level"] < close]
        if close < ema20 and valid_zones:
            nearest_zone = sorted(valid_zones, key=lambda z: abs(z["level"] - close))[0]
            setups.append(create_trade_dict("Short", entry_price, ema50 * 1.003,
                                            close - (ema50 - ema20) * 2, nearest_zone["level"],
                                            tf, last["timestamp"]))

    elif trend == "Bullish trend":
        valid_zones = [z for z in zones if z["type"] == "supply" and z["level"] > close]
        if close > ema20 and valid_zones:
            nearest_zone = sorted(valid_zones, key=lambda z: abs(z["level"] - close))[0]
            setups.append(create_trade_dict("Long", entry_price, ema50 * 0.997,
                                            close + (ema20 - ema50) * 2, nearest_zone["level"],
                                            tf, last["timestamp"]))
    return setups

# === 7. Trade Dictionary Builder ===
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

# === 8. Load Trades File ===
def load_trades(filename=TRADES_FILE):
    if not os.path.exists(filename):
        return {}
    try:
        with open(filename, "r") as f:
            data = f.read().strip()
            return {} if not data else json.loads(data)
    except Exception:
        return {}

# === 9. Save Trade to File + Send to n8n ===
def save_trade(trade_id, trade_data, tf):
    trades = load_trades(TRADES_FILE)
    trade_data["id"] = trade_id

    if trade_id in trades and trades[trade_id].get("sent_to_n8n") is True:
        print(f"✅ Trade {trade_id} already sent to n8n. Skipping.")
        return

    if tf == "15m":
        for t_id, t in list(trades.items()):
            if t["timeframe"] == tf and t["type"] == trade_data["type"] and t["status"] == "pending":
                if t.get("refinements", 1) < 3:
                    trade_data["refinements"] = t.get("refinements", 1) + 1
                    print(f"🔄 Refinement {trade_data['refinements']} replacing {t_id}")
                    del trades[t_id]
                else:
                    print(f"🚫 Max refinements reached for {tf} {trade_data['type']}")
                    return
    else:
        for t_id, t in list(trades.items()):
            if t["timeframe"] == tf and t["type"] == trade_data["type"] and t["status"] == "pending":
                print(f"🚫 Existing pending trade for {tf} {trade_data['type']}")
                return

    # Send to n8n
    try:
        response = requests.post(N8N_WEBHOOK_URL, json=trade_data, timeout=5)
        if response.status_code == 200:
            trade_data["sent_to_n8n"] = True
            print(f"📤 Sent to n8n ({response.status_code})")
        else:
            trade_data["sent_to_n8n"] = False
            print(f"⚠️ Failed to confirm delivery to n8n ({response.status_code})")
    except Exception as e:
        trade_data["sent_to_n8n"] = False
        print(f"❌ Exception sending to n8n: {e}")

    trades[trade_id] = trade_data
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=4)
    print(f"💾 Saved trade {trade_id} with status: {trade_data['sent_to_n8n']}")

# === 10. Retry unsent trades ===
def resend_unsent_trades():
    trades = load_trades(TRADES_FILE)
    updated = False
    for trade_id, trade in trades.items():
        if not trade.get("sent_to_n8n", False):
            try:
                response = requests.post(N8N_WEBHOOK_URL, json=trade, timeout=5)
                if response.status_code == 200:
                    trade["sent_to_n8n"] = True
                    print(f"🔁 Resent trade {trade_id}")
                    updated = True
                else:
                    print(f"⚠️ Retry failed for {trade_id} ({response.status_code})")
            except Exception as e:
                print(f"❌ Retry error for {trade_id}: {e}")
    if updated:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=4)

# === 11. Main Execution ===
if __name__ == "__main__":
    for tf, limit in TIMEFRAMES.items():
        print(f"\n=== {tf.upper()} ===")
        df = get_ohlcv(SYMBOL, tf, limit)
        df = add_ema(df)
        trend = analyze_trend(df)
        zones = detect_liquidity_zones(df)
        setups = detect_setups(df, trend, zones, tf)

        print(f"→ Close: {df['close'].iloc[-1]:,.2f}")
        print(f"→ EMA20: {df['EMA20'].iloc[-1]:,.2f} | EMA50: {df['EMA50'].iloc[-1]:,.2f}")
        print(f"→ Trend: {trend}")

        for setup in setups:
            print(f"📊 Setup: {setup['type']} | Entry {setup['entry']}, TP2 {setup['tp2']}, SL {setup['sl']}")
            trade_id = f"{SYMBOL.replace('/', '')}_{tf}_{df['timestamp'].iloc[-1].strftime('%Y%m%d_%H%M%S')}_{setup['type'][0]}"
            save_trade(trade_id, setup, tf)

    resend_unsent_trades()
