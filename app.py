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

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "AtlasSignals/1.0"})


def get_candles(symbol, interval, limit=250):
    from datetime import datetime, timedelta, timezone

    url = f"{API_URL}/{symbol}/ohlc"

    now = datetime.now(timezone.utc)
    chunk_days = 7 if interval == "1h" else 2
    collected = []

    for step in range(30):
        end_time = now - timedelta(days=step * chunk_days)
        start_time = end_time - timedelta(days=chunk_days)

        response = requests.get(
            url,
            params={
                "interval": interval,
                "limit": 1000,
                "from": start_time.isoformat(),
                "to": end_time.isoformat(),
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Unexpected API response.")

        bars = payload.get("bars", [])

        if not isinstance(bars, list):
            raise ValueError("Invalid candle data.")

        collected.extend(bars)

        if len(collected) >= limit + 1:
            break

    if not collected:
        raise ValueError("No candle data available.")

    df = pd.DataFrame(collected)

    required = ["openTime", "open", "high", "low", "close"]

    if not all(column in df.columns for column in required):
        raise ValueError("Candle data is missing required fields.")

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    if "isOpen" in df.columns:
        df = df[
            ~df["isOpen"].astype(str).str.lower().isin(
                ["true", "1"]
            )
        ]

    df = df.drop_duplicates(subset=["openTime"])
    df = df.sort_values("openTime").reset_index(drop=True)

    if len(df) < limit:
        raise ValueError(
            f"Only {len(df)} completed candles available; "
            f"{limit} required."
        )

    return df.tail(limit).reset_index(drop=True)


def analyse(symbol):
    hourly = get_candles(symbol, "1h", 300)
    fifteen = get_candles(symbol, "15m", 150)

    counts = {
        "H1": len(hourly),
        "M15": len(fifteen),
    }

    if len(hourly) < 200 or len(fifteen) < 15:
        return {
            "market": MARKETS[symbol],
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "Not enough completed candles.",
            "candles_available": counts,
            "candles_required": {
                "H1": 200,
                "M15": 15,
            },
        }

    # Higher-timeframe trend filter.
    hourly["ema50"] = hourly["close"].ewm(
        span=50,
        adjust=False,
    ).mean()

    hourly["ema200"] = hourly["close"].ewm(
        span=200,
        adjust=False,
    ).mean()

    trend = hourly.iloc[-1]

    # Lower-timeframe sweep confirmation.
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
        "candles_available": counts,
        "candle_time": str(last["openTime"]),
    }

    if bullish_trend and bullish_sweep:
        entry = float(last["close"])
        stop_loss = float(
            fifteen.iloc[-5:]["low"].min()
        )
        risk = entry - stop_loss

        if risk > 0:
            result.update({
                "signal": "BUY",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": entry + 2 * risk,
                "reason": (
                    "Bullish trend and sweep conditions met."
                ),
            })

    elif bearish_trend and bearish_sweep:
        entry = float(last["close"])
        stop_loss = float(
            fifteen.iloc[-5:]["high"].max()
        )
        risk = stop_loss - entry

        if risk > 0:
            result.update({
                "signal": "SELL",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": entry - 2 * risk,
                "reason": (
                    "Bearish trend and sweep conditions met."
                ),
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
                "Analysis failed for %s",
                symbol,
            )

            results[symbol] = {
                "market": MARKETS[symbol],
                "symbol": symbol,
                "signal": "UNAVAILABLE",
                "reason": (
                    "Market data or analysis is unavailable."
                ),
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
            "Analysis failed for %s",
            symbol,
        )

        return jsonify({
            "market": MARKETS[symbol],
            "symbol": symbol,
            "signal": "UNAVAILABLE",
            "reason": (
                "Market data or analysis is unavailable."
            ),
        }), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000,
                )
