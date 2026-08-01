import sys
import json
import argparse
from loguru import logger
from groq import Groq

import config
from agents import fetch_erp_data, fetch_portal_data, reconcile, generate_report

# ── Logging ───────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout,
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
           level="INFO", colorize=True)
logger.add("reports/run.log", rotation="1 week", level="DEBUG")

# ── Intent classification ─────────────────────────────────────
SYSTEM_PROMPT = """
You are an intent classifier for a tax reconciliation bot.
Given a user message, return ONLY valid JSON with these fields:

{
  "intent": one of ["fetch_erp", "fetch_tax", "reconcile", "report", "unknown"],
  "period": "YYYY-MM" or null,
  "transaction_code": SAP transaction code string or null,
  "explanation": one sentence describing what you understood
}

Intent definitions:
- fetch_erp  : user wants to download/fetch data from SAP / ERP
- fetch_tax  : user wants to download/check income tax portal data (26AS, AIS, TDS)
- reconcile  : user wants to compare ERP vs tax portal, find differences
- report     : user wants to generate or save the final HTML/PDF report
- unknown    : cannot determine intent

Period: extract month/year from the message. "March 2025" → "2025-03". Null if not mentioned.
Return ONLY the JSON object. No markdown. No extra text.
"""

client = None

def init_groq():
    global client
    client = Groq(api_key=config.GROQ_API_KEY)


def classify_intent(user_message: str) -> dict:
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0,
        max_tokens=200,
    )
    return json.loads(response.choices[0].message.content.strip())


def explain_result(action: str, result: str) -> str:
    prompt = (f"You are a tax assistant. Action taken: {action}. Result: {result}. "
              "Write 2-3 plain English sentences that are specific and actionable.")
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=200,
    )
    return response.choices[0].message.content.strip()


def handle_intent(intent_data: dict) -> str:
    intent   = intent_data.get("intent")
    period   = intent_data.get("period") or config.REPORT_PERIOD
    txn      = intent_data.get("transaction_code") or "FBL3N"
    use_mock = not config.SAP_HOST or config.SAP_HOST in ("your_sap_host_or_ip", "x")

    if use_mock:
        data_note = f" _(using demo data — add SAP/portal credentials to .env for live data)_"
    else:
        data_note = ""

    if intent == "fetch_erp":
        try:
            df = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
            result = explain_result(
                f"Fetched ERP data ({'demo' if use_mock else 'live SAP'})",
                f"{len(df)} records for {period}, transaction {txn}"
            )
            return result + (f"\n\n💡 {data_note.strip()}" if use_mock else "")
        except Exception as e:
            return f"ERP fetch failed: {e}"

    elif intent == "fetch_tax":
        use_mock_portal = use_mock or not config.IT_USER_PAN or config.IT_USER_PAN in ("ABCDE1234F", "x")
        try:
            df = fetch_portal_data(period=period, mock=use_mock_portal)
            result = explain_result(
                f"Fetched 26AS/AIS data ({'demo' if use_mock_portal else 'IT portal'})",
                f"{len(df)} records for {period}"
            )
            return result + (f"\n\n💡 {data_note.strip()}" if use_mock_portal else "")
        except Exception as e:
            return f"Portal fetch failed: {e}"

    elif intent == "reconcile":
        try:
            erp_df    = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
            portal_df = fetch_portal_data(period=period, mock=use_mock)
            mis_df    = reconcile(erp_df, portal_df)
            result = explain_result(
                f"Reconciled ERP vs Tax Portal ({'demo data' if use_mock else 'live'})",
                f"{len(mis_df)} mismatches found for {period}"
            )
            return result + (f"\n\n💡 {data_note.strip()}" if use_mock else "")
        except Exception as e:
            return f"Reconciliation failed: {e}"

    elif intent == "report":
        try:
            erp_df    = fetch_erp_data(period=period, transaction_code=txn, mock=use_mock)
            portal_df = fetch_portal_data(period=period, mock=use_mock)
            mis_df    = reconcile(erp_df, portal_df)
            path      = generate_report(mis_df, period)
            return (f"Report saved to: {path}\n"
                    f"Open the HTML file in your browser to view it."
                    + (f"\n\n💡 {data_note.strip()}" if use_mock else ""))
        except Exception as e:
            return f"Report generation failed: {e}"

    return ("I didn't understand that. Try:\n"
            "  • \"Fetch ERP data for March 2025\"\n"
            "  • \"Run FBL3N for March 2025\"\n"
            "  • \"Download 26AS for March 2025\"\n"
            "  • \"Why is my tax different for March 2025?\"\n"
            "  • \"Generate the report for March 2025\"")


# ── Test modes (bypass chat loop) ────────────────────────────

def test_report(period: str):
    """Run full pipeline: mock data → recon → LLM → HTML/PDF."""
    logger.info(f"=== TEST MODE: Full Report | {period} ===")
    try:
        from agents.recon_engine import reconcile as _reconcile
        erp_df    = fetch_erp_data(period=period, mock=True)
        portal_df = fetch_portal_data(period=period, mock=True)

        # Full merged data to split into matched / mismatched
        import pandas as pd
        from agents.recon_engine import (
            _prepare_erp, _prepare_portal, _aggregate, MISSING_IN_PORTAL,
            MISSING_IN_BOOKS, AMOUNT_MISMATCH
        )
        erp    = _prepare_erp(erp_df)
        portal = _prepare_portal(portal_df)
        name_map = (erp[["vendor_pan","vendor_name"]].drop_duplicates("vendor_pan")
                    .set_index("vendor_pan")["vendor_name"].to_dict())
        portal_name_map = (portal[["deductee_pan","deductor_name"]].drop_duplicates("deductee_pan")
                           .set_index("deductee_pan")["deductor_name"].to_dict())
        erp_agg    = _aggregate(erp, "vendor_pan").rename(
                         columns={"vendor_pan":"pan","tds_amount":"erp_amount"})
        portal_agg = _aggregate(portal, "deductee_pan").rename(
                         columns={"deductee_pan":"pan","tds_amount":"portal_amount"})
        merged = pd.merge(erp_agg, portal_agg, on=["pan","tds_section"], how="outer")
        merged["erp_amount"]    = merged["erp_amount"].fillna(0)
        merged["portal_amount"] = merged["portal_amount"].fillna(0)
        merged["difference"]    = merged["erp_amount"] - merged["portal_amount"]
        merged["party_name"]    = merged["pan"].map(
            lambda p: name_map.get(p) or portal_name_map.get(p) or "Unknown")

        def classify(row):
            if row["erp_amount"] == 0:    return MISSING_IN_BOOKS
            if row["portal_amount"] == 0: return MISSING_IN_PORTAL
            if abs(row["difference"]) > 1: return AMOUNT_MISMATCH
            return "Matched"
        merged["mismatch_type"] = merged.apply(classify, axis=1)

        mis_df = merged[merged["mismatch_type"] != "Matched"].copy()
        matched = merged[merged["mismatch_type"] == "Matched"].to_dict("records")

        path = generate_report(mis_df, period, matched_rows=matched)
        print(f"\n✓ Report saved to: {path}\n")
        print("  Open the HTML file in your browser to preview.")
        print("  The PDF is in the same folder.\n")
    except Exception as e:
        logger.error(str(e))
        import traceback; traceback.print_exc()


def test_llm(period: str):
    """Run mock recon then enrich with Groq LLM explanations."""
    logger.info(f"=== TEST MODE: LLM Report Agent | {period} ===")
    try:
        from agents.report_agent import enrich_with_llm
        erp_df    = fetch_erp_data(period=period, mock=True)
        portal_df = fetch_portal_data(period=period, mock=True)
        mis_df    = reconcile(erp_df, portal_df)

        if mis_df.empty:
            print("\n✓ No mismatches to explain.\n")
            return

        enriched, summary = enrich_with_llm(mis_df, period, matched_count=5)

        print("\n" + "═"*70)
        print("  EXECUTIVE SUMMARY")
        print("═"*70)
        print(summary)
        print()

        print("═"*70)
        print("  MISMATCH DETAILS WITH LLM EXPLANATIONS")
        print("═"*70)
        for _, row in enriched.iterrows():
            urgency_icon = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(row["urgency"], "⚪")
            print(f"\n{urgency_icon}  [{row['urgency']}] {row['party_name']} ({row['pan']})")
            print(f"   Section : {row['tds_section']}  |  Type: {row['mismatch_type']}")
            print(f"   ERP: ₹{row['erp_amount']:,.0f}  |  Portal: ₹{row['portal_amount']:,.0f}"
                  f"  |  Gap: ₹{row['difference']:,.0f}")
            print(f"   What   : {row['plain_english']}")
            print(f"   Why    : {row['root_cause']}")
            print(f"   Action : {row['llm_action']}")
        print()
    except Exception as e:
        logger.error(str(e))


def test_recon(period: str):
    """Run both mock agents then reconcile — no Groq needed."""
    logger.info(f"=== TEST MODE: Reconciliation Engine | {period} ===")
    try:
        erp_df    = fetch_erp_data(period=period, mock=True)
        portal_df = fetch_portal_data(period=period, mock=True)
        mis_df    = reconcile(erp_df, portal_df)

        if mis_df.empty:
            print("\n✓ No mismatches found — all records matched!\n")
            return

        print(f"\n✓ Reconciliation complete — {len(mis_df)} mismatches found\n")
        print("─" * 90)
        for _, row in mis_df.iterrows():
            print(f"  PAN          : {row['pan']}")
            print(f"  Party        : {row['party_name']}")
            print(f"  Section      : {row['tds_section']}")
            print(f"  ERP Amount   : ₹{row['erp_amount']:>12,.0f}")
            print(f"  Portal Amount: ₹{row['portal_amount']:>12,.0f}")
            print(f"  Difference   : ₹{row['difference']:>12,.0f}")
            print(f"  Type         : {row['mismatch_type']}")
            print(f"  Action       : {row['recommended_action']}")
            print("─" * 90)
    except Exception as e:
        logger.error(str(e))


def test_portal(period: str, mock: bool = False):
    """Direct test of the portal agent — no Groq needed."""
    mode = "MOCK" if mock else "LIVE BROWSER"
    logger.info(f"=== TEST MODE: Portal Agent | {period} | {mode} ===")
    try:
        df = fetch_portal_data(period=period, mock=mock)
        print(f"\n✓ Success — {len(df)} records fetched\n")
        print(df.to_string(index=False))
    except Exception as e:
        logger.error(str(e))


def test_erp(period: str, tcode: str, mock: bool = False):
    """Direct test of the ERP agent — no Groq needed."""
    mode = "MOCK" if mock else "LIVE SAP"
    logger.info(f"=== TEST MODE: ERP Agent | {tcode} | {period} | {mode} ===")
    try:
        df = fetch_erp_data(period=period, transaction_code=tcode, mock=mock)
        print(f"\n✓ Success — {len(df)} records fetched\n")
        print(df.to_string(index=False))
    except Exception as e:
        logger.error(str(e))


# ── Chat loop ─────────────────────────────────────────────────

def chat_loop():
    print("\n" + "═"*55)
    print("  Tax Reconciliation Bot — Ready")
    print("  Type your request. Type 'quit' to exit.")
    print("═"*55 + "\n")

    config.validate_config()
    init_groq()

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBot: Goodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "bye"):
            print("Bot: Goodbye!")
            break

        try:
            intent_data = classify_intent(user_input)
            logger.debug(f"Intent: {intent_data}")
            reply = handle_intent(intent_data)
        except json.JSONDecodeError:
            reply = "Sorry, I had trouble understanding that. Please rephrase."
        except Exception as e:
            logger.error(str(e))
            reply = f"Something went wrong: {e}"

        print(f"\nBot: {reply}\n")


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tax Reconciliation Bot")
    parser.add_argument("--test-report", metavar="PERIOD",
                        help="Run full pipeline and save report, e.g. --test-report 2025-03")
    parser.add_argument("--test-llm",   metavar="PERIOD",
                        help="Test LLM explanations, e.g. --test-llm 2025-03")
    parser.add_argument("--test-recon", metavar="PERIOD",
                        help="Test reconciliation engine, e.g. --test-recon 2025-03")
    parser.add_argument("--test-portal", metavar="PERIOD",
                        help="Test portal agent directly, e.g. --test-portal 2025-03")
    parser.add_argument("--test-erp",   metavar="PERIOD",
                        help="Test ERP agent directly, e.g. --test-erp 2025-03")
    parser.add_argument("--tcode",      default="FBL3N",
                        help="SAP transaction code (default: FBL3N)")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock data instead of real SAP (for testing)")
    args = parser.parse_args()

    if args.test_report:
        test_report(period=args.test_report)
    elif args.test_llm:
        test_llm(period=args.test_llm)
    elif args.test_recon:
        test_recon(period=args.test_recon)
    elif args.test_portal:
        test_portal(period=args.test_portal, mock=args.mock)
    elif args.test_erp:
        test_erp(period=args.test_erp, tcode=args.tcode, mock=args.mock)
    else:
        chat_loop()