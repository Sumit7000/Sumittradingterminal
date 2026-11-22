
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
    <h1>SUMIT AI TRADING TERMINAL</h1>
    <p>Application is running successfully.</p>
    <a href="/upstox-test">Test Upstox Connection</a>
    """

@app.route("/callback")
def callback():
    return "Waiting for Upstox authorization."

@app.route("/upstox-test")
def upstox_test():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        return "<h2>ERROR: UPSTOX_ACCESS_TOKEN is missing in Render.</h2>", 500

    url = "https://api.upstox.com/v2/user/profile"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

            return """
            <h1>Upstox Connected Successfully!</h1>
            <p>Your API connection is working.</p>
            <pre>{}</pre>
            """.format(html.escape(json.dumps(data, indent=2)))

    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            errors = body.get("errors") or []

            if errors:
                detail = errors[0]
                code = detail.get("errorCode") or detail.get("error_code") or "Not provided"
                message = detail.get("message") or "No message provided"

                return """
                <h1>Upstox API Error</h1>
                <p>HTTP Status: {}</p>
                <p>Error Code: {}</p>
                <p>Message: {}</p>
                """.format(
                    error.code,
                    html.escape(str(code)),
                    html.escape(str(message))
                ), error.code

            return "<h1>Upstox API Error</h1><p>HTTP Status: {}</p><p>No detailed error returned.</p>".format(error.code), error.code

        except Exception:
            return "<h1>Upstox API Error</h1><p>HTTP Status: {}</p><p>Could not read error details.</p>".format(error.code), error.code

    except Exception as error:
        return "<h1>Connection Failed</h1><p>{}</p>".format(
            html.escape(str(error))
        ), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
