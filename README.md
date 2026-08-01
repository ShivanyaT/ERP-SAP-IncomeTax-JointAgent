# TianBot — Tax Reconciliation Bot

TianBot is a multi-agent Python system that reconciles TDS/withholding-tax entries between an ERP/SAP system and the Indian Income Tax portal (26AS/AIS). It uses natural-language intent classification (via Groq LLM) to drive data fetching, reconciliation, and report generation through a chat interface.

## Overview

Tax teams typically reconcile TDS entries booked in ERP/SAP against the tax department's 26AS/AIS records manually — a slow, error-prone process. TianBot automates this end-to-end: fetch data from both sources (live or mocked), reconcile them, and produce a report explaining every mismatch in plain language.

## Key Features

- **Multi-agent architecture** — dedicated agents for ERP data, tax portal data, reconciliation logic, and report generation.
- **Natural-language interface** — a Groq LLM classifies free-text user requests into intents (`fetch_erp`, `fetch_tax`, `reconcile`, `report`) and routes them to the right agent.
- **SAP GUI automation** — the ERP agent connects to SAP via Windows COM scripting to pull TDS entries directly from SAP GUI, and parses exported Excel data into a normalized DataFrame.
- **Tax portal automation** — the portal agent automates browser login, OTP handling, AIS navigation, and CSV download from the Income Tax portal, then normalizes the downloaded data.
- **Reconciliation engine** — normalizes PANs and TDS sections, aggregates ERP and portal amounts by PAN + section, and classifies mismatches into:
  - Missing in 26AS/AIS
  - Missing in ERP books
  - Amount mismatch
  
  Each mismatch comes with a recommended action.
- **LLM-powered reporting** — mismatches are enriched with plain-English explanations and urgency ratings, rolled up into an executive summary, and rendered as an HTML report (optionally converted to PDF via `pdfkit`/`wkhtmltopdf`).
- **Mock-data fallback** — if SAP or portal credentials aren't configured, TianBot automatically falls back to demo/mock data so the full pipeline can still be exercised.
- **Two interfaces** — a CLI/chat entry point (`main.py`) and a Flask web app (`app.py`) with a simple browser chat UI.

## Architecture

```
TianBot/
├── main.py              # CLI entry point — intent classification & routing
├── app.py                # Flask web app (chat UI, /chat endpoint, report serving)
├── config.py              # Loads credentials & settings from .env
├── requirements.txt
├── agents/
│   ├── erp_agent.py        # SAP GUI automation, ERP data fetch & normalization
│   ├── portal_agent.py      # Portal browser automation, AIS/26AS fetch & normalization
│   ├── recon_engine.py       # PAN/section normalization, aggregation, mismatch classification
│   └── report_agent.py       # LLM explanations, executive summary, HTML/PDF report generation
└── ui/
    └── index.html           # Browser-based chat UI
```

### Workflow

1. User submits a request in natural language (CLI or chat UI).
2. Groq LLM classifies the request into an intent.
3. The relevant agent(s) fetch ERP and/or portal data (live or mock).
4. The reconciliation engine compares the two datasets and flags mismatches.
5. The report agent generates an LLM-annotated HTML/PDF report summarizing the findings.

## Tech Stack

- **Language:** Python
- **LLM:** Groq (intent classification, report explanations)
- **Web framework:** Flask
- **Automation:** `pywinauto`, `pywin32` (SAP GUI COM scripting), browser automation (portal login/OTP/download)
- **Data processing:** `pandas`
- **Reporting:** Jinja2 (HTML templates), `pdfkit` / `wkhtmltopdf` (PDF conversion)
- **Config/logging:** `python-dotenv`, `loguru`

## Setup

1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file with the required credentials:
   - SAP/ERP credentials
   - Income Tax portal credentials
   - Groq API key
   - Report/download paths
3. Run via CLI:
   ```bash
   python main.py
   ```
   or launch the web app:
   ```bash
   python app.py
   ```

If SAP or portal credentials are not provided, TianBot runs in demo mode using mock data.

### Test Modes (CLI)

```bash
python main.py --test-erp        # Test ERP agent
python main.py --test-portal      # Test portal agent
python main.py --test-recon       # Test reconciliation engine
python main.py --test-report      # Test report generation
python main.py --test-llm         # Test LLM intent classification
```

## Notes

- Live SAP GUI automation is Windows-specific.
- PDF report generation requires `wkhtmltopdf` to be installed on the system.
- Credentials and API keys are stored in `.env` and should never be committed to version control.
