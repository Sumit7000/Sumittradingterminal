
import os
import json
import urllib.request
import urllib.error

from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return """
    <h1>SUMIT AI TRADING TERMINAL</h1>
    <p>Application is running successfully.</p>
    <a href="/upstox-test">Test Upstox Connection</a>
    """


@app.route("/upstox-test")
def upstox_test():
    access_token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not access_token:
        return "ERROR: UPSTOX_ACCESS_TOKEN is missing in Render.", 400

    url = "https://api.upstox.com/v2/user/profile"

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + access_token
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("status") == "success":
            return """
            <h2>Upstox Connected Successfully!</h2>
            <p>Your API connection is working.</p>
            """

        return "Upstox returned an unexpected response.", 502

    except urllib.error.HTTPError as error:
        if error.code == 401:
            return "ERROR 401: Access token invalid or expired. Generate a fresh token.", 401
        elif error.code == 403:
            return "ERROR 403: Access is forbidden. Check API permissions.", 403
        else:
            return f"Upstox API returned HTTP error: {error.code}", 502

    except Exception:
        return "Connection failed. Please check deployment and network settings.", 500


if __name__ == "__main__":
    app.run()
