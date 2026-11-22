
from flask import Flask, jsonify
import os
import json
import html
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

app = Flask(__name__)

INDEXES = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX"
}

cache = {
    "time": 0,
    "data": None
}


def get_market_quotes():

    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        raise Exception("UPSTOX_ACCESS_TOKEN is missing in Render.")

    keys = ",".join(INDEXES.values())

    query = urllib.parse.urlencode({
        "instrument_key": keys
    })

    url = (
        "https://api.upstox.com/v2/market-quote/quotes?"
        + query
    )

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "SUMIT-AI-TRADING-TERMINAL/1.0"
        },
        method="GET"
    )

    with urllib.request.urlopen(request, timeout=20) as response:

        result = json.loads(
            response.read().decode("utf-8")
        )

    market_data = result.get("data", {})
    output = {}

    for name, instrument_key in INDEXES.items():

        expected_key = instrument_key.replace("|", ":")
        quote = market_data.get(expected_key)

        if quote is None:
            quote = market_data.get(instrument_key)

        if quote is None:
            for item in market_data.values():
                if item.get("instrument_token") == instrument_key:
                    quote = item
                    break

        if quote:

            ltp = quote.get("last_price")
            net_change = quote.get("net_change", 0)
            previous_close = (
                quote.get("ohlc", {}).get("close")
            )

            change_percent = 0

            if previous_close:
                change_percent = (
                    net_change / previous_close
                ) * 100

            output[name] = {
                "last_price": ltp,
                "change": net_change,
                "change_percent": round(change_percent, 2),
                "open": quote.get("ohlc", {}).get("open"),
                "high": quote.get("ohlc", {}).get("high"),
                "low": quote.get("ohlc", {}).get("low"),
                "previous_close": previous_close
            }

        else:
            output[name] = {
                "error": "Quote not returned by Upstox"
            }

    return output


@app.route("/")
def home():

    return """
<!DOCTYPE html>
<html>
<head>
    <title>SUMIT AI TRADING TERMINAL</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {
            margin: 0;
            padding: 20px;
            background: #101827;
            color: white;
            font-family: Arial, sans-serif;
        }

        h1 {
            color: #38bdf8;
            text-align: center;
        }

        .subtitle {
            text-align: center;
            color: #9ca3af;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(
                auto-fit, minmax(240px, 1fr)
            );
            gap: 18px;
            margin-top: 30px;
        }

        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 22px;
        }

        .name {
            font-size: 18px;
            color: #94a3b8;
        }

        .price {
            font-size: 32px;
            font-weight: bold;
            margin: 15px 0;
        }

        .positive {
            color: #22c55e;
        }

        .negative {
            color: #ef4444;
        }

        .details {
            color: #cbd5e1;
            line-height: 1.9;
            font-size: 14px;
        }

        button {
            background: #0284c7;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 12px 22px;
            cursor: pointer;
            font-size: 15px;
            margin-top: 20px;
        }

        .center {
            text-align: center;
        }

        #status {
            color: #94a3b8;
            margin-top: 20px;
        }

        .footer {
            text-align: center;
            color: #64748b;
            margin-top: 40px;
            font-size: 12px;
        }
    </style>
</head>

<body>

    <h1>SUMIT AI TRADING TERMINAL</h1>

    <p class="subtitle">
        Professional Indian Stock Market Dashboard
    </p>

    <div class="center">
        <button onclick="loadMarket()">
            Refresh Market Data
        </button>

        <p id="status">Connecting to Upstox...</p>
    </div>

    <div class="grid" id="market">
        <div class="card">Loading market data...</div>
    </div>

    <div class="footer">
        Powered by Upstox API | Read-only Market Dashboard
    </div>

<script>

async function loadMarket() {

    const status = document.getElementById("status");

    status.innerText = "Fetching market data...";

    try {

        const response = await fetch("/api/market");

        const result = await response.json();

        if (!response.ok) {
            throw new Error(
                result.error || "Market API failed"
            );
        }

        const market = result.data;

        let html = "";

        for (const [name, item] of Object.entries(market)) {

            if (item.error) {

                html += `
                <div class="card">
                    <div class="name">${name}</div>
                    <p>${item.error}</p>
                </div>`;

                continue;
            }

            const positive = item.change >= 0;

            const color = positive
                ? "positive"
                : "negative";

            const sign = positive ? "+" : "";

            html += `
            <div class="card">

                <div class="name">${name}</div>

                <div class="price">
                    ${Number(item.last_price).toLocaleString(
                        "en-IN",
                        {minimumFractionDigits: 2}
                    )}
                </div>

                <div class="${color}">
                    ${sign}${Number(item.change).toFixed(2)}
                    (${sign}${item.change_percent}%)
                </div>

                <div class="details">
                    Open: ${item.open ?? "—"}<br>
                    High: ${item.high ?? "—"}<br>
                    Low: ${item.low ?? "—"}<br>
                    Previous Close: ${item.previous_close ?? "—"}
                </div>

            </div>`;
        }

        document.getElementById("market").innerHTML = html;

        status.innerText =
            "Market data updated: " + result.updated_at;

    } catch (error) {

        status.innerText = "Error: " + error.message;
    }
}

loadMarket();

setInterval(loadMarket, 15000);

</script>

</body>
</html>
"""


@app.route("/api/market")
def market_api():

    try:

        current_time = time.time()

        if (
            cache["data"] is not None
            and current_time - cache["time"] < 10
        ):
            return jsonify({
                "status": "success",
                "data": cache["data"],
                "updated_at": cache["updated_at"]
            })

        data = get_market_quotes()

        updated_at = datetime.now().strftime(
            "%d-%m-%Y %I:%M:%S %p"
        )

        cache["time"] = current_time
        cache["data"] = data
        cache["updated_at"] = updated_at

        return jsonify({
            "status": "success",
            "data": data,
            "updated_at": updated_at
        })

    except urllib.error.HTTPError as error:

        return jsonify({
            "status": "error",
            "error": "Upstox API returned HTTP " + str(error.code)
        }), error.code

    except Exception:

        app.logger.exception("Market data request failed")

        return jsonify({
            "status": "error",
            "error": "Market data unavailable. Check Render logs."
        }), 500


@app.route("/upstox-test")
def upstox_test():

    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        return "Access token missing", 500

    request = urllib.request.Request(
        "https://api.upstox.com/v2/user/profile",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "SUMIT-AI-TRADING-TERMINAL/1.0"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return "Upstox Connected Successfully! HTTP 200"

    except urllib.error.HTTPError as error:
        return "Upstox API Error: HTTP " + str(error.code), error.code


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
