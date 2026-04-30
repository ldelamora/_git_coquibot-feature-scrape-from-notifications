# app.py — Flask web server for coquibot.
# Serves a simple UI that lets the user trigger a SUMAC login + PDF scraping
# session by clicking a button in the browser.

from flask import Flask, render_template, jsonify
import sumac_login

app = Flask(__name__)


@app.route("/")
def index():
    # Render the main page (templates/index.html) which contains the trigger button.
    return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
    # Called when the user clicks the button in the UI.
    # Delegates to sumac_login.run(), which opens a real Chromium browser,
    # logs into SUMAC, and downloads PDFs for all cases.
    # Returns a JSON response so the frontend can display the outcome.
    try:
        result = sumac_login.run()
        return jsonify({"status": "ok", "message": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    # debug=True enables auto-reload on code changes during development.
    app.run(debug=True)
