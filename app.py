
from flask import Flask, request

app = Flask(__name__)


@app.route("/")
def home():
    return "SUMIT AI TRADING TERMINAL is running!"


@app.route("/callback")
def callback():
    authorization_code = request.args.get("code")

    if authorization_code:
        return """
        <h2>Upstox Authorization Received</h2>
        <p>Callback is working successfully.</p>
        <p>You can close this page.</p>
        """

    return """
    <h2>SUMIT AI TRADING TERMINAL</h2>
    <p>Waiting for Upstox authorization.</p>
    """
