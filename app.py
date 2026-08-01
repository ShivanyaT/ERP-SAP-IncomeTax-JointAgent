import os
import sys
import json
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from loguru import logger

import config
from agents import fetch_erp_data, fetch_portal_data, reconcile, generate_report
from main import classify_intent, explain_result, init_groq

app = Flask(__name__)
UI_DIR = str(Path(__file__).parent / "ui")
CORS(app)

logger.remove()
logger.add(sys.stdout,
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
           level="INFO", colorize=True)

# ── Intent handler (reused from main.py logic) ────────────────

def handle_intent_web(intent_data: dict) -> dict:
    """
    Same logic as handle_intent() in main.py but returns a dict
    with { text, report_path } instead of a plain string.
    """
    intent   = intent_data.get("intent")
    period   = intent_data.get("period") or config.REPORT_PERIOD
    txn      = intent_data.get("transaction_code") or "FBL3N"
    use_mock = not config.SAP_HOST or config.SAP_HOST in ("your_sap_host_or_ip", "x", "")

    demo_note = "\n\n> 💡 Using demo data. Add real credentials to `.env` for live data." if use_mock else ""

    if intent == "fetch_erp":
        df   = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
        text = explain_result(f"Fetched ERP data ({'demo' if use_mock else 'live SAP'})",
                              f"{len(df)} records for {period}, transaction {txn}")
        return {"text": text + demo_note}

    elif intent == "fetch_tax":
        use_mock_portal = use_mock or config.IT_USER_PAN in ("ABCDE1234F", "x", "")
        df   = fetch_portal_data(period=period, mock=use_mock_portal)
        text = explain_result(f"Fetched portal data ({'demo' if use_mock_portal else 'live'})",
                              f"{len(df)} records for {period}")
        return {"text": text + demo_note}

    elif intent == "reconcile":
        erp_df    = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
        portal_df = fetch_portal_data(period=period, mock=use_mock)
        mis_df    = reconcile(erp_df, portal_df)
        if mis_df.empty:
            text = f"✅ All TDS entries for {period} are fully reconciled — no mismatches found."
        else:
            lines = [f"Found **{len(mis_df)} mismatch(es)** for {period}:\n"]
            for _, r in mis_df.iterrows():
                icon = {"Amount Mismatch": "🟡", "Missing in 26AS/AIS": "🔴",
                        "Missing in ERP Books": "🔵"}.get(r["mismatch_type"], "⚪")
                lines.append(f"{icon} **{r['party_name']}** ({r['tds_section']}) — "
                              f"{r['mismatch_type']} | "
                              f"ERP ₹{r['erp_amount']:,.0f} vs Portal ₹{r['portal_amount']:,.0f}")
                lines.append(f"   → {r['recommended_action']}\n")
            text = "\n".join(lines)
        return {"text": text + demo_note}

    elif intent == "report":
        erp_df    = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
        portal_df = fetch_portal_data(period=period, mock=use_mock)
        mis_df    = reconcile(erp_df, portal_df)
        path      = generate_report(mis_df, period)
        filename  = Path(path).name
        text = (f"✅ Report generated for **{period}**.\n\n"
                f"📄 [{filename}](/reports/{filename})")
        return {"text": text + demo_note, "report_path": f"/reports/{filename}"}

    return {"text": ("I didn't understand that. Try:\n"
                     "- *Fetch ERP data for March 2025*\n"
                     "- *Why is my tax different for March 2025?*\n"
                     "- *Generate the report for March 2025*")}


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data    = request.get_json()
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"reply": "Please type a message."}), 400

    try:
        intent_data = classify_intent(message)
        logger.info(f"Intent: {intent_data.get('intent')} | Period: {intent_data.get('period')}")
        result = handle_intent_web(intent_data)
        return jsonify({"reply": result["text"],
                        "report_path": result.get("report_path")})
    except json.JSONDecodeError:
        return jsonify({"reply": "Sorry, I couldn't parse that. Please rephrase."})
    except Exception as e:
        logger.error(str(e))
        return jsonify({"reply": f"Error: {e}"}), 500


@app.route("/reports/<filename>")
def serve_report(filename):
    return send_from_directory(config.REPORTS_DIR, filename)


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    config.validate_config()
    init_groq()
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    logger.info("Starting Tax Reconciliation Bot UI on http://localhost:5000")
    import webbrowser
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False)