import os
import time
import glob
import subprocess
from pathlib import Path
from datetime import datetime, date

import pandas as pd
from loguru import logger

import config

# ── Mock data (used when SAP is not available) ────────────────

def _mock_erp_data(period: str) -> pd.DataFrame:
    """
    Returns realistic sample ERP/SAP TDS data for testing.
    Intentionally includes mismatches with mock portal data for demo purposes.
    """
    year, month = int(period[:4]), int(period[5:7])
    data = [
        # (vendor_pan,      vendor_name,              tds_section, tds_amount, invoice_amount, doc_number)
        ("AABCS1234C", "Infosys Limited",              "194C",  42000,  350000, "5100001234"),
        ("AAACB2222B", "Tata Consultancy Services",    "194J",  80000,  800000, "5100001235"),
        ("AABCT3456D", "Wipro Technologies",            "194C",  15000,  150000, "5100001236"),
        ("AABCM7890E", "Mahindra & Mahindra",           "194C",  25000,  250000, "5100001237"),
        ("AABCH4567F", "HCL Technologies",              "194J",  60000,  600000, "5100001238"),
        # This entry intentionally missing from portal (to create a mismatch)
        ("AABCZ9999Z", "Zomato Pvt Ltd",               "194C",  8000,   80000,  "5100001239"),
        # This entry has an amount mismatch with portal
        ("AABCP1122G", "Paytm Payments Bank",          "194A",  12000,  120000, "5100001240"),
    ]
    rows = []
    for pan, name, section, tds, inv, doc in data:
        rows.append({
            "vendor_pan":      pan,
            "vendor_name":     name,
            "tds_section":     section,
            "tds_amount":      tds,
            "invoice_amount":  inv,
            "posting_date":    date(year, month, 15),
            "document_number": doc,
        })
    df = pd.DataFrame(rows)
    logger.success(f"[ERP Agent] Mock data loaded — {len(df)} records for {period}")
    return df


# ── Helpers ───────────────────────────────────────────────────

def _month_range(period: str):
    """'2025-03' → ('01.03.2025', '31.03.2025') SAP date format"""
    import calendar
    year, month = int(period[:4]), int(period[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return f"01.{month:02d}.{year}", f"{last_day}.{month:02d}.{year}"


def _wait_for_file(folder: str, pattern: str, timeout: int = 30) -> str | None:
    deadline = time.time() + timeout
    before   = set(glob.glob(os.path.join(folder, pattern)))
    while time.time() < deadline:
        time.sleep(1)
        after = set(glob.glob(os.path.join(folder, pattern)))
        new   = after - before
        if new:
            return sorted(new)[-1]
    return None


def _launch_sap() -> bool:
    import psutil
    for proc in psutil.process_iter(['name']):
        if 'saplogon' in proc.info['name'].lower():
            logger.info("[ERP Agent] SAP Logon already running")
            return False
    sap_paths = [
        r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe",
        r"C:\Program Files\SAP\FrontEnd\SAPgui\saplogon.exe",
    ]
    for path in sap_paths:
        if os.path.exists(path):
            subprocess.Popen([path])
            logger.info("[ERP Agent] SAP Logon launched — waiting...")
            time.sleep(6)
            return True
    raise FileNotFoundError(
        "SAP Logon not found. Check install path in erp_agent.py → _launch_sap()"
    )


def _get_sap_session():
    import win32com.client
    try:
        sap_gui_auto = win32com.client.GetObject("SAPGUI")
        application  = sap_gui_auto.GetScriptingEngine
        connection   = application.Children(0)
        session      = connection.Children(0)
        logger.info("[ERP Agent] Attached to SAP session")
        return session
    except Exception as e:
        raise ConnectionError(
            f"Cannot connect to SAP GUI: {e}\n"
            "Ensure SAP is open and scripting is enabled:\n"
            "  SAP GUI → Options → Accessibility & Scripting → Enable scripting"
        )


def _login_sap(session):
    try:
        if session.findById("wnd[0]/usr/txtRSYST-BNAME", False):
            logger.info("[ERP Agent] Logging in...")
            session.findById("wnd[0]/usr/txtRSYST-MANDT").text = config.SAP_CLIENT
            session.findById("wnd[0]/usr/txtRSYST-BNAME").text = config.SAP_USER
            session.findById("wnd[0]/usr/txtRSYST-BCODE").text = config.SAP_PASSWORD
            session.findById("wnd[0]").sendVKey(0)
            time.sleep(3)
            logger.success("[ERP Agent] Logged in")
    except Exception:
        logger.info("[ERP Agent] Already logged in")


def _fill_fbl3n(session, date_from, date_to):
    for fid in ["ctxtSO_BUDAT-LOW", "ctxtPA_AUGBD-LOW"]:
        try: session.findById(f"wnd[0]/usr/{fid}").text = date_from
        except Exception: pass
    for fid in ["ctxtSO_BUDAT-HIGH", "ctxtPA_AUGBD-HIGH"]:
        try: session.findById(f"wnd[0]/usr/{fid}").text = date_to
        except Exception: pass


def _fill_s_alr(session, date_from, date_to):
    try: session.findById("wnd[0]/usr/ctxtP_FKDAT-LOW").text  = date_from
    except Exception: pass
    try: session.findById("wnd[0]/usr/ctxtP_FKDAT-HIGH").text = date_to
    except Exception: pass


def _export_to_excel(session, tcode, downloads_dir) -> str:
    try:
        session.findById("wnd[0]/mbar/menu[0]/menu[1]/menu[2]").select()
    except Exception:
        try: session.findById("wnd[0]").sendVKey(45)
        except Exception: pass
    time.sleep(2)
    try:
        export_wnd = session.findById("wnd[1]", False)
        if export_wnd:
            try:
                session.findById(
                    "wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150"
                    "/sub:SAPLSPO5:0200/radSPOPLI-SELFLAG[1,0]"
                ).select()
            except Exception: pass
            session.findById("wnd[1]/tbar[0]/btn[0]").press()
            time.sleep(1)
    except Exception: pass

    filename = f"ERP_{tcode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    out_path  = str(Path(downloads_dir).resolve() / filename)
    try:
        for wid in ["wnd[1]", "wnd[2]"]:
            try:
                save_wnd = session.findById(wid, False)
                if save_wnd:
                    for fid in ["usr/ctxtDY_FILENAME", "usr/ctxtFILENAME"]:
                        try:
                            save_wnd.findById(fid).text = out_path
                            break
                        except Exception: pass
                    save_wnd.findById("tbar[0]/btn[0]").press()
                    time.sleep(2)
                    break
            except Exception: pass
    except Exception:
        logger.warning("[ERP Agent] Could not set save path")

    logger.success(f"[ERP Agent] Exported to: {out_path}")
    return out_path


def _run_transaction(session, tcode, date_from, date_to, downloads_dir) -> str:
    session.findById("wnd[0]/tbar[0]/okcd").text = f"/{tcode}"
    session.findById("wnd[0]").sendVKey(0)
    time.sleep(2)
    if tcode in ("FBL3N", "FBL1N"):
        _fill_fbl3n(session, date_from, date_to)
    elif tcode == "S_ALR_87012357":
        _fill_s_alr(session, date_from, date_to)
    session.findById("wnd[0]").sendVKey(8)   # F8 Execute
    time.sleep(4)
    return _export_to_excel(session, tcode, downloads_dir)


def _parse_erp_excel(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, header=0)
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {
        "Vendor": "vendor_name", "Vendor Name": "vendor_name", "Name 1": "vendor_name",
        "Tax Number 1": "vendor_pan", "PAN": "vendor_pan", "STCD1": "vendor_pan",
        "W/Tax Code": "tds_section", "Withholding Tax Code": "tds_section",
        "W/Tax Amount": "tds_amount", "Withholding Tax Amount": "tds_amount",
        "Amount in Local Currency": "invoice_amount", "Amount": "invoice_amount",
        "Posting Date": "posting_date", "Document Date": "posting_date",
        "Document Number": "document_number", "Doc. No.": "document_number",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)
    for col in ["vendor_pan","vendor_name","tds_section",
                "tds_amount","invoice_amount","posting_date","document_number"]:
        if col not in df.columns:
            df[col] = None
    df["tds_amount"]     = pd.to_numeric(df["tds_amount"],     errors="coerce").fillna(0)
    df["invoice_amount"] = pd.to_numeric(df["invoice_amount"], errors="coerce").fillna(0)
    df["posting_date"]   = pd.to_datetime(df["posting_date"],  errors="coerce")
    df = df[df["document_number"].notna()].copy()
    df.reset_index(drop=True, inplace=True)
    logger.info(f"[ERP Agent] Parsed {len(df)} records from {filepath}")
    return df[["vendor_pan","vendor_name","tds_section",
               "tds_amount","invoice_amount","posting_date","document_number"]]


# ── Public entry point ────────────────────────────────────────

def fetch_erp_data(period: str, transaction_code: str = "FBL3N",
                   mock: bool = False) -> pd.DataFrame:
    """
    Fetch TDS/ledger data from SAP for the given period.

    mock=True  → returns sample data (no SAP needed, for development/testing)
    mock=False → connects to real SAP GUI via COM scripting
    """
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)

    if mock:
        return _mock_erp_data(period)

    # ── Real SAP flow ─────────────────────────────────────────
    date_from, date_to = _month_range(period)
    logger.info(f"[ERP Agent] {transaction_code} | {date_from} → {date_to}")

    _launch_sap()
    session   = _get_sap_session()
    _login_sap(session)
    xlsx_path = _run_transaction(session, transaction_code,
                                 date_from, date_to, config.DOWNLOADS_DIR)

    if not os.path.exists(xlsx_path):
        found = _wait_for_file(config.DOWNLOADS_DIR, "*.xlsx", timeout=20)
        if found:
            xlsx_path = found
        else:
            raise FileNotFoundError(
                f"SAP export not found in {config.DOWNLOADS_DIR}.\n"
                "Try a manual export once to verify SAP scripting is working."
            )

    return _parse_erp_excel(xlsx_path)