
from flask import Flask, jsonify, render_template_string, request
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

app = Flask(__name__)

# -----------------------------------------
# CONFIGURATION
# -----------------------------------------

INDEXES = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX"
}

OPTION_UNDERLYINGS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX"
}

cache = {}


# -----------------------------------------
# UPSTOX API CONNECTION
# -----------------------------------------

class UpstoxAPIError(Exception):

    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def upstox_get(endpoint, params=None):

    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        raise UpstoxAPIError(
            500,
            "UPSTOX_ACCESS_TOKEN is missing in Render."
        )

    url = "https://api.upstox.com" + endpoint

    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "SUMIT-AI-TRADING-TERMINAL/1.0"
        },
        method="GET"
    )

    try:

        with urllib.request.urlopen(req, timeout=20) as response:

            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:

        raw = error.read().decode(
            "utf-8", errors="replace"
        )

        message = "Upstox API request failed."

        try:
            body = json.loads(raw)

            errors = body.get("errors") or []

            if errors:
                message = errors[0].get(
                    "message", message
                )
            else:
                message = body.get(
                    "detail",
                    body.get("message", message)
                )

            if body.get("error_code") == 1010:
                message = (
                    "Cloudflare Error 1010: "
                    "Upstox security blocked this request."
                )

        except Exception:
            pass

        # Never expose the access token
        message = str(message).replace(
            token, "[REDACTED]"
        )

        raise UpstoxAPIError(
            error.code,
            message
        )

    except urllib.error.URLError:
        raise UpstoxAPIError(
            503,
            "Unable to reach Upstox servers."
        )


def number(value, default=0):

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# -----------------------------------------
# MARKET DATA
# -----------------------------------------

def get_market_quotes():

    instrument_keys = ",".join(
        INDEXES.values()
    )

    result = upstox_get(
        "/v2/market-quote/quotes",
        {
            "instrument_key": instrument_keys
        }
    )

    market_data = result.get("data", {})

    output = {}

    for name, instrument_key in INDEXES.items():

        quote = None

        # Upstox may return keys with colon separators
        expected_key = instrument_key.replace("|", ":")

        quote = market_data.get(expected_key)

        if quote is None:
            quote = market_data.get(instrument_key)

        # Fallback: match instrument token
        if quote is None:

            for item in market_data.values():

                if item.get("instrument_token") == instrument_key:
                    quote = item
                    break

        if quote is None:

            output[name] = {
                "error": "Quote not returned by Upstox"
            }

            continue

        ohlc = quote.get("ohlc", {})

        last_price = number(
            quote.get("last_price")
        )

        previous_close = number(
            ohlc.get("close")
        )

        change = number(
            quote.get("net_change")
        )

        change_percent = 0

        if previous_close:
            change_percent = (
                change / previous_close
            ) * 100

        output[name] = {
            "last_price": last_price,
            "change": change,
            "change_percent": round(
                change_percent, 2
            ),
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "previous_close": previous_close
        }

    return output


# -----------------------------------------
# OPTION EXPIRIES
# -----------------------------------------

def get_option_expiries():

    result = upstox_get(
        "/v2/option/contract",
        {
            "instrument_key":
                OPTION_UNDERLYINGS["NIFTY"]
        }
    )

    contracts = result.get("data", [])

    expiries = sorted(set(
        item.get("expiry")
        for item in contracts
        if item.get("expiry")
    ))

    return {
        "expiries": expiries,
        "total_contracts": len(contracts)
    }


# -----------------------------------------
# OPTION CHAIN
# -----------------------------------------

def calculate_max_pain(rows):

    if not rows:
        return None

    strikes = [
        number(row.get("strike_price"))
        for row in rows
    ]

    min_pain = None
    max_pain_strike = None

    for settlement_price in strikes:

        total_pain = 0

        for row in rows:

            strike = number(
                row.get("strike_price")
            )

            call_oi = number(
                row.get("call_options", {})
                   .get("market_data", {})
                   .get("oi")
            )

            put_oi = number(
                row.get("put_options", {})
                   .get("market_data", {})
                   .get("oi")
            )

            # Call writer intrinsic payout
            call_pain = (
                max(settlement_price - strike, 0)
                * call_oi
            )

            # Put writer intrinsic payout
            put_pain = (
                max(strike - settlement_price, 0)
                * put_oi
            )

            total_pain += call_pain + put_pain

        if min_pain is None or total_pain < min_pain:

            min_pain = total_pain
            max_pain_strike = settlement_price

    return max_pain_strike


def get_option_chain(expiry):

    if not expiry:
        raise UpstoxAPIError(
            400, "Please select an expiry date."
        )

    result = upstox_get(
        "/v2/option/chain",
        {
            "instrument_key":
                OPTION_UNDERLYINGS["NIFTY"],
            "expiry_date": expiry
        }
    )

    raw_rows = result.get("data", [])

    rows = []

    total_call_oi = 0
    total_put_oi = 0

    spot_price = 0

    for item in raw_rows:

        call = item.get("call_options") or {}
        put = item.get("put_options") or {}

        call_market = call.get("market_data") or {}
        put_market = put.get("market_data") or {}

        call_greeks = call.get("option_greeks") or {}
        put_greeks = put.get("option_greeks") or {}

        call_oi = number(call_market.get("oi"))
        put_oi = number(put_market.get("oi"))

        call_prev_oi = number(
            call_market.get("prev_oi")
        )

        put_prev_oi = number(
            put_market.get("prev_oi")
        )

        total_call_oi += call_oi
        total_put_oi += put_oi

        spot_price = number(
            item.get("underlying_spot_price"),
            spot_price
        )

        rows.append({

            "strike": number(
                item.get("strike_price")
            ),

            "call": {
                "ltp": number(call_market.get("ltp")),
                "oi": call_oi,
                "oi_change": call_oi - call_prev_oi,
                "volume": number(call_market.get("volume")),
                "iv": number(call_greeks.get("iv")),
                "delta": number(call_greeks.get("delta")),
                "bid": number(call_market.get("bid_price")),
                "ask": number(call_market.get("ask_price"))
            },

            "put": {
                "ltp": number(put_market.get("ltp")),
                "oi": put_oi,
                "oi_change": put_oi - put_prev_oi,
                "volume": number(put_market.get("volume")),
                "iv": number(put_greeks.get("iv")),
                "delta": number(put_greeks.get("delta")),
                "bid": number(put_market.get("bid_price")),
                "ask": number(put_market.get("ask_price"))
            }

        })

    rows.sort(
        key=lambda x: x["strike"]
    )

    pcr = 0

    if total_call_oi:
        pcr = total_put_oi / total_call_oi

    max_pain = calculate_max_pain(raw_rows)

    return {
        "expiry": expiry,
        "spot_price": spot_price,
        "pcr": round(pcr, 3),
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "max_pain": max_pain,
        "total_strikes": len(rows),
        "data": rows
    }


# -----------------------------------------
# HTML DASHBOARD
# -----------------------------------------

DASHBOARD = r"""
<!DOCTYPE html>
<html>
<head>

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>SUMIT AI TRADING TERMINAL</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    padding: 18px;
    background: #0b1220;
    color: white;
    font-family: Arial, sans-serif;
}

header {
    text-align: center;
    margin-bottom: 25px;
}

header h1 {
    color: #38bdf8;
    font-size: 26px;
}

.subtitle {
    color: #94a3b8;
}

.section {
    margin-top: 28px;
}

.section h2 {
    color: #38bdf8;
    font-size: 20px;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(210px, 1fr));
    gap: 15px;
}

.card {
    background: #172338;
    border: 1px solid #263851;
    border-radius: 12px;
    padding: 18px;
}

.card-title {
    color: #94a3b8;
    font-size: 14px;
}

.price {
    font-size: 29px;
    font-weight: bold;
    margin: 12px 0;
}

.positive {
    color: #22c55e;
}

.negative {
    color: #ef4444;
}

.details {
    color: #cbd5e1;
    font-size: 13px;
    line-height: 1.8;
}

.controls {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
    margin: 15px 0;
}

select, button {
    padding: 12px;
    border-radius: 7px;
    border: 1px solid #334155;
    background: #172338;
    color: white;
    font-size: 14px;
}

button {
    background: #0284c7;
    border: none;
    cursor: pointer;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin: 18px 0;
}

.stat {
    background: #172338;
    padding: 15px;
    border-radius: 10px;
}

.stat-label {
    font-size: 12px;
    color: #94a3b8;
}

.stat-value {
    font-size: 21px;
    margin-top: 8px;
    font-weight: bold;
}

.table-wrap {
    overflow-x: auto;
    background: #172338;
    border-radius: 10px;
}

table {
    width: 100%;
    min-width: 850px;
    border-collapse: collapse;
    font-size: 12px;
    text-align: right;
}

th, td {
    padding: 12px 9px;
    border-bottom: 1px solid #334155;
    white-space: nowrap;
}

th {
    background: #24344d;
    color: #bae6fd;
}

td.strike {
    text-align: center;
    font-weight: bold;
    color: #facc15;
}

.atm {
    background: #29415b;
}

.message {
    color: #fbbf24;
    padding: 12px;
}

.footer {
    text-align: center;
    color: #64748b;
    font-size: 12px;
    margin: 35px 0 15px;
}

@media(max-width:600px) {
    body {
        padding: 12px;
    }

    header h1 {
        font-size: 21px;
    }
}

</style>
</head>

<body>

<header>
    <h1>SUMIT AI TRADING TERMINAL</h1>
    <div class="subtitle">
        Indian Market | Upstox API Dashboard
    </div>
</header>


<div class="section">

    <h2>LIVE MARKET OVERVIEW</h2>

    <div class="grid" id="market">
        <div class="card">Loading market data...</div>
    </div>

    <p class="subtitle" id="market-status">
        Connecting to Upstox...
    </p>

</div>


<div class="section">

    <h2>NIFTY 50 OPTION CHAIN</h2>

    <div class="controls">

        <label for="expiry">Select Expiry:</label>

        <select id="expiry">
            <option value="">Loading expiries...</option>
        </select>

        <button onclick="loadOptionChain()">
            Refresh Option Chain
        </button>

    </div>

    <div id="option-status" class="message">
        Loading expiry dates...
    </div>

    <div class="stats">

        <div class="stat">
            <div class="stat-label">NIFTY SPOT</div>
            <div class="stat-value" id="spot">—</div>
        </div>

        <div class="stat">
            <div class="stat-label">TOTAL PCR</div>
            <div class="stat-value" id="pcr">—</div>
        </div>

        <div class="stat">
            <div class="stat-label">MAX PAIN (EST.)</div>
            <div class="stat-value" id="maxpain">—</div>
        </div>

        <div class="stat">
            <div class="stat-label">TOTAL STRIKES</div>
            <div class="stat-value" id="strikes">—</div>
        </div>

    </div>

    <div class="stats">

        <div class="stat">
            <div class="stat-label">TOTAL CALL OI</div>
            <div class="stat-value" id="calloi">—</div>
        </div>

        <div class="stat">
            <div class="stat-label">TOTAL PUT OI</div>
            <div class="stat-value" id="putoi">—</div>
        </div>

    </div>

    <div class="table-wrap">

        <table>

            <thead>
                <tr>
                    <th colspan="4">CALL OPTIONS (CE)</th>
                    <th>STRIKE</th>
                    <th colspan="4">PUT OPTIONS (PE)</th>
                </tr>

                <tr>
                    <th>OI</th>
                    <th>OI CHANGE</th>
                    <th>LTP</th>
                    <th>IV</th>
                    <th>PRICE</th>
                    <th>IV</th>
                    <th>LTP</th>
                    <th>OI CHANGE</th>
                    <th>OI</th>
                </tr>
            </thead>

            <tbody id="option-table">

                <tr>
                    <td colspan="9">
                        Select an expiry to load option chain.
                    </td>
                </tr>

            </tbody>

        </table>

    </div>

</div>


<div class="footer">
    SUMIT AI TRADING TERMINAL |
    Market data powered by Upstox
    <br><br>
    Read-only dashboard. No orders are placed.
</div>


<script>

function formatNumber(value) {

    if (value === null || value === undefined) {
        return "—";
    }

    return Number(value).toLocaleString("en-IN", {
        maximumFractionDigits: 2
    });
}


async function loadMarket() {

    const status = document.getElementById(
        "market-status"
    );

    try {

        const response = await fetch("/api/market");

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || "Market API failed");
        }

        let content = "";

        for (const [name, item] of Object.entries(result.data)) {

            if (item.error) {

                content += `
                    <div class="card">
                        <div class="card-title">${name}</div>
                        <p>${item.error}</p>
                    </div>
                `;

                continue;
            }

            const positive = item.change >= 0;

            const color = positive
                ? "positive"
                : "negative";

            const sign = positive ? "+" : "";

            content += `
                <div class="card">

                    <div class="card-title">${name}</div>

                    <div class="price">
                        ${formatNumber(item.last_price)}
                    </div>

                    <div class="${color}">
                        ${sign}${formatNumber(item.change)}
                        (${sign}${item.change_percent}%)
                    </div>

                    <div class="details">
                        Open: ${formatNumber(item.open)}<br>
                        High: ${formatNumber(item.high)}<br>
                        Low: ${formatNumber(item.low)}<br>
                        Previous Close:
                        ${formatNumber(item.previous_close)}
                    </div>

                </div>
            `;
        }

        document.getElementById(
            "market"
        ).innerHTML = content;

        status.innerText =
            "Last updated: " + result.updated_at;

    } catch (error) {

        status.innerText = "Market Error: " + error.message;
    }
}


async function loadExpiries() {

    const select = document.getElementById("expiry");

    const status = document.getElementById(
        "option-status"
    );

    try {

        const response = await fetch(
            "/api/option-expiries"
        );

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || "Expiry API failed");
        }

        select.innerHTML = "";

        if (!result.expiries.length) {

            status.innerText =
                "No option expiry dates returned by Upstox.";

            return;
        }

        result.expiries.forEach(function(date) {

            const option = document.createElement("option");

            option.value = date;
            option.textContent = date;

            select.appendChild(option);
        });

        status.innerText =
            result.total_contracts +
            " option contracts available.";

        await loadOptionChain();

    } catch (error) {

        status.innerText =
            "Expiry Error: " + error.message;
    }
}


async function loadOptionChain() {

    const expiry = document.getElementById(
        "expiry"
    ).value;

    const status = document.getElementById(
        "option-status"
    );

    if (!expiry) {
        status.innerText = "Please select an expiry.";
        return;
    }

    status.innerText = "Loading NIFTY option chain...";

    try {

        const response = await fetch(
            "/api/option-chain?expiry=" +
            encodeURIComponent(expiry)
        );

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || "Option chain failed");
        }

        document.getElementById("spot").innerText =
            formatNumber(result.spot_price);

        document.getElementById("pcr").innerText =
            formatNumber(result.pcr);

        document.getElementById("maxpain").innerText =
            formatNumber(result.max_pain);

        document.getElementById("strikes").innerText =
            result.total_strikes;

        document.getElementById("calloi").innerText =
            formatNumber(result.total_call_oi);

        document.getElementById("putoi").innerText =
            formatNumber(result.total_put_oi);

        const tbody = document.getElementById(
            "option-table"
        );

        tbody.innerHTML = "";

        if (!result.data.length) {

            tbody.innerHTML = `
                <tr>
                    <td colspan="9">No option data available.</td>
                </tr>
            `;

            return;
        }

        result.data.forEach(function(row) {

            const tr = document.createElement("tr");

            const isATM =
                Math.abs(row.strike - result.spot_price) <
                Math.abs(
                    result.data.reduce(function(previous, current) {

                        return Math.abs(
                            current.strike - result.spot_price
                        ) <
                        Math.abs(
                            previous.strike - result.spot_price
                        )
                        ? current
                        : previous;

                    }).strike - result.spot_price
                ) + 0.001;

            if (isATM) {
                tr.className = "atm";
            }

            const values = [

                formatNumber(row.call.oi),
                formatNumber(row.call.oi_change),
                formatNumber(row.call.ltp),
                formatNumber(row.call.iv),

                formatNumber(row.strike),

                formatNumber(row.put.iv),
                formatNumber(row.put.ltp),
                formatNumber(row.put.oi_change),
                formatNumber(row.put.oi)

            ];

            values.forEach(function(value, index) {

                const td = document.createElement("td");

                td.textContent = value;

                if (index === 4) {
                    td.className = "strike";
                }

                tr.appendChild(td);
            });

            tbody.appendChild(tr);
        });

        status.innerText =
            "Option chain updated for expiry " + expiry;

    } catch (error) {

        status.innerText =
            "Option Chain Error: " + error.message;
    }
}


document.getElementById("expiry").addEventListener(
    "change",
    loadOptionChain
);


// Initial loading
loadMarket();
loadExpiries();


// Refresh market data every 15 seconds
setInterval(loadMarket, 15000);

// Refresh option chain every 30 seconds
setInterval(function() {

    if (document.getElementById("expiry").value) {
        loadOptionChain();
    }

}, 30000);

</script>

</body>
</html>
"""


# -----------------------------------------
# FLASK ROUTES
# -----------------------------------------

@app.route("/")
def home():
    return render_template_string(DASHBOARD)


@app.route("/api/market")
def market_api():

    key = "market"
    current_time = time.time()

    if (
        key in cache
        and current_time - cache[key]["time"] < 10
    ):

        return jsonify(cache[key]["data"])

    try:

        data = get_market_quotes()

        response = {
            "status": "success",
            "data": data,
            "updated_at": datetime.now().strftime(
                "%d-%m-%Y %I:%M:%S %p"
            )
        }

        cache[key] = {
            "time": current_time,
            "data": response
        }

        return jsonify(response)

    except UpstoxAPIError as error:

        return jsonify({
            "status": "error",
            "error": error.message
        }), error.status

    except Exception:

        app.logger.exception("Market API error")

        return jsonify({
            "status": "error",
            "error": "Market data unavailable."
        }), 500


@app.route("/api/option-expiries")
def option_expiries_api():

    key = "option_expiries"
    current_time = time.time()

    if (
        key in cache
        and current_time - cache[key]["time"] < 300
    ):

        return jsonify(cache[key]["data"])

    try:

        data = get_option_expiries()

        response = {
            "status": "success",
            "expiries": data["expiries"],
            "total_contracts": data["total_contracts"]
        }

        cache[key] = {
            "time": current_time,
            "data": response
        }

        return jsonify(response)

    except UpstoxAPIError as error:

        return jsonify({
            "status": "error",
            "error": error.message
        }), error.status

    except Exception:

        app.logger.exception("Expiry API error")

        return jsonify({
            "status": "error",
            "error": "Unable to fetch option expiries."
        }), 500


@app.route("/api/option-chain")
def option_chain_api():

    expiry = request.args.get("expiry", "").strip()

    if not expiry:
        return jsonify({
            "status": "error",
            "error": "Expiry date is required."
        }), 400

    # Validate date format
    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return jsonify({
            "status": "error",
            "error": "Invalid expiry date format."
        }), 400

    key = "option_chain_" + expiry
    current_time = time.time()

    if (
        key in cache
        and current_time - cache[key]["time"] < 20
    ):

        return jsonify(cache[key]["data"])

    try:

        data = get_option_chain(expiry)

        response = {
            "status": "success",
            **data
        }

        cache[key] = {
            "time": current_time,
            "data": response
        }

        return jsonify(response)

    except UpstoxAPIError as error:

        return jsonify({
            "status": "error",
            "error": error.message
        }), error.status

    except Exception:

        app.logger.exception("Option chain API error")

        return jsonify({
            "status": "error",
            "error": "Unable to fetch option chain."
        }), 500


@app.route("/upstox-test")
def upstox_test():

    try:

        result = upstox_get(
            "/v2/user/profile"
        )

        user = result.get("data", {})

        return jsonify({
            "status": "success",
            "message": "Upstox Connected Successfully!",
            "broker": user.get("broker"),
            "is_active": user.get("is_active")
        })

    except UpstoxAPIError as error:

        return jsonify({
            "status": "error",
            "http_status": error.status,
            "message": error.message
        }), error.status


# -----------------------------------------
# START APPLICATION
# -----------------------------------------

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
