import os
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger
from groq import Groq

import config


# ── LLM explanation ───────────────────────────────────────────

EXPLAIN_SYSTEM = """
You are a senior chartered accountant explaining TDS reconciliation issues 
to a finance executive. Be concise, specific, and actionable.
Always respond in valid JSON only — no markdown, no extra text.
"""

EXPLAIN_USER = """
Analyse this TDS mismatch and respond with ONLY a JSON object:

{{
  "plain_english": "1-2 sentence simple explanation of what went wrong",
  "root_cause":    "most likely reason this happened",
  "action":        "exact next step the finance team should take",
  "urgency":       one of ["High", "Medium", "Low"]
}}

Mismatch details:
- Party Name   : {party_name}
- PAN          : {pan}
- TDS Section  : {tds_section}
- ERP Amount   : ₹{erp_amount:,.0f}
- Portal Amount: ₹{portal_amount:,.0f}
- Difference   : ₹{difference:,.0f}
- Mismatch Type: {mismatch_type}
"""

SUMMARY_SYSTEM = """
You are a senior CA writing a one-paragraph executive summary of a 
TDS reconciliation report. Be direct, factual, and professional.
Respond with plain text only — no bullet points, no markdown.
"""

SUMMARY_USER = """
Write a 3-4 sentence executive summary for a TDS reconciliation report with these findings:

Period        : {period}
Company       : {company}
Total ERP TDS : ₹{total_erp:,.0f}
Total Portal  : ₹{total_portal:,.0f}
Net Difference: ₹{net_diff:,.0f}
Matched items : {matched}
Mismatches    : {mismatches}

Mismatch breakdown:
{breakdown}
"""


def _explain_mismatch(client: Groq, row: pd.Series) -> dict:
    """Call Groq to explain one mismatch row. Returns dict with LLM fields."""
    prompt = EXPLAIN_USER.format(
        party_name    = row["party_name"],
        pan           = row["pan"],
        tds_section   = row["tds_section"],
        erp_amount    = row["erp_amount"],
        portal_amount = row["portal_amount"],
        difference    = row["difference"],
        mismatch_type = row["mismatch_type"],
    )
    try:
        resp = client.chat.completions.create(
            model      = "llama-3.3-70b-versatile",
            messages   = [
                {"role": "system", "content": EXPLAIN_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature = 0.2,
            max_tokens  = 300,
        )
        raw  = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences
        raw  = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"[Report Agent] LLM explanation failed for {row['pan']}: {e}")
        return {
            "plain_english": row["recommended_action"],
            "root_cause":    "Could not determine automatically.",
            "action":        "Review manually with your CA.",
            "urgency":       "Medium",
        }


def _build_summary(client: Groq, mismatches_df: pd.DataFrame,
                   period: str, matched_count: int) -> str:
    """Generate an executive summary paragraph via Groq."""
    breakdown_lines = (
        mismatches_df.groupby("mismatch_type")["difference"]
        .agg(count="count", total=lambda x: x.abs().sum())
        .reset_index()
        .apply(lambda r: f"  {r['mismatch_type']}: {int(r['count'])} item(s), "
                         f"₹{r['total']:,.0f} at stake", axis=1)
        .tolist()
    )
    prompt = SUMMARY_USER.format(
        period      = period,
        company     = config.COMPANY_NAME,
        total_erp   = mismatches_df["erp_amount"].sum(),
        total_portal= mismatches_df["portal_amount"].sum(),
        net_diff    = mismatches_df["difference"].sum(),
        matched     = matched_count,
        mismatches  = len(mismatches_df),
        breakdown   = "\n".join(breakdown_lines),
    )
    try:
        resp = client.chat.completions.create(
            model      = "llama-3.3-70b-versatile",
            messages   = [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature = 0.3,
            max_tokens  = 250,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[Report Agent] Summary generation failed: {e}")
        return (f"TDS reconciliation for {period} identified {len(mismatches_df)} "
                f"mismatches requiring attention.")


def enrich_with_llm(mismatches_df: pd.DataFrame, period: str,
                    matched_count: int = 0) -> tuple[pd.DataFrame, str]:
    """
    Add LLM-generated explanations to each mismatch row.
    Also generates an executive summary paragraph.

    Returns:
        enriched_df : mismatches_df with extra columns
                      [plain_english, root_cause, llm_action, urgency]
        summary_text: executive summary string
    """
    if mismatches_df.empty:
        return mismatches_df, "No mismatches found — all TDS entries are reconciled."

    client = Groq(api_key=config.GROQ_API_KEY)
    logger.info(f"[Report Agent] Explaining {len(mismatches_df)} mismatches via Groq...")

    explanations = []
    for i, row in mismatches_df.iterrows():
        logger.info(f"[Report Agent] [{i+1}/{len(mismatches_df)}] "
                    f"{row['party_name']} — {row['mismatch_type']}")
        exp = _explain_mismatch(client, row)
        explanations.append(exp)

    exp_df = pd.DataFrame(explanations)
    enriched = mismatches_df.copy().reset_index(drop=True)
    enriched["plain_english"] = exp_df.get("plain_english", "")
    enriched["root_cause"]    = exp_df.get("root_cause",    "")
    enriched["llm_action"]    = exp_df.get("action",        "")
    enriched["urgency"]       = exp_df.get("urgency",       "Medium")

    logger.info("[Report Agent] Generating executive summary...")
    summary = _build_summary(client, mismatches_df, period, matched_count)

    logger.success("[Report Agent] LLM enrichment complete")
    return enriched, summary


# ── HTML / PDF rendering ──────────────────────────────────────

def _render_html(enriched_df: pd.DataFrame, summary: str,
                 matched_rows: list, period: str) -> str:
    """Render the Jinja2 template and return HTML string."""
    from jinja2 import Environment, FileSystemLoader
    template_dir = Path(__file__).parent.parent / "templates"
    env      = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("report.html")

    year, month = period[:4], period[5:7]
    import calendar
    period_label = f"{calendar.month_name[int(month)]} {year}"

    # Build mismatch dicts for template
    mismatch_rows = [
        {
            "pan":           row["pan"],
            "party_name":    row["party_name"],
            "tds_section":   row["tds_section"],
            "erp_amount":    row["erp_amount"],
            "portal_amount": row["portal_amount"],
            "difference":    row["difference"],
            "mismatch_type": row["mismatch_type"],
            "plain_english": row.get("plain_english", ""),
            "root_cause":    row.get("root_cause",    ""),
            "llm_action":    row.get("llm_action",    ""),
            "urgency":       row.get("urgency",       "Medium"),
        }
        for _, row in enriched_df.iterrows()
    ]
    # Sort: High → Medium → Low
    urgency_order = {"High": 0, "Medium": 1, "Low": 2}
    mismatch_rows.sort(key=lambda r: urgency_order.get(r["urgency"], 9))
    total_erp    = enriched_df["erp_amount"].sum()    if not enriched_df.empty else 0
    total_portal = enriched_df["portal_amount"].sum() if not enriched_df.empty else 0

    # Add matched rows totals from matched_rows list
    for r in matched_rows:
        total_erp    += r.get("erp_amount", 0)
        total_portal += r.get("portal_amount", 0)

    html = template.render(
        company      = config.COMPANY_NAME,
        period       = period,
        period_label = period_label,
        generated_at = datetime.now().strftime("%d %b %Y, %H:%M"),
        summary      = summary,
        mismatches   = mismatch_rows,
        matched_rows = matched_rows,
        matched_count= len(matched_rows),
        total_erp    = total_erp,
        total_portal = total_portal,
    )
    return html


def _save_html(html: str, period: str) -> str:
    """Save HTML to reports/ and return path."""
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    filename = f"TDS_Recon_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    path     = str(Path(config.REPORTS_DIR) / filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.success(f"[Report Agent] HTML saved: {path}")
    return path


def _html_to_pdf(html_path: str) -> str:
    """Convert HTML to PDF using pdfkit. All styles are inline so wkhtmltopdf renders correctly."""
    try:
        import pdfkit
        pdf_path = html_path.replace(".html", ".pdf")
        wk_paths = [
            r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
            r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
        ]
        config_obj = None
        for p in wk_paths:
            if os.path.exists(p):
                config_obj = pdfkit.configuration(wkhtmltopdf=p)
                break

        options = {
            "page-size":                "A4",
            "margin-top":               "12mm",
            "margin-bottom":            "12mm",
            "margin-left":              "12mm",
            "margin-right":             "12mm",
            "encoding":                 "UTF-8",
            "no-outline":               None,
            "disable-smart-shrinking":  None,
            "print-media-type":         None,
            "enable-local-file-access": None,
            "zoom":                     "1.0",
        }
        kwargs = {"options": options}
        if config_obj:
            kwargs["configuration"] = config_obj

        # Use from_string to avoid blank PDF bug with Windows local file paths
        with open(html_path, "r", encoding="utf-8") as fh:
            html_string = fh.read()
        pdfkit.from_string(html_string, pdf_path, **kwargs)
        logger.success(f"[Report Agent] PDF saved: {pdf_path}")
        return pdf_path
    except Exception as e:
        logger.warning(f"[Report Agent] PDF failed ({e}) — HTML report still saved.")
        return html_path


def generate_report(mismatches_df: pd.DataFrame, period: str,
                    matched_rows: list = None, matched_count: int = 0) -> str:
    """
    Full pipeline:
      1. Enrich mismatches with LLM explanations  (Phase 5)
      2. Render Jinja2 HTML template              (Phase 6)
      3. Convert HTML → PDF via WeasyPrint        (Phase 6)

    matched_rows: list of dicts for matched (clean) items to show in report
    Returns path to the saved PDF (or HTML if PDF fails).
    """
    matched_rows = matched_rows or []

    # ── Step 1: LLM enrichment ────────────────────────────────
    enriched_df, summary = enrich_with_llm(mismatches_df, period,
                                           matched_count=len(matched_rows))

    # ── Step 2: Render HTML ───────────────────────────────────
    html     = _render_html(enriched_df, summary, matched_rows, period)
    html_path = _save_html(html, period)

    # ── Step 3: PDF ───────────────────────────────────────────
    final_path = _html_to_pdf(html_path)

    return final_path