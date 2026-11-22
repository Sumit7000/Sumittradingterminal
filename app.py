
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
        return """
        <h2>Access Token Missing</h2>
        <p>Please add UPSTOX_ACCESS_TOKEN in Render Environment.</p>
        """, 400

    url = "https://api.upstox.com/v2/user/profile"

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}"
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("status") == "success":
            return """
            <h2>Upstox Connected Successfully!</h2>
            <p>Your API connection is working.</p>
            <p>SUMIT AI TRADING TERMINAL is ready for the next step.</p>
            """

        return "<h2>Upstox returned an unexpected response.</h2>", 502

    except urllib.error.HTTPError as error:
        return f"<h2>Upstox connection failed. HTTP status: {error.code}</h2>", 502

    except Exception:
        return """
        <h2>Connection could not be completed.</h2>
        <p>Please check your Render configuration and try again.</p>
        """, 500


if __name__ == "__main__":
    app.run()
