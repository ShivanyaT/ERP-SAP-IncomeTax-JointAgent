import os
import time
import glob
import subprocess
from pathlib import Path
from datetime import datetime, date

import pandas as pd
from loguru import logger

import config


# ── Mock data ─────────────────────────────────────────────────

def _mock_portal_data(period: str) -> pd.DataFrame:
    """
    Returns realistic sample 26AS / AIS data for testing.
    Intentionally has mismatches vs mock ERP data:
      - Zomato entry missing (present in ERP, not here)
      - Paytm amount is different (12000 in ERP, 9500 here)
      - HDFC Bank entry exists here but not in ERP
    """
    year, month = int(period[:4]), int(period[5:7])
    data = [
        # (deductor_tan,  deductor_name,              pan,          section, amount,  ack_number)
        ("BLRI12345E", "Infosys Limited",              "AABCS1234C", "194C",  42000,  "ACK2025001"),
        ("MUMR56789F", "Tata Consultancy Services",    "AAACB2222B", "194J",  80000,  "ACK2025002"),
        ("PNEA11111G", "Wipro Technologies",            "AABCT3456D", "194C",  15000,  "ACK2025003"),
        ("MUMA22222H", "Mahindra & Mahindra",           "AABCM7890E", "194C",  25000,  "ACK2025004"),
        ("CHNI33333I", "HCL Technologies",              "AABCH4567F", "194J",  60000,  "ACK2025005"),
        # Paytm — amount mismatch (ERP shows 12000, portal shows 9500)
        ("DELI44444J", "Paytm Payments Bank",          "AABCP1122G", "194A",  9500,   "ACK2025006"),
        # Extra entry in portal not in ERP — bot should flag this too
        ("MUMB55555K", "HDFC Bank Limited",            "AABCH9876B", "194A",  35000,  "ACK2025007"),
    ]
    rows = []
    for tan, name, pan, section, amount, ack in data:
        rows.append({
            "deductor_tan":          tan,
            "deductor_name":         name,
            "deductee_pan":          pan,
            "tds_section":           section,
            "tds_amount":            amount,
            "deposit_date":          date(year, month, 20),
            "acknowledgement_number": ack,
        })
    df = pd.DataFrame(rows)
    logger.success(f"[Portal Agent] Mock data loaded — {len(df)} records for {period}")
    return df


# ── Browser automation helpers ────────────────────────────────

def _open_browser(url: str):
    """Open URL in the default browser."""
    logger.info(f"[Portal Agent] Opening: {url}")
    subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    time.sleep(4)


def _find_browser_window():
    """Return pywinauto handle to the browser window containing the IT portal."""
    from pywinauto import Desktop
    desktop = Desktop(backend="uia")
    keywords = ["income tax", "incometax", "e-filing"]
    for win in desktop.windows():
        try:
            title = win.window_text().lower()
            if any(kw in title for kw in keywords):
                logger.info(f"[Portal Agent] Found browser window: {win.window_text()}")
                return win
        except Exception:
            pass
    raise RuntimeError(
        "Could not find the Income Tax portal browser window.\n"
        "Make sure the portal is open and visible on screen."
    )


def _click_and_type(window, search_text: str, value: str):
    """Find a field by its label/placeholder and type into it."""
    try:
        field = window.child_window(title=search_text, control_type="Edit")
        field.set_focus()
        field.type_keys(value, with_spaces=True)
        time.sleep(0.5)
    except Exception as e:
        logger.warning(f"[Portal Agent] Could not type into '{search_text}': {e}")


def _click_button(window, label: str):
    """Click a button by its label."""
    try:
        btn = window.child_window(title=label, control_type="Button")
        btn.click_input()
        time.sleep(2)
    except Exception as e:
        logger.warning(f"[Portal Agent] Could not click '{label}': {e}")


def _wait_for_download(folder: str, pattern: str = "*.csv", timeout: int = 60) -> str:
    """Wait for a new file to appear in the downloads folder."""
    logger.info(f"[Portal Agent] Waiting for download in {folder}...")
    deadline  = time.time() + timeout
    before    = set(glob.glob(os.path.join(folder, pattern)))
    while time.time() < deadline:
        time.sleep(2)
        after = set(glob.glob(os.path.join(folder, pattern)))
        new   = after - before
        if new:
            path = sorted(new)[-1]
            logger.success(f"[Portal Agent] Downloaded: {path}")
            return path
    raise TimeoutError(
        f"Download did not complete within {timeout}s.\n"
        f"Check {folder} manually."
    )


# ── Login flow ────────────────────────────────────────────────

def _login(window):
    """Fill PAN + password and submit."""
    logger.info("[Portal Agent] Filling login form...")

    # PAN field
    _click_and_type(window, "Enter your user ID", config.IT_USER_PAN)

    # Continue button
    _click_button(window, "Continue")
    time.sleep(1)

    # Password field
    _click_and_type(window, "Password", config.IT_PASSWORD)

    # Login button
    _click_button(window, "Login")
    time.sleep(3)

    logger.info("[Portal Agent] Login submitted — checking for OTP screen...")


def _handle_otp(window) -> bool:
    """
    If an OTP screen is detected, pause and ask the user to enter it manually.
    Returns True if OTP was handled.
    """
    try:
        # Look for OTP input field
        otp_field = window.child_window(
            title_re=".*OTP.*|.*One Time.*|.*Verification.*",
            control_type="Edit"
        )
        if otp_field.exists(timeout=5):
            logger.info("[Portal Agent] OTP screen detected")
            print("\n" + "─"*50)
            print("  OTP required — check your registered mobile/email")
            otp = input("  Enter OTP here: ").strip()
            print("─"*50 + "\n")
            otp_field.set_focus()
            otp_field.type_keys(otp)
            time.sleep(0.5)
            _click_button(window, "Verify")
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


# ── Navigate to AIS and download ──────────────────────────────

def _navigate_to_ais(window, period: str):
    """
    Navigate to Annual Information Statement (AIS) for the given period
    and trigger the CSV download.
    """
    year = period[:4]
    logger.info(f"[Portal Agent] Navigating to AIS for FY {year}...")

    # Path: e-File → Income Tax Return → View AIS
    _click_button(window, "e-File")
    time.sleep(1)
    _click_button(window, "Income Tax Returns")
    time.sleep(1)
    _click_button(window, "View AIS")
    time.sleep(2)

    # Select financial year
    try:
        fy_dropdown = window.child_window(title_re=".*Financial Year.*", control_type="ComboBox")
        fy_dropdown.select(f"FY {year}-{str(int(year)+1)[2:]}")
        time.sleep(1)
    except Exception:
        logger.warning("[Portal Agent] Could not select financial year — using default")

    # Download TDS data as CSV
    logger.info("[Portal Agent] Downloading TDS section as CSV...")
    _click_button(window, "TDS/TCS")
    time.sleep(1)
    _click_button(window, "Download")
    time.sleep(1)

    # Choose CSV format if dialog appears
    try:
        csv_option = window.child_window(title_re=".*CSV.*", control_type="Button")
        if csv_option.exists(timeout=3):
            csv_option.click_input()
            time.sleep(1)
    except Exception:
        pass


# ── Parse downloaded CSV ──────────────────────────────────────

def _parse_portal_csv(filepath: str) -> pd.DataFrame:
    """
    Read the AIS/26AS CSV and normalise to standard schema.
    The IT portal export has varying column names — we map all common variants.
    """
    df = pd.read_csv(filepath, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]

    col_map = {
        "Deductor TAN":          "deductor_tan",
        "TAN of Deductor":       "deductor_tan",
        "Deductor Name":         "deductor_name",
        "Name of Deductor":      "deductor_name",
        "PAN of Deductee":       "deductee_pan",
        "Deductee PAN":          "deductee_pan",
        "Section":               "tds_section",
        "Nature of Payment":     "tds_section",
        "Amount Paid/Credited":  "tds_amount",
        "Tax Deducted":          "tds_amount",
        "TDS Amount":            "tds_amount",
        "Date of Deposit":       "deposit_date",
        "Transaction Date":      "deposit_date",
        "Acknowledgement No":    "acknowledgement_number",
        "Challan No":            "acknowledgement_number",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    for col in ["deductor_tan","deductor_name","deductee_pan","tds_section",
                "tds_amount","deposit_date","acknowledgement_number"]:
        if col not in df.columns:
            df[col] = None

    df["tds_amount"]  = pd.to_numeric(df["tds_amount"], errors="coerce").fillna(0)
    df["deposit_date"] = pd.to_datetime(df["deposit_date"], errors="coerce")
    df = df[df["acknowledgement_number"].notna()].copy()
    df.reset_index(drop=True, inplace=True)

    logger.info(f"[Portal Agent] Parsed {len(df)} records from {filepath}")
    return df[["deductor_tan","deductor_name","deductee_pan","tds_section",
               "tds_amount","deposit_date","acknowledgement_number"]]


# ── Public entry point ────────────────────────────────────────

def fetch_portal_data(period: str, mock: bool = False) -> pd.DataFrame:
    """
    Fetch 26AS / AIS TDS data from the Income Tax portal.

    mock=True  → returns sample data (no browser, no login needed)
    mock=False → automates the browser: login → OTP → AIS → download CSV
    """
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)

    if mock:
        return _mock_portal_data(period)

    # ── Live browser flow ─────────────────────────────────────
    logger.info("[Portal Agent] Starting browser automation...")

    _open_browser(config.IT_PORTAL_URL)

    window = _find_browser_window()
    _login(window)
    _handle_otp(window)

    # Verify we're logged in
    try:
        dashboard = window.child_window(title_re=".*Dashboard.*|.*e-Filing.*")
        if dashboard.exists(timeout=10):
            logger.success("[Portal Agent] Login successful — on dashboard")
    except Exception:
        logger.warning("[Portal Agent] Could not confirm dashboard — continuing anyway")

    _navigate_to_ais(window, period)

    # Wait for CSV to land in downloads
    csv_path = _wait_for_download(config.DOWNLOADS_DIR, "*.csv", timeout=60)

    return _parse_portal_csv(csv_path)