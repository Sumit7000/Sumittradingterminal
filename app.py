
import os
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

UPSTOX_ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()

BASE_URL = "https://api.upstox.com/v2"

# Index keys used for live market quotes and option chains.
INDEXES = {
    "NIFTY": {
        "name": "NIFTY 50",
        "key": "NSE_INDEX|Nifty 50"
    },
    "BANKNIFTY": {
        "name": "BANK NIFTY",
        "key": "NSE_INDEX|Nifty Bank"
    },
    "SENSEX": {
        "name": "SENSEX",
        "key": "BSE_INDEX|SENSEX"
    }
}


# --------------------------------------------------
# UPSTOX API HELPER
# --------------------------------------------------

def upstox_get(endpoint, params=None):
    if not UPSTOX_ACCESS_TOKEN:
        raise Exception("UPSTOX_ACCESS_TOKEN is missing in Render environment variables.")

    url = BASE_URL + endpoint

    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "User-Agent": "SUMIT-AI-TRADING-TERMINAL/1.0"
    }

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise Exception(f"Upstox HTTP {error.code}: {body[:700]}")

    except urllib.error.URLError as error:
        raise Exception(f"Connection error: {error.reason}")


def number(value, default=0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def valid_symbol(symbol):
    symbol = (symbol or "NIFTY").upper().strip()

    if symbol not in INDEXES:
        raise ValueError("Invalid index. Select NIFTY, BANKNIFTY or SENSEX.")

    return symbol


# --------------------------------------------------
# LIVE MARKET DATA - ALL THREE INDICES
# --------------------------------------------------

@app.route("/api/market")
def market_data():
    try:
        keys = [item["key"] for item in INDEXES.values()]

        result = upstox_get(
            "/market-quote/quotes",
            {"instrument_key": ",".join(keys)}
        )

        raw_data = result.get("data", {})
        market = {}

        for symbol, config in INDEXES.items():
            key = config["key"]

            # Upstox returns quote objects keyed by instrument key.
            quote = raw_data.get(key)

            if quote is None:
                # Fallback in case the response key format differs.
                quote = next(
                    (
                        value for value in raw_data.values()
                        if value.get("instrument_token") == key
                    ),
                    None
                )

            if not quote:
                market[symbol] = {
                    "name": config["name"],
                    "available": False,
                    "message": "Quote not returned by Upstox"
                }
                continue

            ltp = number(quote.get("last_price"))
            net_change = number(quote.get("net_change"))

            ohlc = quote.get("ohlc") or {}
            previous_close = number(ohlc.get("close"))

            percent = (
                (net_change / previous_close) * 100
                if previous_close else 0
            )

            market[symbol] = {
                "name": config["name"],
                "available": True,
                "ltp": ltp,
                "change": net_change,
                "percent": percent,
                "open": number(ohlc.get("open")),
                "high": number(ohlc.get("high")),
                "low": number(ohlc.get("low")),
                "close": previous_close,
                "volume": quote.get("volume", 0)
            }

        return jsonify({
            "success": True,
            "market": market,
            "updated_at": datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# --------------------------------------------------
# OPTION EXPIRIES - SELECTED INDEX
# --------------------------------------------------

@app.route("/api/option-expiries")
def option_expiries():
    try:
        symbol = valid_symbol(request.args.get("symbol"))
        instrument_key = INDEXES[symbol]["key"]

        result = upstox_get(
            "/option/contract",
            {"instrument_key": instrument_key}
        )

        contracts = result.get("data", [])

        expiries = sorted({
            contract.get("expiry")
            for contract in contracts
            if contract.get("expiry")
        })

        return jsonify({
            "success": True,
            "symbol": symbol,
            "name": INDEXES[symbol]["name"],
            "expiries": expiries
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
            "expiries": []
        }), 400


# --------------------------------------------------
# MAX PAIN CALCULATION
# --------------------------------------------------

def calculate_max_pain(chain):
    strikes = []

    for row in chain:
        strike = number(row.get("strike_price"))

        call = row.get("call_options") or {}
        put = row.get("put_options") or {}

        call_data = call.get("market_data") or {}
        put_data = put.get("market_data") or {}

        strikes.append({
            "strike": strike,
            "call_oi": number(call_data.get("oi")),
            "put_oi": number(put_data.get("oi"))
        })

    if not strikes:
        return None

    lowest_pain = None
    max_pain_strike = None

    for settlement in [item["strike"] for item in strikes]:
        total_pain = 0

        for item in strikes:
            # Call writers' intrinsic payout.
            total_pain += (
                max(0, settlement - item["strike"])
                * item["call_oi"]
            )

            # Put writers' intrinsic payout.
            total_pain += (
                max(0, item["strike"] - settlement)
                * item["put_oi"]
            )

        if lowest_pain is None or total_pain < lowest_pain:
            lowest_pain = total_pain
            max_pain_strike = settlement

    return max_pain_strike


# --------------------------------------------------
# OPTION CHAIN - NIFTY / BANK NIFTY / SENSEX
# --------------------------------------------------

@app.route("/api/option-chain")
def option_chain():
    try:
        symbol = valid_symbol(request.args.get("symbol"))
        expiry = request.args.get("expiry", "").strip()

        if not expiry:
            return jsonify({
                "success": False,
                "error": "Please select an expiry date."
            }), 400

        try:
            datetime.strptime(expiry, "%Y-%m-%d")
        except ValueError:
            return jsonify({
                "success": False,
                "error": "Invalid expiry date format."
            }), 400

        instrument_key = INDEXES[symbol]["key"]

        result = upstox_get(
            "/option/chain",
            {
                "instrument_key": instrument_key,
                "expiry_date": expiry
            }
        )

        raw_chain = result.get("data", [])

        if not raw_chain:
            return jsonify({
                "success": True,
                "symbol": symbol,
                "expiry": expiry,
                "chain": [],
                "message": "No option-chain data returned for this expiry."
            })

        chain = []
        total_call_oi = 0
        total_put_oi = 0
        total_call_change = 0
        total_put_change = 0

        for row in raw_chain:
            call = row.get("call_options") or {}
            put = row.get("put_options") or {}

            call_data = call.get("market_data") or {}
            put_data = put.get("market_data") or {}

            call_greeks = call.get("option_greeks") or {}
            put_greeks = put.get("option_greeks") or {}

            call_oi = number(call_data.get("oi"))
            put_oi = number(put_data.get("oi"))

            call_prev_oi = number(call_data.get("prev_oi"))
            put_prev_oi = number(put_data.get("prev_oi"))

            call_oi_change = call_oi - call_prev_oi
            put_oi_change = put_oi - put_prev_oi

            total_call_oi += call_oi
            total_put_oi += put_oi

            total_call_change += call_oi_change
            total_put_change += put_oi_change

            chain.append({
                "strike": number(row.get("strike_price")),

                "call": {
                    "ltp": number(call_data.get("ltp")),
                    "oi": call_oi,
                    "oi_change": call_oi_change,
                    "volume": number(call_data.get("volume")),
                    "iv": number(call_greeks.get("iv")),
                    "delta": number(call_greeks.get("delta")),
                    "bid": number(call_data.get("bid_price")),
                    "ask": number(call_data.get("ask_price"))
                },

                "put": {
                    "ltp": number(put_data.get("ltp")),
                    "oi": put_oi,
                    "oi_change": put_oi_change,
                    "volume": number(put_data.get("volume")),
                    "iv": number(put_greeks.get("iv")),
                    "delta": number(put_greeks.get("delta")),
                    "bid": number(put_data.get("bid_price")),
                    "ask": number(put_data.get("ask_price"))
                }
            })

        chain.sort(key=lambda item: item["strike"])

        pcr = (
            total_put_oi / total_call_oi
            if total_call_oi else 0
        )

        max_pain = calculate_max_pain(raw_chain)

        spot = number(raw_chain[0].get("underlying_spot_price"))

        return jsonify({
            "success": True,
            "symbol": symbol,
            "name": INDEXES[symbol]["name"],
            "expiry": expiry,
            "spot": spot,
            "pcr": pcr,
            "max_pain": max_pain,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "call_oi_change": total_call_change,
            "put_oi_change": total_put_change,
            "chain": chain
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error)
        }), 400


# --------------------------------------------------
# UPSTOX CONNECTION TEST
# --------------------------------------------------

@app.route("/upstox-test")
def upstox_test():
    try:
        result = upstox_get("/user/profile")

        return jsonify({
            "success": True,
            "message": "Upstox connection successful",
            "profile_status": result.get("status")
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# --------------------------------------------------
# MAIN DASHBOARD
# --------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>SUMIT AI TRADING TERMINAL</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        * { box-sizing: border-box; }

        body {
            margin: 0;
            padding: 18px;
            background: #080d18;
            color: #e8eefc;
            font-family: Arial, sans-serif;
        }

        h1 {
            color: #42a5ff;
            margin-bottom: 5px;
        }

        .subtitle {
            color: #94a3b8;
            margin-bottom: 22px;
        }

        .cards {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
        }

        .card, .panel {
            background: #111a2b;
            border: 1px solid #24344e;
            border-radius: 12px;
            padding: 17px;
        }

        .index-name {
            color: #9eb3d1;
            font-size: 14px;
        }

        .price {
            font-size: 27px;
            font-weight: bold;
            margin: 12px 0 5px;
        }

        .green { color: #22c98a; }
        .red { color: #ff647c; }
        .muted { color: #9aa9c0; }

        .panel {
            margin-top: 22px;
            overflow: hidden;
        }

        select, button {
            background: #1b2b45;
            border: 1px solid #365172;
            color: white;
            padding: 11px;
            border-radius: 7px;
            font-size: 15px;
            margin: 5px 5px 10px 0;
        }

        button {
            background: #0879df;
            border: none;
            cursor: pointer;
        }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
            gap: 10px;
            margin: 15px 0;
        }

        .stat {
            background: #19263b;
            padding: 13px;
            border-radius: 8px;
        }

        .stat-label {
            color: #9eb3d1;
            font-size: 12px;
        }

        .stat-value {
            font-size: 19px;
            font-weight: bold;
            margin-top: 7px;
        }

        .table-wrap {
            overflow-x: auto;
            margin-top: 15px;
        }

        table {
            border-collapse: collapse;
            width: 100%;
            min-width: 850px;
            font-size: 13px;
        }

        th, td {
            padding: 10px 8px;
            border-bottom: 1px solid #29364c;
            text-align: right;
            white-space: nowrap;
        }

        th {
            background: #1b2b45;
            color: #d7e6ff;
        }

        th:nth-child(5), td:nth-child(5) {
            text-align: center;
            font-weight: bold;
        }

        .call-heading { color: #ff788a; }
        .put-heading { color: #31d9a0; }

        .message {
            color: #ffc66d;
            padding: 12px 0;
        }

        .footer {
            margin-top: 25px;
            color: #788ba8;
            font-size: 12px;
        }
    </style>
</head>

<body>

    <h1>SUMIT AI TRADING TERMINAL</h1>
    <div class="subtitle">
        Live Index Dashboard | Upstox API | Read-only
    </div>

    <h3>LIVE MARKET DATA</h3>

    <div class="cards">

        <div class="card">
            <div class="index-name">NIFTY 50</div>
            <div class="price" id="nifty-price">Loading...</div>
            <div id="nifty-change" class="muted">--</div>
            <div class="muted" id="nifty-range"></div>
        </div>

        <div class="card">
            <div class="index-name">BANK NIFTY</div>
            <div class="price" id="banknifty-price">Loading...</div>
            <div id="banknifty-change" class="muted">--</div>
            <div class="muted" id="banknifty-range"></div>
        </div>

        <div class="card">
            <div class="index-name">SENSEX</div>
            <div class="price" id="sensex-price">Loading...</div>
            <div id="sensex-change" class="muted">--</div>
            <div class="muted" id="sensex-range"></div>
        </div>

    </div>

    <div class="panel">

        <h3>OPTION CHAIN ANALYSIS</h3>

        <label for="symbol">Select Index:</label>

        <select id="symbol" onchange="loadExpiries()">
            <option value="NIFTY">NIFTY 50</option>
            <option value="BANKNIFTY">BANK NIFTY</option>
            <option value="SENSEX">SENSEX</option>
        </select>

        <label for="expiry">Expiry:</label>

        <select id="expiry">
            <option value="">Select expiry</option>
        </select>

        <button onclick="loadOptionChain()">Load Option Chain</button>

        <div id="status" class="message">
            Select an index and expiry to load option-chain data.
        </div>

        <div class="stats">

            <div class="stat">
                <div class="stat-label">UNDERLYING SPOT</div>
                <div class="stat-value" id="spot">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">PCR</div>
                <div class="stat-value" id="pcr">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">MAX PAIN</div>
                <div class="stat-value" id="maxpain">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">TOTAL CALL OI</div>
                <div class="stat-value" id="calloi">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">TOTAL PUT OI</div>
                <div class="stat-value" id="putoi">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">CALL OI CHANGE</div>
                <div class="stat-value" id="callchange">--</div>
            </div>

            <div class="stat">
                <div class="stat-label">PUT OI CHANGE</div>
                <div class="stat-value" id="putchange">--</div>
            </div>

        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th colspan="4" class="call-heading">CALL OPTIONS (CE)</th>
                        <th rowspan="2">STRIKE</th>
                        <th colspan="4" class="put-heading">PUT OPTIONS (PE)</th>
                    </tr>
                    <tr>
                        <th>LTP</th>
                        <th>OI</th>
                        <th>OI CHANGE</th>
                        <th>VOLUME</th>
                        <th>VOLUME</th>
                        <th>OI CHANGE</th>
                        <th>OI</th>
                        <th>LTP</th>
                    </tr>
                </thead>

                <tbody id="chain-body">
                    <tr>
                        <td colspan="9" style="text-align:center">
                            Option chain will appear here.
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

    </div>

    <div class="footer">
        Data provided by Upstox API. Market data may be delayed or unavailable.
        PCR and Max Pain are analytical indicators, not guaranteed predictions.
        This terminal does not place orders.
        <br><br>
        Last market refresh: <span id="updated">--</span>
    </div>


<script>

function fmt(value) {
    return Number(value || 0).toLocaleString("en-IN", {
        maximumFractionDigits: 2
    });
}

function compact(value) {
    return Number(value || 0).toLocaleString("en-IN", {
        maximumFractionDigits: 0
    });
}


// -----------------------------------
// LIVE MARKET CARDS
// -----------------------------------

async function loadMarket() {
    try {
        const response = await fetch("/api/market");
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || "Market data unavailable");
        }

        const market = data.market;

        const ids = {
            NIFTY: "nifty",
            BANKNIFTY: "banknifty",
            SENSEX: "sensex"
        };

        for (const symbol of Object.keys(ids)) {
            const id = ids[symbol];
            const item = market[symbol];

            if (!item || !item.available) {
                document.getElementById(id + "-price").textContent = "--";
                document.getElementById(id + "-change").textContent =
                    item ? item.message : "Data unavailable";
                continue;
            }

            document.getElementById(id + "-price").textContent =
                fmt(item.ltp);

            const change = Number(item.change);
            const percent = Number(item.percent);

            const changeElement =
                document.getElementById(id + "-change");

            changeElement.textContent =
                (change >= 0 ? "+" : "") +
                fmt(change) + " (" +
                (percent >= 0 ? "+" : "") +
                fmt(percent) + "%)";

            changeElement.className =
                change >= 0 ? "green" : "red";

            document.getElementById(id + "-range").textContent =
                "O: " + fmt(item.open) +
                " | H: " + fmt(item.high) +
                " | L: " + fmt(item.low);
        }

        document.getElementById("updated").textContent =
            data.updated_at || "--";

    } catch (error) {
        document.getElementById("updated").textContent =
            "Error: " + error.message;
    }
}


// -----------------------------------
// LOAD EXPIRIES FOR SELECTED INDEX
// -----------------------------------

async function loadExpiries() {
    const symbol = document.getElementById("symbol").value;
    const expirySelect = document.getElementById("expiry");
    const status = document.getElementById("status");

    expirySelect.innerHTML =
        '<option value="">Loading expiries...</option>';

    document.getElementById("chain-body").innerHTML = "";

    status.textContent = "Loading " + symbol + " expiries...";

    try {
        const response = await fetch(
            "/api/option-expiries?symbol=" +
            encodeURIComponent(symbol)
        );

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || "Unable to load expiries");
        }

        expirySelect.innerHTML =
            '<option value="">Select expiry</option>';

        if (!data.expiries || data.expiries.length === 0) {
            status.textContent =
                "No active expiries returned for " + symbol;
            return;
        }

        data.expiries.forEach(function(expiry) {
            const option = document.createElement("option");
            option.value = expiry;
            option.textContent = expiry;
            expirySelect.appendChild(option);
        });

        status.textContent =
            data.name + " expiries loaded. Select an expiry.";

    } catch (error) {
        expirySelect.innerHTML =
            '<option value="">No expiries available</option>';

        status.textContent = error.message;
    }
}


// -----------------------------------
// LOAD SELECTED OPTION CHAIN
// -----------------------------------

async function loadOptionChain() {
    const symbol = document.getElementById("symbol").value;
    const expiry = document.getElementById("expiry").value;
    const status = document.getElementById("status");

    if (!expiry) {
        status.textContent = "Please select an expiry first.";
        return;
    }

    status.textContent = "Loading " + symbol + " option chain...";

    try {
        const url =
            "/api/option-chain?symbol=" +
            encodeURIComponent(symbol) +
            "&expiry=" +
            encodeURIComponent(expiry);

        const response = await fetch(url);
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || "Option chain unavailable");
        }

        document.getElementById("spot").textContent =
            fmt(data.spot);

        document.getElementById("pcr").textContent =
            Number(data.pcr || 0).toFixed(2);

        document.getElementById("maxpain").textContent =
            data.max_pain == null ? "--" : fmt(data.max_pain);

        document.getElementById("calloi").textContent =
            compact(data.total_call_oi);

        document.getElementById("putoi").textContent =
            compact(data.total_put_oi);

        document.getElementById("callchange").textContent =
            compact(data.call_oi_change);

        document.getElementById("putchange").textContent =
            compact(data.put_oi_change);

        const tbody = document.getElementById("chain-body");
        tbody.innerHTML = "";

        if (!data.chain || data.chain.length === 0) {
            tbody.innerHTML =
                '<tr><td colspan="9">No option-chain rows returned.</td></tr>';

            status.textContent = data.message || "No chain data.";
            return;
        }

        data.chain.forEach(function(row) {
            const tr = document.createElement("tr");

            const values = [
                fmt(row.call.ltp),
                compact(row.call.oi),
                compact(row.call.oi_change),
                compact(row.call.volume),
                fmt(row.strike),
                compact(row.put.volume),
                compact(row.put.oi_change),
                compact(row.put.oi),
                fmt(row.put.ltp)
            ];

            values.forEach(function(value, index) {
                const td = document.createElement("td");
                td.textContent = value;

                if (index === 0 || index === 1 || index === 2 || index === 3) {
                    td.className = "call-heading";
                }

                if (index === 5 || index === 6 || index === 7 || index === 8) {
                    td.className = "put-heading";
                }

                tr.appendChild(td);
            });

            tbody.appendChild(tr);
        });

        status.textContent =
            symbol + " option chain loaded successfully for " + expiry;

    } catch (error) {
        status.textContent = error.message;

        document.getElementById("chain-body").innerHTML =
            '<tr><td colspan="9">' +
            error.message +
            '</td></tr>';
    }
}


// -----------------------------------
// INITIALIZE DASHBOARD
// -----------------------------------

loadMarket();
loadExpiries();

setInterval(loadMarket, 15000);

</script>

</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


# --------------------------------------------------
# START APPLICATION
# --------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
