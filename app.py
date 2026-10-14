from flask import Flask, jsonify
from flask_cors import CORS
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
CORS(app)

API_URL = "https://biquote.io/api"

MARKETS = {
    "XAUUSD": "Gold",
    "XAGUSD": "Silver",
    "US30": "US30",
    "NAS100": "NAS100",
    "EURUSD": "EURUSD",
}


def get_candles(symbol, interval, target=250):
    url = f"{API_URL}/{symbol}/ohlc"
    now = datetime.now(timezone.utc)
    end_time = now
    frames = []

    # Collect candles from successive historical windows.
    window_days = 7

    for _ in range(8):
        start_time = end_time - timedelta(days=window_days)

        params = {
            "interval": interval,
            "limit": 1000,
            "from": start_time.isoformat(),
            "to": end_time.isoformat(),
        }

        response = requests.get(
            url, params=params, timeout=20
        )
        response.raise_for_status()

        payload = response.json()
        bars = payload.get("bars", []) if isinstance(payload, dict) else []

        if bars:
            frames.append(pd.DataFrame(bars))

        # Stop once we have enough raw candles to analyse.
        if frames:
            combined = pd.concat(frames, ignore_index=True)
            if len(combined) >= target + 20:
                break

        end_time = start_time

    if not frames:
        raise ValueError("No historical candles returned.")

    df = pd.concat(frames, ignore_index=True)

    required = ["openTime", "open", "high", "low", "close"]

    if not all(col in df.columns for col in required):
        raise ValueError("Candle data is missing required fields.")

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    if "isOpen" in df.columns:
        open_flags = (
            df["isOpen"].astype(str).str.lower()
            .isin(["true", "1"])
        )
        df = df[~open_flags]

    df = df.drop_duplicates(subset=["openTime"])
    df = df.sort_values("openTime").reset_index(drop=True)

    if df.empty:
        raise ValueError("No completed candles available.")

    return df


def analyse(symbol):
    hourly = get_candles(symbol, "1h", 250)
    fifteen = get_candles(symbol, "15m", 100)

    if len(hourly) < 200 or len(fifteen) < 15:
        return {
            "market": MARKETS[symbol],
            "symbol": symbol,
            "signal": "WAIT",
            "reason": (
                f"Not enough completed candles. "
                f"H1: {len(hourly)}, M15: {len(fifteen)}."
            ),
        }

    hourly["ema50"] = hourly["close"].ewm(
        span=50, adjust=False
    ).mean()

    hourly["ema200"] = hourly["close"].ewm(
        span=200, adjust=False
    ).mean()

    trend = hourly.iloc[-1]
    previous = fifteen.iloc[-2]
    last = fifteen.iloc[-1]
    recent = fifteen.iloc[-12:-1]

    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())

    bullish_trend = trend["ema50"] > trend["ema200"]
    bearish_trend = trend["ema50"] < trend["ema200"]

    bullish_sweep = (
        last["low"] < recent_low
        and last["close"] > recent_low
        and last["close"] > previous["high"]
    )

    bearish_sweep = (
        last["high"] > recent_high
        and last["close"] < recent_high
        and last["close"] < previous["low"]
    )

    result = {
        "market": MARKETS[symbol],
        "symbol": symbol,
        "signal": "WAIT",
        "entry": None,
        "stop_loss": None,
        "take_profit": None,
        "risk_reward": "1:2",
        "strategy": "H1 EMA 50/200 + M15 sweep",
        "reason": "No confirmed setup. Wait.",
        "candle_time": str(last["openTime"]),
    }

    if bullish_trend and bullish_sweep:
        entry = float(last["close"])
        stop_loss = float(fifteen.iloc[-5:]["low"].min())
        risk = entry - stop_loss

        if risk > 0:
            result.update({
                "signal": "BUY",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": entry + 2 * risk,
                "reason": "Bullish trend and sweep conditions met.",
            })

    elif bearish_trend and bearish_sweep:
        entry = float(last["close"])
        stop_loss = float(fifteen.iloc[-5:]["high"].max())
        risk = stop_loss - entry

        if risk > 0:
            result.update({
                "signal": "SELL",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": entry - 2 * risk,
                "reason": "Bearish trend and sweep conditions met.",
            })

    return result


@app.route("/")
def home():
    return jsonify({
        "name": "Atlas Signals",
        "status": "online",
        "message": "Signal engine API is running.",
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "online"})


@app.route("/api/signals")
def all_signals():
    results = {}

    for symbol in MARKETS:
        try:
            results[symbol] = analyse(symbol)
        except Exception:
            app.logger.exception("Analysis failed for %s", symbol)
            results[symbol] = {
                "market": MARKETS[symbol],
                "symbol": symbol,
                "signal": "UNAVAILABLE",
                "reason": "Market data or analysis is temporarily unavailable.",
            }

    return jsonify(results)


@app.route("/api/signals/<symbol>")
def one_signal(symbol):
    symbol = symbol.upper()

    if symbol not in MARKETS:
        return jsonify({
            "error": "Unsupported market.",
            "supported_markets": list(MARKETS.keys()),
        }), 404

    try:
        return jsonify(analyse(symbol))
    except Exception:
        app.logger.exception("Analysis failed for %s", symbol)
        return jsonify({
            "market": MARKETS[symbol],
            "symbol": symbol,
            "signal": "UNAVAILABLE",
            "reason": "Market data or analysis is temporarily unavailable.",
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
