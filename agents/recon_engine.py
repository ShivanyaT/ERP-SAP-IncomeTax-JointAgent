import pandas as pd
from loguru import logger


# ── Mismatch type labels ──────────────────────────────────────

MISSING_IN_PORTAL  = "Missing in 26AS/AIS"
MISSING_IN_BOOKS   = "Missing in ERP Books"
AMOUNT_MISMATCH    = "Amount Mismatch"
SECTION_MISMATCH   = "TDS Section Mismatch"


# ── Helpers ───────────────────────────────────────────────────

def _normalise_pan(series: pd.Series) -> pd.Series:
    """Uppercase, strip whitespace, replace blanks with UNKNOWN."""
    return series.astype(str).str.strip().str.upper().replace("NAN", "UNKNOWN")


def _normalise_section(series: pd.Series) -> pd.Series:
    """Strip spaces, uppercase. '194 C' → '194C'"""
    return series.astype(str).str.replace(" ", "").str.upper().str.strip()


def _prepare_erp(erp_df: pd.DataFrame) -> pd.DataFrame:
    df = erp_df.copy()
    df["vendor_pan"]  = _normalise_pan(df["vendor_pan"])
    df["tds_section"] = _normalise_section(df["tds_section"])
    df["tds_amount"]  = pd.to_numeric(df["tds_amount"], errors="coerce").fillna(0)
    return df


def _prepare_portal(portal_df: pd.DataFrame) -> pd.DataFrame:
    df = portal_df.copy()
    df["deductee_pan"] = _normalise_pan(df["deductee_pan"])
    df["tds_section"]  = _normalise_section(df["tds_section"])
    df["tds_amount"]   = pd.to_numeric(df["tds_amount"], errors="coerce").fillna(0)
    return df


def _aggregate(df: pd.DataFrame, pan_col: str) -> pd.DataFrame:
    """
    Group by PAN + TDS section and sum amounts.
    One row per (vendor, section) pair — same as how 26AS aggregates.
    """
    return (
        df.groupby([pan_col, "tds_section"], as_index=False)
          .agg(tds_amount=("tds_amount", "sum"))
    )


# ── Core reconciliation ───────────────────────────────────────

def reconcile(erp_df: pd.DataFrame, portal_df: pd.DataFrame,
              amount_tolerance: float = 1.0) -> pd.DataFrame:
    """
    Compare ERP TDS data against Income Tax portal (26AS/AIS) data.

    Logic:
      1. Aggregate both datasets by (PAN, TDS section)
      2. Outer-join on PAN + section
      3. Classify each row:
           - Only in ERP          → Missing in 26AS/AIS
           - Only in portal       → Missing in ERP Books
           - Both but diff amount → Amount Mismatch  (> tolerance)
           - Both, same amount    → Matched (excluded from output)

    amount_tolerance: differences <= this value (₹) are treated as matched
                      (handles rounding differences)

    Returns DataFrame of mismatches only, sorted by mismatch_type then amount.
    """

    if erp_df.empty and portal_df.empty:
        logger.warning("[Recon] Both datasets are empty — nothing to reconcile")
        return pd.DataFrame()

    logger.info(f"[Recon] ERP records: {len(erp_df)} | Portal records: {len(portal_df)}")

    # ── Prepare & aggregate ───────────────────────────────────
    erp    = _prepare_erp(erp_df)
    portal = _prepare_portal(portal_df)

    # Get vendor name lookup from ERP (pan → name)
    name_map = (
        erp[["vendor_pan", "vendor_name"]]
        .drop_duplicates("vendor_pan")
        .set_index("vendor_pan")["vendor_name"]
        .to_dict()
    )
    # Also pull deductor names from portal
    portal_name_map = (
        portal[["deductee_pan", "deductor_name"]]
        .drop_duplicates("deductee_pan")
        .set_index("deductee_pan")["deductor_name"]
        .to_dict()
    )

    erp_agg    = _aggregate(erp,    "vendor_pan").rename(
                     columns={"vendor_pan": "pan", "tds_amount": "erp_amount"})
    portal_agg = _aggregate(portal, "deductee_pan").rename(
                     columns={"deductee_pan": "pan", "tds_amount": "portal_amount"})

    # ── Outer join ────────────────────────────────────────────
    merged = pd.merge(
        erp_agg, portal_agg,
        on=["pan", "tds_section"],
        how="outer"
    )
    merged["erp_amount"]    = merged["erp_amount"].fillna(0)
    merged["portal_amount"] = merged["portal_amount"].fillna(0)
    merged["difference"]    = merged["erp_amount"] - merged["portal_amount"]

    # ── Classify ──────────────────────────────────────────────
    def classify(row):
        if row["erp_amount"] == 0:
            return MISSING_IN_BOOKS
        if row["portal_amount"] == 0:
            return MISSING_IN_PORTAL
        if abs(row["difference"]) > amount_tolerance:
            return AMOUNT_MISMATCH
        return "Matched"

    merged["mismatch_type"] = merged.apply(classify, axis=1)

    # ── Drop matched rows ─────────────────────────────────────
    mismatches = merged[merged["mismatch_type"] != "Matched"].copy()

    # ── Attach vendor/deductor names ─────────────────────────
    mismatches["party_name"] = mismatches["pan"].map(
        lambda p: name_map.get(p) or portal_name_map.get(p) or "Unknown"
    )

    # ── Recommended action ────────────────────────────────────
    def recommend(row):
        if row["mismatch_type"] == MISSING_IN_PORTAL:
            return (f"TDS of ₹{row['erp_amount']:,.0f} booked in ERP but not in 26AS. "
                    f"Follow up with deductor {row['party_name']} to deposit and file TDS return.")
        if row["mismatch_type"] == MISSING_IN_BOOKS:
            return (f"₹{row['portal_amount']:,.0f} TDS appears in 26AS but not in ERP books. "
                    f"Check if invoice from {row['party_name']} was posted. May need a journal entry.")
        if row["mismatch_type"] == AMOUNT_MISMATCH:
            diff = abs(row["difference"])
            direction = "over-reported in ERP" if row["difference"] > 0 else "under-reported in ERP"
            return (f"Amount {direction} by ₹{diff:,.0f}. "
                    f"ERP: ₹{row['erp_amount']:,.0f} vs 26AS: ₹{row['portal_amount']:,.0f}. "
                    f"Verify invoice and TDS certificate from {row['party_name']}.")
        return "Review manually."

    mismatches["recommended_action"] = mismatches.apply(recommend, axis=1)

    # ── Final column order ────────────────────────────────────
    mismatches = mismatches[[
        "pan", "party_name", "tds_section",
        "erp_amount", "portal_amount", "difference",
        "mismatch_type", "recommended_action"
    ]].sort_values(
        by=["mismatch_type", "difference"],
        key=lambda col: col.abs() if col.name == "difference" else col,
        ascending=[True, False]
    ).reset_index(drop=True)

    logger.success(f"[Recon] Done — {len(mismatches)} mismatches found "
                   f"({(merged['mismatch_type']=='Matched').sum()} matched)")
    return mismatches