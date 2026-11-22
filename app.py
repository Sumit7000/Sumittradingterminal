
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ==========================================================
# SUMIT AI TRADING TERMINAL
# Read-only trading decision support. No order placement.
# ==========================================================

UPSTOX_ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()

V2 = "https://api.upstox.com/v2"
V3 = "https://api.upstox.com/v3"
IST = ZoneInfo("Asia/Kolkata")

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

# ==========================================================
# SETTINGS
# ==========================================================

CANDLE_INTERVAL = 5
CANDLE_LOOKBACK = 60

VOLUME_MULTIPLIER = 1.20
BREAKOUT_BUFFER_PERCENT = 0.0005

ALERT_CACHE_SECONDS = 20
ALERT_HISTORY_SECONDS = 900

# Initial option premium stop estimate.
# 0.25 = 25% below option premium entry.
PREMIUM_STOP_PERCENT = 0.25

# Reject an option if bid/ask spread exceeds this percentage.
MAX_SPREAD_PERCENT = 8.0

MIN_RISK_REWARD = 2.0

# ==========================================================
# TEMPORARY SERVER STORAGE
# These reset if Render restarts.
# ==========================================================

cache = {}
alert_history = []
alerted_events = {}

active_plans = {}
plan_events = {}


# ==========================================================
# GENERAL HELPERS
# ==========================================================

def number(value, default=0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def current_time():
    return datetime.now(IST)


def formatted_time():
    return current_time().strftime("%d-%m-%Y %H:%M:%S")


def valid_symbol(symbol):
    symbol = (symbol or "NIFTY").upper().strip()

    if symbol not in INDEXES:
        raise ValueError(
            "Select NIFTY, BANKNIFTY or SENSEX."
        )

    return symbol


def api_get(base, endpoint, params=None):

    if not UPSTOX_ACCESS_TOKEN:
        raise Exception(
            "UPSTOX_ACCESS_TOKEN missing in Render environment."
        )

    url = base + endpoint

    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "User-Agent": "SUMIT-AI-TRADING-TERMINAL/2.0"
    }

    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise Exception(
            f"Upstox HTTP {error.code}: {body[:700]}"
        )

    except urllib.error.URLError as error:
        raise Exception(
            f"Connection error: {error.reason}"
        )


def upstox_v2(endpoint, params=None):
    return api_get(V2, endpoint, params)


def upstox_v3(endpoint):
    return api_get(V3, endpoint)


def quote_for_keys(keys):

    keys = [key for key in keys if key]

    if not keys:
        return {}

    result = upstox_v2(
        "/market-quote/quotes",
        {
            "instrument_key": ",".join(keys)
        }
    )

    return result.get("data", {}) or {}


def find_quote(quotes, instrument_key):

    if instrument_key in quotes:
        return quotes[instrument_key]

    for quote in quotes.values():

        if quote.get("instrument_token") == instrument_key:
            return quote

    return None


def quote_bid_ask(quote):

    depth = quote.get("depth") or {}

    buy = depth.get("buy") or []
    sell = depth.get("sell") or []

    bid = number(
        buy[0].get("price")
    ) if buy else number(quote.get("bid_price"))

    ask = number(
        sell[0].get("price")
    ) if sell else number(quote.get("ask_price"))

    return bid, ask


# ==========================================================
# LIVE MARKET DATA
# ==========================================================

@app.route("/api/market")
def market_data():

    try:

        keys = [
            item["key"]
            for item in INDEXES.values()
        ]

        quotes = quote_for_keys(keys)

        market = {}

        for symbol, config in INDEXES.items():

            quote = find_quote(
                quotes,
                config["key"]
            )

            if not quote:

                market[symbol] = {
                    "name": config["name"],
                    "available": False,
                    "message": "Quote unavailable"
                }

                continue

            ltp = number(
                quote.get("last_price")
            )

            change = number(
                quote.get("net_change")
            )

            ohlc = quote.get("ohlc") or {}

            previous_close = number(
                ohlc.get("close")
            )

            percent = (
                change / previous_close * 100
                if previous_close else 0
            )

            market[symbol] = {
                "name": config["name"],
                "available": True,
                "ltp": ltp,
                "change": change,
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
            "updated_at": formatted_time()
        })

    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ==========================================================
# OPTION EXPIRIES
# ==========================================================

def get_contracts(symbol):

    result = upstox_v2(
        "/option/contract",
        {
            "instrument_key": INDEXES[symbol]["key"]
        }
    )

    return result.get("data") or []


@app.route("/api/option-expiries")
def option_expiries():

    try:

        symbol = valid_symbol(
            request.args.get("symbol")
        )

        contracts = get_contracts(symbol)

        expiries = sorted({
            item.get("expiry")
            for item in contracts
            if item.get("expiry")
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


# ==========================================================
# MAX PAIN
# ==========================================================

def calculate_max_pain(chain):

    strikes = []

    for row in chain:

        call = (
            row.get("call_options") or {}
        ).get("market_data") or {}

        put = (
            row.get("put_options") or {}
        ).get("market_data") or {}

        strikes.append({
            "strike": number(row.get("strike_price")),
            "call_oi": number(call.get("oi")),
            "put_oi": number(put.get("oi"))
        })

    if not strikes:
        return None

    best_pain = None
    best_strike = None

    for settlement in [
        item["strike"] for item in strikes
    ]:

        pain = 0

        for item in strikes:

            pain += (
                max(0, settlement - item["strike"])
                * item["call_oi"]
            )

            pain += (
                max(0, item["strike"] - settlement)
                * item["put_oi"]
            )

        if best_pain is None or pain < best_pain:

            best_pain = pain
            best_strike = settlement

    return best_strike


# ==========================================================
# OPTION CHAIN
# ==========================================================

@app.route("/api/option-chain")
def option_chain():

    try:

        symbol = valid_symbol(
            request.args.get("symbol")
        )

        expiry = (
            request.args.get("expiry") or ""
        ).strip()

        if not expiry:
            raise ValueError("Select an expiry date.")

        datetime.strptime(
            expiry,
            "%Y-%m-%d"
        )

        result = upstox_v2(
            "/option/chain",
            {
                "instrument_key": INDEXES[symbol]["key"],
                "expiry_date": expiry
            }
        )

        raw_chain = result.get("data") or []

        if not raw_chain:

            return jsonify({
                "success": True,
                "symbol": symbol,
                "expiry": expiry,
                "chain": [],
                "message": "No option-chain data returned."
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

            call_change = (
                call_oi - number(call_data.get("prev_oi"))
            )

            put_change = (
                put_oi - number(put_data.get("prev_oi"))
            )

            total_call_oi += call_oi
            total_put_oi += put_oi

            total_call_change += call_change
            total_put_change += put_change

            chain.append({

                "strike": number(
                    row.get("strike_price")
                ),

                "call": {
                    "ltp": number(call_data.get("ltp")),
                    "oi": call_oi,
                    "oi_change": call_change,
                    "volume": number(call_data.get("volume")),
                    "iv": number(call_greeks.get("iv")),
                    "delta": number(call_greeks.get("delta")),
                    "bid": number(call_data.get("bid_price")),
                    "ask": number(call_data.get("ask_price"))
                },

                "put": {
                    "ltp": number(put_data.get("ltp")),
                    "oi": put_oi,
                    "oi_change": put_change,
                    "volume": number(put_data.get("volume")),
                    "iv": number(put_greeks.get("iv")),
                    "delta": number(put_greeks.get("delta")),
                    "bid": number(put_data.get("bid_price")),
                    "ask": number(put_data.get("ask_price"))
                }
            })

        chain.sort(
            key=lambda item: item["strike"]
        )

        pcr = (
            total_put_oi / total_call_oi
            if total_call_oi else 0
        )

        return jsonify({

            "success": True,
            "symbol": symbol,
            "name": INDEXES[symbol]["name"],
            "expiry": expiry,

            "spot": number(
                raw_chain[0].get("underlying_spot_price")
            ),

            "pcr": pcr,
            "max_pain": calculate_max_pain(raw_chain),

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


# ==========================================================
# CANDLE DATA
# ==========================================================

def parse_candle_time(value):

    try:

        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)

        return dt.astimezone(IST)

    except Exception:

        return None


def get_completed_candles(symbol):

    instrument_key = INDEXES[symbol]["key"]

    encoded_key = urllib.parse.quote(
        instrument_key,
        safe=""
    )

    endpoint = (
        f"/historical-candle/intraday/"
        f"{encoded_key}/minutes/{CANDLE_INTERVAL}"
    )

    result = upstox_v3(endpoint)

    raw = (
        result.get("data", {}).get("candles") or []
    )

    candles = sorted(
        raw,
        key=lambda candle: str(candle[0])
    )

    now = current_time()

    completed = []

    for candle in candles:

        candle_time = parse_candle_time(
            candle[0]
        )

        if candle_time is None:
            continue

        candle_end = candle_time + timedelta(
            minutes=CANDLE_INTERVAL
        )

        if candle_end <= now:
            completed.append(candle)

    if len(completed) < 15:

        raise Exception(
            "Not enough completed 5-minute candles."
        )

    return completed[-CANDLE_LOOKBACK:]


# ==========================================================
# SUPPORT AND RESISTANCE
# ==========================================================

def find_swing_levels(candles):

    if len(candles) < 12:

        raise Exception(
            "Not enough candles to calculate levels."
        )

    highs = [
        number(c[2]) for c in candles
    ]

    lows = [
        number(c[3]) for c in candles
    ]

    swing_highs = []
    swing_lows = []

    for i in range(2, len(candles) - 2):

        is_high = (
            highs[i] > highs[i - 1]
            and highs[i] > highs[i - 2]
            and highs[i] >= highs[i + 1]
            and highs[i] >= highs[i + 2]
        )

        is_low = (
            lows[i] < lows[i - 1]
            and lows[i] < lows[i - 2]
            and lows[i] <= lows[i + 1]
            and lows[i] <= lows[i + 2]
        )

        if is_high:
            swing_highs.append(highs[i])

        if is_low:
            swing_lows.append(lows[i])

    latest_close = number(candles[-1][4])

    resistance = sorted(set(
        round(x, 2)
        for x in swing_highs
        if x > latest_close
    ))

    support = sorted(set(
        (
            round(x, 2)
            for x in swing_lows
            if x < latest_close
        ),
        reverse=True
    ))

    if not resistance:

        recent_high = max(
            number(c[2])
            for c in candles[-20:-1]
        )

        if recent_high > latest_close:
            resistance = [round(recent_high, 2)]

    if not support:

        recent_low = min(
            number(c[3])
            for c in candles[-20:-1]
        )

        if recent_low < latest_close:
            support = [round(recent_low, 2)]

    return {
        "support": support[0] if support else None,
        "resistance": resistance[0] if resistance else None
    }


# ==========================================================
# TECHNICAL ANALYSIS
# ==========================================================

def analyze_index(symbol):

    candles = get_completed_candles(symbol)

    if len(candles) < 15:
        raise Exception("Insufficient candles.")

    previous = candles[-2]
    latest = candles[-1]

    # Calculate levels before the latest trigger candle.
    level_candles = candles[:-1]

    levels = find_swing_levels(
        level_candles
    )

    support = levels["support"]
    resistance = levels["resistance"]

    previous_close = number(previous[4])
    close = number(latest[4])

    volume = number(latest[5])

    recent_volumes = [
        number(c[5])
        for c in candles[-21:-1]
        if number(c[5]) > 0
    ]

    average_volume = (
        sum(recent_volumes) / len(recent_volumes)
        if recent_volumes else 0
    )

    volume_available = average_volume > 0

    volume_confirmed = (
        volume >= average_volume * VOLUME_MULTIPLIER
        if volume_available else None
    )

    signal = None
    trigger_level = None

    if resistance is not None:

        if (
            previous_close <= resistance
            and close > resistance * (
                1 + BREAKOUT_BUFFER_PERCENT
            )
            and volume_confirmed is not False
        ):

            signal = "BREAKOUT"
            trigger_level = resistance

    if support is not None:

        if (
            previous_close >= support
            and close < support * (
                1 - BREAKOUT_BUFFER_PERCENT
            )
            and volume_confirmed is not False
        ):

            signal = "BREAKDOWN"
            trigger_level = support

    underlying_sl = None
    target1 = None
    target2 = None

    risk_points = 0

    if signal == "BREAKOUT":

        underlying_sl = support

        if (
            underlying_sl is None
            or underlying_sl >= close
        ):

            underlying_sl = min(
                number(c[3])
                for c in level_candles[-10:]
            )

        risk_points = close - underlying_sl

        if risk_points > 0:

            target1 = close + risk_points * 2
            target2 = close + risk_points * 3

    elif signal == "BREAKDOWN":

        underlying_sl = resistance

        if (
            underlying_sl is None
            or underlying_sl <= close
        ):

            underlying_sl = max(
                number(c[2])
                for c in level_candles[-10:]
            )

        risk_points = underlying_sl - close

        if risk_points > 0:

            target1 = close - risk_points * 2
            target2 = close - risk_points * 3

    return {

        "symbol": symbol,
        "name": INDEXES[symbol]["name"],

        "price": close,

        "support": support,
        "resistance": resistance,

        "signal": signal,
        "trigger_level": trigger_level,

        "underlying_sl": underlying_sl,
        "underlying_target1": target1,
        "underlying_target2": target2,

        "risk_points": risk_points,

        "volume": volume,
        "average_volume": average_volume,
        "volume_confirmed": volume_confirmed,

        "candle_time": str(latest[0])
    }


# ==========================================================
# BREAKOUT / BREAKDOWN ALERTS
# ==========================================================

def generate_alert(symbol):

    now = time.time()

    cache_key = "alert_" + symbol

    cached = cache.get(cache_key)

    if (
        cached
        and now - cached["time"] < ALERT_CACHE_SECONDS
    ):

        return cached["data"]

    analysis = analyze_index(symbol)

    if analysis["signal"]:

        event_id = (
            f'{symbol}_'
            f'{analysis["signal"]}_'
            f'{analysis["trigger_level"]}_'
            f'{analysis["candle_time"]}'
        )

        if event_id not in alerted_events:

            event = {

                "id": event_id,
                "symbol": symbol,
                "name": analysis["name"],

                "type": analysis["signal"],

                "level": analysis["trigger_level"],
                "price": analysis["price"],

                "volume_confirmed": analysis["volume_confirmed"],

                "candle_time": analysis["candle_time"],

                "created_at": formatted_time(),
                "timestamp": now
            }

            alert_history.insert(0, event)

            alerted_events[event_id] = now

    cache[cache_key] = {
        "time": now,
        "data": analysis
    }

    return analysis


@app.route("/api/breakout-alerts")
def breakout_alerts():

    results = {}
    errors = {}

    for symbol in INDEXES:

        try:

            results[symbol] = generate_alert(symbol)

        except Exception as error:

            errors[symbol] = str(error)

    now = time.time()

    global alert_history

    alert_history = [
        event for event in alert_history
        if now - event["timestamp"] <= ALERT_HISTORY_SECONDS
    ]

    for event_id, timestamp in list(
        alerted_events.items()
    ):

        if now - timestamp > ALERT_HISTORY_SECONDS:
            del alerted_events[event_id]

    return jsonify({

        "success": True,

        "levels": results,
        "alerts": alert_history[:30],

        "errors": errors,

        "updated_at": formatted_time()
    })


# ==========================================================
# AUTOMATIC ATM / ITM SELECTION
# ==========================================================

def select_option_contract(
    symbol,
    expiry,
    signal,
    spot
):

    contracts = get_contracts(symbol)

    desired_type = (
        "CE" if signal == "BREAKOUT"
        else "PE"
    )

    matching = [

        contract for contract in contracts

        if contract.get("expiry") == expiry

        and str(
            contract.get("instrument_type", "")
        ).upper() == desired_type

        and contract.get("instrument_key")
    ]

    if not matching:

        raise Exception(
            f"No {desired_type} contracts found for "
            f"{symbol} on {expiry}."
        )

    strikes = sorted(set(
        number(contract.get("strike_price"))
        for contract in matching
    ))

    if not strikes:
        raise Exception("Strike prices unavailable.")

    atm = min(
        strikes,
        key=lambda strike: abs(strike - spot)
    )

    atm_index = strikes.index(atm)

    if desired_type == "CE":

        itm = strikes[
            max(0, atm_index - 1)
        ]

    else:

        itm = strikes[
            min(len(strikes) - 1, atm_index + 1)
        ]

    contract_map = {
        number(contract.get("strike_price")): contract
        for contract in matching
    }

    candidate_strikes = [atm]

    if itm != atm:
        candidate_strikes.append(itm)

    candidates = []

    for strike in candidate_strikes:

        contract = contract_map.get(strike)

        if not contract:
            continue

        candidates.append({
            "strike": strike,
            "choice": (
                "ATM" if strike == atm
                else "1-step ITM"
            ),
            "contract": contract
        })

    keys = [
        item["contract"]["instrument_key"]
        for item in candidates
    ]

    quotes = quote_for_keys(keys)

    evaluated = []

    for item in candidates:

        contract = item["contract"]

        quote = find_quote(
            quotes,
            contract["instrument_key"]
        )

        if not quote:
            continue

        premium = number(
            quote.get("last_price")
        )

        if premium <= 0:
            continue

        bid, ask = quote_bid_ask(quote)

        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        spread_percent = (
            (ask - bid) / premium * 100
        )

        volume = number(
            quote.get("volume")
        )

        oi = number(
            quote.get("oi")
        )

        if spread_percent > MAX_SPREAD_PERCENT:
            continue

        evaluated.append({

            "strike": item["strike"],
            "choice": item["choice"],

            "contract": contract,

            "premium": premium,

            "bid": bid,
            "ask": ask,

            "spread_percent": spread_percent,

            "volume": volume,
            "oi": oi
        })

    if not evaluated:

        raise Exception(
            "No suitable ATM/ITM contract passed the live "
            "bid/ask spread check. No trade plan generated."
        )

    # Prefer ATM when its spread is acceptable.
    # Otherwise use the available ITM contract.
    selected = next(
        (
            item for item in evaluated
            if item["choice"] == "ATM"
        ),
        None
    )

    if selected is None:

        selected = max(
            evaluated,
            key=lambda item: (
                item["volume"],
                item["oi"]
            )
        )

    lot_size = int(
        number(
            selected["contract"].get("lot_size")
        )
    )

    if lot_size <= 0:

        raise Exception(
            "Valid contract lot size not returned by Upstox."
        )

    selected["lot_size"] = lot_size
    selected["option_type"] = desired_type

    return selected


# ==========================================================
# CREATE TRADE PLAN
# ==========================================================

@app.route("/api/trade-plan", methods=["POST"])
def create_trade_plan():

    try:

        body = request.get_json(silent=True) or {}

        symbol = valid_symbol(
            body.get("symbol")
        )

        expiry = str(
            body.get("expiry") or ""
        ).strip()

        risk_budget = number(
            body.get("risk_budget")
        )

        if risk_budget <= 0:

            raise ValueError(
                "Enter a manual maximum risk amount greater than zero."
            )

        datetime.strptime(
            expiry,
            "%Y-%m-%d"
        )

        analysis = analyze_index(symbol)

        signal = analysis["signal"]

        if signal not in (
            "BREAKOUT",
            "BREAKDOWN"
        ):

            raise Exception(
                "No confirmed fresh breakout/breakdown. "
                "No trade plan generated."
            )

        if analysis["risk_points"] <= 0:

            raise Exception(
                "Underlying stop-loss could not be calculated."
            )

        option = select_option_contract(
            symbol,
            expiry,
            signal,
            analysis["price"]
        )

        premium_entry = option["premium"]

        # Initial premium stop estimate.
        premium_sl = round(
            premium_entry * (
                1 - PREMIUM_STOP_PERCENT
            ),
            2
        )

        premium_risk = (
            premium_entry - premium_sl
        )

        if premium_risk <= 0:

            raise Exception(
                "Unable to calculate option premium risk."
            )

        lot_size = option["lot_size"]

        risk_per_lot = (
            premium_risk * lot_size
        )

        lots = int(
            risk_budget // risk_per_lot
        )

        if lots < 1:

            raise Exception(
                "Your manual risk budget is below the estimated "
                f"risk for one lot (₹{risk_per_lot:.2f}). "
                "Increase your budget or skip this trade."
            )

        quantity = lots * lot_size

        # Minimum 1:2 reward-to-risk.
        target1 = round(
            premium_entry + premium_risk * MIN_RISK_REWARD,
            2
        )

        target2 = round(
            premium_entry + premium_risk * 3,
            2
        )

        estimated_loss = round(
            premium_risk * quantity,
            2
        )

        target1_profit = round(
            (target1 - premium_entry) * quantity,
            2
        )

        target2_profit = round(
            (target2 - premium_entry) * quantity,
            2
        )

        contract = option["contract"]

        plan_id = (
            f"{symbol}_"
            f"{int(time.time() * 1000)}"
        )

        plan = {

            "plan_id": plan_id,

            "symbol": symbol,
            "name": INDEXES[symbol]["name"],

            "signal": signal,

            "direction": (
                "BULLISH" if signal == "BREAKOUT"
                else "BEARISH"
            ),

            "underlying_entry": analysis["price"],
            "underlying_stop": analysis["underlying_sl"],

            "underlying_target1": analysis["underlying_target1"],
            "underlying_target2": analysis["underlying_target2"],

            "option_type": option["option_type"],

            "strike": option["strike"],
            "strike_choice": option["choice"],

            "instrument_key": contract["instrument_key"],

            "trading_symbol": contract.get("trading_symbol"),

            "expiry": expiry,

            "lot_size": lot_size,
            "lots": lots,
            "quantity": quantity,

            "premium_entry": premium_entry,
            "premium_stop": premium_sl,

            "premium_target1": target1,
            "premium_target2": target2,

            "risk_budget": risk_budget,

            "risk_per_lot": round(
                risk_per_lot,
                2
            ),

            "estimated_max_loss": estimated_loss,

            "estimated_target1_profit": target1_profit,
            "estimated_target2_profit": target2_profit,

            "risk_reward_target1": "1:2",
            "risk_reward_target2": "1:3",

            "spread_percent": round(
                option["spread_percent"],
                2
            ),

            "status": "ACTIVE",

            "target1_hit": False,
            "target2_hit": False,

            "created_at": formatted_time(),

            "exit_reason": None
        }

        active_plans[plan_id] = plan
        plan_events[plan_id] = []

        return jsonify({
            "success": True,
            "plan": plan
        })

    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 400


# ==========================================================
# TRADE MONITOR / EXIT ALERTS
# ==========================================================

@app.route("/api/trade-monitor")
def trade_monitor():

    plan_id = request.args.get("plan_id", "")

    plan = active_plans.get(plan_id)

    if not plan:

        return jsonify({
            "success": False,
            "error": "Trade plan not found."
        }), 404

    if plan["status"] != "ACTIVE":

        return jsonify({
            "success": True,
            "plan": plan,
            "events": plan_events.get(plan_id, [])
        })

    try:

        quotes = quote_for_keys([
            plan["instrument_key"]
        ])

        option_quote = find_quote(
            quotes,
            plan["instrument_key"]
        )

        if not option_quote:

            raise Exception(
                "Option premium quote unavailable."
            )

        premium = number(
            option_quote.get("last_price")
        )

        underlying_quotes = quote_for_keys([
            INDEXES[plan["symbol"]]["key"]
        ])

        underlying_quote = find_quote(
            underlying_quotes,
            INDEXES[plan["symbol"]]["key"]
        )

        underlying = number(
            underlying_quote.get("last_price")
        ) if underlying_quote else 0

        events = plan_events.setdefault(
            plan_id,
            []
        )

        def add_event(event_type, reason):

            if event_type in [
                item["type"] for item in events
            ]:
                return

            event = {
                "type": event_type,
                "reason": reason,
                "premium": premium,
                "underlying": underlying,
                "created_at": formatted_time()
            }

            events.append(event)

            plan["last_event"] = event
            plan["last_event_at"] = event["created_at"]

        # Stop-loss has priority.
        if premium > 0 and premium <= plan["premium_stop"]:

            add_event(
                "STOP_LOSS",
                "OPTION PREMIUM STOP-LOSS HIT"
            )

            plan["status"] = "EXIT ALERT"
            plan["exit_reason"] = "OPTION PREMIUM STOP-LOSS"

        elif underlying > 0:

            if (
                plan["signal"] == "BREAKOUT"
                and underlying <= plan["underlying_stop"]
            ):

                add_event(
                    "UNDERLYING_INVALIDATION",
                    "UNDERLYING SUPPORT INVALIDATED"
                )

                plan["status"] = "EXIT ALERT"
                plan["exit_reason"] = "UNDERLYING INVALIDATION"

            elif (
                plan["signal"] == "BREAKDOWN"
                and underlying >= plan["underlying_stop"]
            ):

                add_event(
                    "UNDERLYING_INVALIDATION",
                    "UNDERLYING RESISTANCE INVALIDATED"
                )

                plan["status"] = "EXIT ALERT"
                plan["exit_reason"] = "UNDERLYING INVALIDATION"

        if plan["status"] == "ACTIVE" and premium > 0:

            if (
                premium >= plan["premium_target2"]
                and not plan["target2_hit"]
            ):

                plan["target2_hit"] = True

                add_event(
                    "TARGET_2",
                    "TARGET 2 HIT - REVIEW EXIT"
                )

                plan["status"] = "EXIT ALERT"
                plan["exit_reason"] = "TARGET 2 HIT"

            elif (
                premium >= plan["premium_target1"]
                and not plan["target1_hit"]
            ):

                plan["target1_hit"] = True

                add_event(
                    "TARGET_1",
                    "TARGET 1 HIT - REVIEW PARTIAL EXIT"
                )

        plan["latest_premium"] = premium
        plan["latest_underlying"] = underlying

        return jsonify({

            "success": True,

            "plan": plan,

            "events": events,

            "exit_alert": (
                plan["status"] == "EXIT ALERT"
            )
        })

    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error),
            "plan": plan
        }), 400


# ==========================================================
# TRADE PLAN HISTORY
# ==========================================================

@app.route("/api/trade-plans")
def trade_plans():

    return jsonify({
        "success": True,
        "plans": list(
            active_plans.values()
        )[-20:][::-1]
    })


# ==========================================================
# UPSTOX CONNECTION TEST
# ==========================================================

@app.route("/upstox-test")
def upstox_test():

    try:

        result = upstox_v2(
            "/user/profile"
        )

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


# ==========================================================
# DASHBOARD HTML
# ==========================================================

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
    padding: 16px;
    background: #080d18;
    color: #e8eefc;
    font-family: Arial, sans-serif;
}

h1 {
    color: #42a5ff;
    margin-bottom: 5px;
}

.subtitle, .muted {
    color: #94a3b8;
}

.cards, .alert-grid, .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px;
}

.card, .panel, .level-card, .stat {
    background: #111a2b;
    border: 1px solid #24344e;
    border-radius: 11px;
    padding: 15px;
}

.panel {
    margin-top: 18px;
    overflow: hidden;
}

.price {
    font-size: 26px;
    font-weight: bold;
    margin: 10px 0;
}

.green { color: #22c98a; }
.red { color: #ff647c; }
.call { color: #ff8797; }
.put { color: #31d9a0; }

select, input, button {
    background: #1b2b45;
    border: 1px solid #365172;
    color: white;
    padding: 10px;
    border-radius: 7px;
    margin: 5px 5px 10px 0;
    font-size: 14px;
    max-width: 100%;
}

button {
    background: #0879df;
    border: 0;
    cursor: pointer;
}

button.secondary {
    background: #34445d;
}

.stats {
    margin: 14px 0;
}

.stat-label {
    font-size: 11px;
    color: #9eb3d1;
}

.stat-value {
    font-size: 18px;
    font-weight: bold;
    margin-top: 7px;
    overflow-wrap: anywhere;
}

.table-wrap {
    overflow-x: auto;
}

table {
    border-collapse: collapse;
    width: 100%;
    min-width: 850px;
    font-size: 12px;
}

th, td {
    padding: 9px 7px;
    border-bottom: 1px solid #29364c;
    text-align: right;
    white-space: nowrap;
}

th {
    background: #1b2b45;
}

th:nth-child(5), td:nth-child(5) {
    text-align: center;
    font-weight: bold;
}

.message {
    color: #ffc66d;
    padding: 9px 0;
    white-space: pre-wrap;
}

.alert-item {
    padding: 12px;
    margin: 8px 0;
    background: #19263b;
    border-left: 4px solid #42a5ff;
    border-radius: 7px;
}

.breakout { border-color: #22c98a; }
.breakdown { border-color: #ff647c; }

.level-row {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    margin-top: 9px;
}

.plan-box {
    background: #17243a;
    border: 1px solid #365172;
    border-radius: 9px;
    padding: 13px;
    margin-top: 12px;
}

.plan-box .level-row {
    border-bottom: 1px solid #293c58;
    padding-bottom: 7px;
}

.footer {
    margin-top: 20px;
    color: #788ba8;
    font-size: 12px;
    line-height: 1.6;
}

label {
    font-size: 13px;
    color: #b9c9df;
}

</style>

</head>

<body>

<h1>SUMIT AI TRADING TERMINAL</h1>

<div class="subtitle">
Live Market | Option Chain | Breakout / Breakdown |
Automatic Options Trade Planner
</div>


<h3>LIVE MARKET DATA</h3>

<div class="cards">

<div class="card">
<div class="muted">NIFTY 50</div>
<div class="price" id="nifty-price">--</div>
<div id="nifty-change">--</div>
<div class="muted" id="nifty-range"></div>
</div>

<div class="card">
<div class="muted">BANK NIFTY</div>
<div class="price" id="banknifty-price">--</div>
<div id="banknifty-change">--</div>
<div class="muted" id="banknifty-range"></div>
</div>

<div class="card">
<div class="muted">SENSEX</div>
<div class="price" id="sensex-price">--</div>
<div id="sensex-change">--</div>
<div class="muted" id="sensex-range"></div>
</div>

</div>


<!-- ALERTS -->

<div class="panel">

<h3>BREAKOUT / BREAKDOWN ALERTS</h3>

<button onclick="enableNotifications()">
Enable Browser Notifications
</button>

<button class="secondary" onclick="loadAlerts()">
Refresh Analysis
</button>

<div id="alert-status" class="message">
Loading candle analysis...
</div>

<div class="alert-grid" id="levels"></div>

<h4>Alert History</h4>

<div id="alert-history">
No alerts yet.
</div>

</div>


<!-- OPTION CHAIN -->

<div class="panel">

<h3>OPTION CHAIN</h3>

<label>Index</label>

<select id="symbol" onchange="loadExpiries()">

<option value="NIFTY">NIFTY 50</option>

<option value="BANKNIFTY">BANK NIFTY</option>

<option value="SENSEX">SENSEX</option>

</select>

<label>Expiry</label>

<select id="expiry">
<option value="">Select expiry</option>
</select>

<button onclick="loadOptionChain()">
Load Option Chain
</button>

<div id="chain-status" class="message">
Select index and expiry.
</div>

<div class="stats">

<div class="stat">
<div class="stat-label">SPOT</div>
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
<div class="stat-label">CALL OI</div>
<div class="stat-value" id="calloi">--</div>
</div>

<div class="stat">
<div class="stat-label">PUT OI</div>
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
<th colspan="4" class="call">CALL (CE)</th>
<th rowspan="2">STRIKE</th>
<th colspan="4" class="put">PUT (PE)</th>
</tr>

<tr>
<th>LTP</th>
<th>OI</th>
<th>OI CHG</th>
<th>VOLUME</th>
<th>VOLUME</th>
<th>OI CHG</th>
<th>OI</th>
<th>LTP</th>
</tr>

</thead>

<tbody id="chain-body">

<tr>
<td colspan="9">
Option chain will appear here.
</td>
</tr>

</tbody>

</table>

</div>

</div>


<!-- TRADE PLANNER -->

<div class="panel">

<h3>OPTIONS ENTRY / EXIT PLANNER</h3>

<p class="muted">
Risk setting: Manual per trade. Enter the maximum rupee
amount you are prepared to risk. The planner calculates
lots from the estimated option-premium stop.
</p>

<label>Index</label>

<select id="plan-symbol">

<option value="NIFTY">NIFTY 50</option>

<option value="BANKNIFTY">BANK NIFTY</option>

<option value="SENSEX">SENSEX</option>

</select>

<label>Expiry</label>

<select id="plan-expiry">
<option value="">Load expiry dates</option>
</select>

<label>Maximum risk per trade (₹)</label>

<input
    id="risk-budget"
    type="number"
    min="1"
    step="100"
    placeholder="Enter your risk amount"
>

<button onclick="loadPlanExpiries()">
Load Expiry Dates
</button>

<button onclick="createPlan()">
Generate Trade Plan
</button>

<div id="plan-status" class="message">
No trade plan generated.
</div>

<div id="plan-result"></div>

<h4>Active / Recent Plans</h4>

<div id="plan-history">
No plans yet.
</div>

</div>


<div class="footer">

Data provided by Upstox.

Option selection uses ATM first, then one-step ITM
if the ATM quote fails the spread check.

The option premium stop is initially estimated at
25% below the entry premium.

Target 1 is 1:2 and Target 2 is 1:3 against that
estimated premium risk.

Actual fills, slippage, gaps, and changing premiums
can result in a different loss or reward.

Exit alerts are notifications only. They do not
place orders or close positions.

Server-side plan history resets if Render restarts.

<br><br>

Last market refresh:
<span id="updated">--</span>

</div>


<script>

// ==================================================
// HELPERS
// ==================================================

function fmt(value) {

    return Number(value || 0).toLocaleString(
        "en-IN",
        {maximumFractionDigits: 2}
    );

}

function compact(value) {

    return Number(value || 0).toLocaleString(
        "en-IN",
        {maximumFractionDigits: 0}
    );

}

const seenAlerts = new Set(
    JSON.parse(
        localStorage.getItem("sumit_seen_alerts") || "[]"
    )
);

const notifiedPlanEvents = new Set();


// ==================================================
// NOTIFICATIONS
// ==================================================

async function enableNotifications() {

    if (!("Notification" in window)) {

        alert("Browser notifications are not supported.");

        return;
    }

    const permission =
        await Notification.requestPermission();

    if (permission === "granted") {

        new Notification(
            "SUMIT AI TRADING TERMINAL",
            {
                body: "Browser alerts enabled."
            }
        );

    } else {

        alert(
            "Please allow notifications in browser settings."
        );

    }

}


function notify(title, body, tag) {

    if (
        "Notification" in window
        && Notification.permission === "granted"
    ) {

        new Notification(title, {
            body: body,
            tag: tag
        });

    }

}


// ==================================================
// LIVE MARKET
// ==================================================

async function loadMarket() {

    try {

        const response = await fetch("/api/market");

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error);
        }

        const ids = {
            NIFTY: "nifty",
            BANKNIFTY: "banknifty",
            SENSEX: "sensex"
        };

        for (const symbol of Object.keys(ids)) {

            const id = ids[symbol];

            const item = data.market[symbol];

            if (!item || !item.available) {

                document.getElementById(
                    id + "-price"
                ).textContent = "--";

                document.getElementById(
                    id + "-change"
                ).textContent =
                    item?.message || "Unavailable";

                continue;

            }

            document.getElementById(
                id + "-price"
            ).textContent = fmt(item.ltp);

            const changeElement =
                document.getElementById(
                    id + "-change"
                );

            changeElement.textContent =
                (item.change >= 0 ? "+" : "")
                + fmt(item.change)
                + " ("
                + (item.percent >= 0 ? "+" : "")
                + fmt(item.percent)
                + "%)";

            changeElement.className =
                item.change >= 0 ? "green" : "red";

            document.getElementById(
                id + "-range"
            ).textContent =
                "O " + fmt(item.open)
                + " | H " + fmt(item.high)
                + " | L " + fmt(item.low);

        }

        document.getElementById(
            "updated"
        ).textContent = data.updated_at || "--";

    } catch (error) {

        document.getElementById(
            "updated"
        ).textContent = error.message;

    }

}


// ==================================================
// ALERT LEVELS
// ==================================================

function renderLevels(levels) {

    const container =
        document.getElementById("levels");

    container.innerHTML = "";

    for (const symbol of [
        "NIFTY",
        "BANKNIFTY",
        "SENSEX"
    ]) {

        const item = levels[symbol];

        const card = document.createElement("div");

        card.className = "level-card";

        if (!item) {

            card.textContent =
                symbol + ": Candle data unavailable";

            container.appendChild(card);

            continue;

        }

        const heading = document.createElement("b");

        heading.textContent = item.name;

        card.appendChild(heading);

        const rows = [

            "Last completed close: " + fmt(item.price),

            "Support: " +
            (item.support == null
                ? "--"
                : fmt(item.support)),

            "Resistance: " +
            (item.resistance == null
                ? "--"
                : fmt(item.resistance)),

            "Signal: " +
            (item.signal || "No confirmed signal"),

            "Volume confirmation: " +
            (
                item.volume_confirmed === null
                ? "Unavailable"
                : item.volume_confirmed
                    ? "Yes"
                    : "No"
            )

        ];

        rows.forEach(function(text) {

            const row = document.createElement("div");

            row.className = "level-row";

            row.textContent = text;

            card.appendChild(row);

        });

        container.appendChild(card);

    }

}


// ==================================================
// ALERT HISTORY
// ==================================================

function renderAlerts(alerts) {

    const container =
        document.getElementById("alert-history");

    container.innerHTML = "";

    if (!alerts || alerts.length === 0) {

        container.textContent = "No alerts yet.";

        return;

    }

    alerts.forEach(function(event) {

        const item = document.createElement("div");

        item.className =
            "alert-item "
            + (
                event.type === "BREAKOUT"
                ? "breakout"
                : "breakdown"
            );

        const title = document.createElement("b");

        title.textContent =
            (
                event.type === "BREAKOUT"
                ? "🚀 BREAKOUT — "
                : "🔻 BREAKDOWN — "
            ) + event.name;

        const detail = document.createElement("div");

        detail.textContent =
            "Level: " + fmt(event.level)
            + " | Candle close: " + fmt(event.price);

        const time = document.createElement("div");

        time.className = "muted";

        time.textContent = event.created_at;

        item.appendChild(title);
        item.appendChild(detail);
        item.appendChild(time);

        container.appendChild(item);

    });

}


// ==================================================
// LOAD ALERTS
// ==================================================

async function loadAlerts() {

    const status =
        document.getElementById("alert-status");

    status.textContent =
        "Analyzing completed 5-minute candles...";

    try {

        const response =
            await fetch("/api/breakout-alerts");

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error);
        }

        renderLevels(data.levels || {});

        renderAlerts(data.alerts || []);

        (data.alerts || []).forEach(function(event) {

            if (!seenAlerts.has(event.id)) {

                notify(
                    event.type === "BREAKOUT"
                        ? "Bullish Breakout"
                        : "Bearish Breakdown",

                    event.name
                    + " | Level "
                    + fmt(event.level),

                    "alert-" + event.id
                );

                seenAlerts.add(event.id);

            }

        });

        localStorage.setItem(
            "sumit_seen_alerts",
            JSON.stringify(
                Array.from(seenAlerts).slice(-150)
            )
        );

        const errors = data.errors || {};

        const failed = Object.keys(errors);

        status.textContent =
            failed.length
            ? "Some index data unavailable: "
                + failed.join(", ")
                + ". "
                + failed.map(
                    symbol => symbol + ": " + errors[symbol]
                ).join(" | ")
            : "Analysis updated: " + data.updated_at;

    } catch (error) {

        status.textContent =
            "Alert error: " + error.message;

    }

}


// ==================================================
// EXPIRIES
// ==================================================

async function loadExpiriesFor(symbol, selectId) {

    const select =
        document.getElementById(selectId);

    select.innerHTML =
        '<option value="">Loading...</option>';

    const response = await fetch(
        "/api/option-expiries?symbol="
        + encodeURIComponent(symbol)
    );

    const data = await response.json();

    if (!data.success) {
        throw new Error(data.error);
    }

    select.innerHTML =
        '<option value="">Select expiry</option>';

    (data.expiries || []).forEach(function(expiry) {

        const option =
            document.createElement("option");

        option.value = expiry;
        option.textContent = expiry;

        select.appendChild(option);

    });

    if (!data.expiries || data.expiries.length === 0) {

        throw new Error(
            "No expiries returned for " + symbol
        );

    }

}


async function loadExpiries() {

    try {

        await loadExpiriesFor(
            document.getElementById("symbol").value,
            "expiry"
        );

        document.getElementById(
            "chain-status"
        ).textContent =
            "Select expiry and load option chain.";

    } catch (error) {

        document.getElementById(
            "chain-status"
        ).textContent = error.message;

    }

}


async function loadPlanExpiries() {

    try {

        const symbol =
            document.getElementById("plan-symbol").value;

        await loadExpiriesFor(
            symbol,
            "plan-expiry"
        );

        document.getElementById(
            "plan-status"
        ).textContent =
            "Expiry dates loaded. Enter your manual risk amount.";

    } catch (error) {

        document.getElementById(
            "plan-status"
        ).textContent = error.message;

    }

}


// ==================================================
// OPTION CHAIN
// ==================================================

async function loadOptionChain() {

    const symbol =
        document.getElementById("symbol").value;

    const expiry =
        document.getElementById("expiry").value;

    const status =
        document.getElementById("chain-status");

    if (!expiry) {

        status.textContent = "Select an expiry.";

        return;

    }

    status.textContent = "Loading option chain...";

    try {

        const url =
            "/api/option-chain?symbol="
            + encodeURIComponent(symbol)
            + "&expiry="
            + encodeURIComponent(expiry);

        const response = await fetch(url);

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error);
        }

        const stats = {

            spot: data.spot,

            pcr: Number(data.pcr || 0).toFixed(2),

            maxpain: data.max_pain,

            calloi: data.total_call_oi,

            putoi: data.total_put_oi,

            callchange: data.call_oi_change,

            putchange: data.put_oi_change

        };

        Object.entries(stats).forEach(function([id, value]) {

            document.getElementById(id).textContent =
                value == null ? "--" : fmt(value);

        });

        const body =
            document.getElementById("chain-body");

        body.innerHTML = "";

        if (!data.chain || data.chain.length === 0) {

            body.innerHTML =
                '<tr><td colspan="9">No chain data.</td></tr>';

            return;

        }

        data.chain.forEach(function(row) {

            const tr = document.createElement("tr");

            const values = [

                row.call.ltp,
                row.call.oi,
                row.call.oi_change,
                row.call.volume,

                row.strike,

                row.put.volume,
                row.put.oi_change,
                row.put.oi,
                row.put.ltp

            ];

            values.forEach(function(value, index) {

                const td = document.createElement("td");

                td.textContent = fmt(value);

                if (index < 4) td.className = "call";

                if (index > 4) td.className = "put";

                tr.appendChild(td);

            });

            body.appendChild(tr);

        });

        status.textContent =
            symbol + " chain loaded for " + expiry;

    } catch (error) {

        status.textContent = error.message;

    }

}


// ==================================================
// TRADE PLAN DISPLAY
// ==================================================

function renderPlan(plan) {

    const container =
        document.getElementById("plan-result");

    container.innerHTML = "";

    const box = document.createElement("div");

    box.className = "plan-box";

    const rows = [

        ["Signal", plan.signal + " / " + plan.direction],

        [
            "Selected option",
            plan.trading_symbol
            || plan.option_type + " " + plan.strike
        ],

        [
            "Strike selection",
            plan.strike + " (" + plan.strike_choice + ")"
        ],

        [
            "Underlying entry",
            fmt(plan.underlying_entry)
        ],

        [
            "Underlying stop",
            fmt(plan.underlying_stop)
        ],

        [
            "Underlying targets",
            fmt(plan.underlying_target1)
            + " / "
            + fmt(plan.underlying_target2)
        ],

        [
            "Option premium entry",
            "₹" + fmt(plan.premium_entry)
        ],

        [
            "Option premium stop-loss",
            "₹" + fmt(plan.premium_stop)
        ],

        [
            "Option Target 1",
            "₹" + fmt(plan.premium_target1)
        ],

        [
            "Option Target 2",
            "₹" + fmt(plan.premium_target2)
        ],

        [
            "Expiry",
            plan.expiry
        ],

        [
            "Lots / Quantity",
            plan.lots
            + " lots / "
            + plan.quantity
            + " units"
        ],

        [
            "Lot size",
            plan.lot_size
        ],

        [
            "Manual risk budget",
            "₹" + fmt(plan.risk_budget)
        ],

        [
            "Estimated maximum loss",
            "₹" + fmt(plan.estimated_max_loss)
        ],

        [
            "Target 1 estimated profit",
            "₹" + fmt(plan.estimated_target1_profit)
        ],

        [
            "Target 2 estimated profit",
            "₹" + fmt(plan.estimated_target2_profit)
        ],

        [
            "Risk : Reward",
            "1:2 / 1:3"
        ],

        [
            "Bid-ask spread",
            plan.spread_percent + "%"
        ],

        [
            "Plan status",
            plan.status
        ]

    ];

    rows.forEach(function(row) {

        const line = document.createElement("div");

        line.className = "level-row";

        const label = document.createElement("span");

        label.textContent = row[0];

        const value = document.createElement("b");

        value.textContent = row[1];

        line.appendChild(label);
        line.appendChild(value);

        box.appendChild(line);

    });

    const note = document.createElement("p");

    note.className = "muted";

    note.textContent =
        "Read-only trade plan. Option stop is initially estimated "
        + "at 25% below entry premium. Actual execution, slippage, "
        + "gaps and premium movement can change the result. "
        + "No order has been placed.";

    box.appendChild(note);

    container.appendChild(box);

}


// ==================================================
// CREATE PLAN
// ==================================================

async function createPlan() {

    const status =
        document.getElementById("plan-status");

    const symbol =
        document.getElementById("plan-symbol").value;

    const expiry =
        document.getElementById("plan-expiry").value;

    const risk =
        Number(
            document.getElementById("risk-budget").value
        );

    if (!risk || risk <= 0) {

        status.textContent =
            "Enter your maximum risk amount in rupees.";

        return;

    }

    if (!expiry) {

        status.textContent =
            "Load and select an expiry first.";

        return;

    }

    status.textContent =
        "Checking signal, contracts, premium, liquidity and risk...";

    try {

        const response = await fetch(
            "/api/trade-plan",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({

                    symbol: symbol,

                    expiry: expiry,

                    risk_budget: risk

                })
            }
        );

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error);
        }

        renderPlan(data.plan);

        status.textContent =
            "Trade plan generated. No order has been placed.";

        await loadPlans();

    } catch (error) {

        status.textContent =
            "No trade plan: " + error.message;

        document.getElementById(
            "plan-result"
        ).innerHTML = "";

    }

}


// ==================================================
// PLAN HISTORY
// ==================================================

async function loadPlans() {

    try {

        const response =
            await fetch("/api/trade-plans");

        const data = await response.json();

        const container =
            document.getElementById("plan-history");

        container.innerHTML = "";

        if (!data.plans || data.plans.length === 0) {

            container.textContent = "No plans yet.";

            return;

        }

        data.plans.forEach(function(plan) {

            const item = document.createElement("div");

            item.className = "alert-item";

            item.textContent =
                plan.name
                + " | "
                + plan.signal
                + " | "
                + plan.option_type
                + " "
                + plan.strike
                + " | "
                + plan.status;

            container.appendChild(item);

        });

    } catch (error) {

        console.error(error);

    }

}


// ==================================================
// MONITOR ACTIVE PLANS
// ==================================================

async function monitorPlans() {

    try {

        const response =
            await fetch("/api/trade-plans");

        const data = await response.json();

        for (const plan of (data.plans || [])) {

            if (plan.status !== "ACTIVE") {
                continue;
            }

            const monitorResponse = await fetch(
                "/api/trade-monitor?plan_id="
                + encodeURIComponent(plan.plan_id)
            );

            const monitor = await monitorResponse.json();

            if (!monitor.success) {
                continue;
            }

            const events = monitor.events || [];

            for (const event of events) {

                const eventKey =
                    plan.plan_id + "_" + event.type;

                if (notifiedPlanEvents.has(eventKey)) {
                    continue;
                }

                notifiedPlanEvents.add(eventKey);

                notify(
                    "OPTIONS " + event.type,
                    plan.name
                    + " | "
                    + event.reason
                    + " | Premium ₹"
                    + fmt(event.premium),

                    "plan-" + eventKey
                );

                if (
                    event.type === "STOP_LOSS"
                    || event.type === "UNDERLYING_INVALIDATION"
                    || event.type === "TARGET_2"
                ) {

                    document.getElementById(
                        "plan-status"
                    ).textContent =
                        "Exit alert: "
                        + event.reason
                        + ". Review and manage your position manually.";

                    renderPlan(monitor.plan);

                }

                if (event.type === "TARGET_1") {

                    document.getElementById(
                        "plan-status"
                    ).textContent =
                        "Target 1 reached. Review partial-exit decision manually.";

                    renderPlan(monitor.plan);

                }

            }

        }

        await loadPlans();

    } catch (error) {

        console.error(error);

    }

}


// ==================================================
// START DASHBOARD
// ==================================================

loadMarket();

loadExpiries();

loadAlerts();

loadPlans();

setInterval(loadMarket, 15000);

setInterval(loadAlerts, 30000);

setInterval(monitorPlans, 15000);

</script>

</body>
</html>
"""


# ==========================================================
# HOME
# ==========================================================

@app.route("/")
def home():

    return render_template_string(HTML)


# ==========================================================
# RUN
# ==========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
