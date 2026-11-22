
from flask import Flask
import os
import json
import html
import urllib.request
import urllib.error

app = Flask(__name__)


@app.route("/")
def home():
    return """
    <html>
    <head>
        <title>SUMIT AI TRADING TERMINAL</title>
    </head>
    <body>
        <h1>SUMIT AI TRADING TERMINAL</h1>
        <h3>Application is running successfully!</h3>
        <a href="/upstox-test">Test Upstox Connection</a>
    </body>
    </html>
    """


@app.route("/callback")
def callback():
    return "Waiting for Upstox authorization."


@app.route("/upstox-test")
def upstox_test():

    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        return """
        <h1>Configuration Error</h1>
        <p>UPSTOX_ACCESS_TOKEN is missing in Render.</p>
        """, 500

    url = "https://api.upstox.com/v2/user/profile"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "SUMIT-AI-TRADING-TERMINAL/1.0"
        },
        method="GET"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw_body = response.read().decode(
                "utf-8", errors="replace"
            )

            try:
                data = json.loads(raw_body)
            except json.JSONDecodeError:
                data = {"response": raw_body}

            safe_data = json.dumps(
                data, indent=2
            ).replace(token, "[REDACTED]")

            return """
            <html>
            <body>
                <h1>Upstox Connected Successfully!</h1>
                <p>HTTP Status: {}</p>
                <h3>Profile Response</h3>
                <pre>{}</pre>
            </body>
            </html>
            """.format(
                response.status,
                html.escape(safe_data[:4000])
            )

    except urllib.error.HTTPError as error:

        raw_body = error.read().decode(
            "utf-8", errors="replace"
        )

        safe_body = raw_body.replace(
            token, "[REDACTED]"
        )

        try:
            error_data = json.loads(safe_body)
        except json.JSONDecodeError:
            error_data = {}

        if (
            error.code == 403
            and error_data.get("error_code") == 1010
        ):

            return """
            <html>
            <body>
                <h1>Cloudflare Error 1010</h1>
                <h3>Access Denied by Upstox Security</h3>
                <p>Cloudflare blocked this server request.</p>
                <p>User-Agent was not accepted.</p>
                <p>Adding a User-Agent does not guarantee access.</p>
                <p>Contact Upstox support and request investigation.</p>
            </body>
            </html>
            """, 403

        return """
        <html>
        <body>
            <h1>Upstox API Error</h1>
            <p>HTTP Status: {}</p>
            <h3>Diagnostic Response</h3>
            <pre>{}</pre>
        </body>
        </html>
        """.format(
            error.code,
            html.escape(safe_body[:4000])
        ), error.code

    except Exception as error:

        return """
        <h1>Connection Failed</h1>
        <p>{}</p>
        """.format(
            html.escape(str(error))
        ), 500


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
