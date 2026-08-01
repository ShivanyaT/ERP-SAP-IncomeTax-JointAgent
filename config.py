from dotenv import load_dotenv
import os

load_dotenv()

# ── SAP ──────────────────────────────────────────────────────
SAP_HOST     = os.getenv("SAP_HOST")
SAP_SYSNR    = os.getenv("SAP_SYSNR", "00")
SAP_CLIENT   = os.getenv("SAP_CLIENT", "100")
SAP_USER     = os.getenv("SAP_USER")
SAP_PASSWORD = os.getenv("SAP_PASSWORD")
SAP_LANG     = os.getenv("SAP_LANG", "EN")

# ── Income Tax Portal ─────────────────────────────────────────
IT_PORTAL_URL = os.getenv("IT_PORTAL_URL")
IT_USER_PAN   = os.getenv("IT_USER_PAN")
IT_PASSWORD   = os.getenv("IT_PASSWORD")

# ── Groq ──────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ── Report ────────────────────────────────────────────────────
REPORT_PERIOD  = os.getenv("REPORT_PERIOD", "2024-04")
COMPANY_NAME   = os.getenv("COMPANY_NAME", "Your Company")
REPORTS_DIR    = os.getenv("REPORTS_DIR", "reports")
DOWNLOADS_DIR  = os.getenv("DOWNLOADS_DIR", "downloads")


def validate_config():
    """Call this at startup to catch missing credentials early."""
    required = {
        "SAP_HOST": SAP_HOST,
        "SAP_USER": SAP_USER,
        "SAP_PASSWORD": SAP_PASSWORD,
        "IT_USER_PAN": IT_USER_PAN,
        "IT_PASSWORD": IT_PASSWORD,
        "GROQ_API_KEY": GROQ_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing required env variables: {', '.join(missing)}\n"
            f"Check your .env file."
        )