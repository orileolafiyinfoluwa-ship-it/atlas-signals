from flask import Flask, jsonify
from flask_cors import CORS
import requests
import pandas as pd

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


def get_candles(symbol, interval, limit=250):
    url = f"{API_URL}/{symbol}/ohlc"

    response = requests.get(
        url,
        params={"interval": interval, "limit": limit},
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Unexpected API response.")

    bars = payload.get("bars")

    if not isinstance(bars, list) or not bars:
        raise ValueError("No candle data available.")

    df = pd.DataFrame(bars)

    required = ["openTime", "open", "high", "low", "close"]

    if not all(column in df.columns for column in required):
        raise ValueError("Candle data is missing required fields.")

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    # Exclude unfinished candles.
    if "isOpen" in df.columns:
        df = df[
            ~df["isOpen"].astype(str).str.lower().isin(
                ["true", "1"]
            )
        ]

    # Biquote returns the newest candles first.
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
            "reason": "Not enough completed candles.",
        }

    # H1 trend filter.
    hourly["ema50"] = hourly["close"].ewm(
        span=50, adjust=False
    ).mean()

    hourly["ema200"] = hourly["close"].ewm(
        span=200, adjust=False
    ).mean()

    trend = hourly.iloc[-1]

    # M15 setup.
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
        stop_loss = float(
            min(fifteen.iloc[-5:]["low"].min(), last["low"])
        )
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
        stop_loss = float(
            max(fifteen.iloc[-5:]["high"].max(), last["high"])
        )
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
            app.logger.exception(
                "Signal calculation failed for %s", symbol
            )

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
        app.logger.exception(
            "Signal calculation failed for %s", symbol
        )

        return jsonify({
            "market": MARKETS[symbol],
            "symbol": symbol,
            "signal": "UNAVAILABLE",
            "reason": "Market data or analysis is temporarily unavailable.",
        }), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000,
      )
