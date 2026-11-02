"""Organic Leak Tracer: 'organic' leads that arrived on a paid click.

The attribution gate is shut because no model can currently show that paid
spend drives leads. This agent does not model anything. It reads the click id
the lead physically arrived with, out of LEAD_ENTRANCEURL, and asks whether a
lead the CRM labelled organic came in on a Meta or Google ad click.

A click id is not an inference. If a lead labelled organic carries an fbclid,
that lead clicked a Meta ad. It is the one piece of paid-influence evidence
available that does not depend on the evidence gate at all, which is why it is
worth a standing agent rather than a one-off query.

PERSONAL DATA
-------------
LEADID and click ids identify individuals, and LEAD_ENTRANCEURL can carry more
besides. Nothing lead-level leaves this module: it returns counts, shares and
date ranges only, and the control-center state and UI never receive an id. The
parsing happens locally against a file that is already on this machine.

Extraction follows the value-bidding project's click-id map rather than
inventing a second rule -- Meta leads carry fbclid, Google leads carry gclid
with gbraid/wbraid as the iOS privacy-preserving fallbacks.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Google's own ids first, then Meta. Order matters only for the "which platform"
# label; a lead carrying both is counted once per platform column.
GOOGLE_PARAMS = ("gclid", "gbraid", "wbraid")
META_PARAMS = ("fbclid",)
ALL_PARAMS = GOOGLE_PARAMS + META_PARAMS

# Relative to the workspace root; override with CRM_EXPORT_GLOB.
CRM_GLOB = os.getenv("CRM_EXPORT_GLOB", "loi-scoring/data/crm/crm_export_merged*.xlsx")
SHEET = "merged"
CHANNEL_COLUMN = "LEAD_CHANNEL"
URL_COLUMN = "LEAD_ENTRANCEURL"

# Cached on the export's path+mtime: the file changes weekly at most, and
# re-parsing 7k rows on every dashboard poll would make the page crawl.
_CACHE: dict = {}


def _click_param(url, key: str):
    if not isinstance(url, str) or "?" not in url:
        return None
    try:
        return (parse_qs(urlparse(url).query).get(key) or [None])[0]
    except ValueError:
        return None


def newest_export(workspace_root: Path) -> Path | None:
    matches = sorted(workspace_root.glob(CRM_GLOB),
                     key=lambda path: path.stat().st_mtime)
    return matches[-1] if matches else None


def scan(workspace_root: Path) -> dict:
    """Aggregate counts only. See the PERSONAL DATA note above."""
    export = newest_export(workspace_root)
    if export is None:
        return {"status": "NO_EXPORT", "detail":
                "No CRM export found. Expected " + CRM_GLOB}

    key = (str(export), export.stat().st_mtime)
    if _CACHE.get("key") == key:
        return _CACHE["value"]

    try:
        import pandas as pd
    except ImportError:
        return {"status": "PANDAS_MISSING",
                "detail": "pandas is needed to read the CRM export."}

    try:
        frame = pd.read_excel(export, sheet_name=SHEET)
    except Exception as error:                      # noqa: BLE001
        return {"status": "UNREADABLE", "detail": f"{export.name}: {error}"}

    missing = [c for c in (CHANNEL_COLUMN, URL_COLUMN)
               if c not in frame.columns]
    if missing:
        return {"status": "COLUMNS_MISSING",
                "detail": f"{export.name} has no {', '.join(missing)}."}

    urls = frame[URL_COLUMN]
    found = {name: urls.map(lambda u, k=name: _click_param(u, k)).notna()
             for name in ALL_PARAMS}
    google = found["gclid"] | found["gbraid"] | found["wbraid"]
    meta = found["fbclid"]

    labelled_organic = (frame[CHANNEL_COLUMN].astype(str)
                        .str.contains("organic", case=False, na=False))
    organic = labelled_organic.sum()
    traced = int((labelled_organic & (google | meta)).sum())

    window = {}
    if "LEAD_CREATEDAT" in frame.columns:
        try:
            created = pd.to_datetime(frame.loc[labelled_organic,
                                               "LEAD_CREATEDAT"],
                                     errors="coerce").dropna()
            if len(created):
                window = {"first": str(created.min().date()),
                          "last": str(created.max().date())}
        except Exception:                            # noqa: BLE001
            window = {}

    result = {
        "status": "TRACED" if traced else "CLEAN",
        "export": export.name,
        "rows": int(len(frame)),
        "organic_labelled": int(organic),
        "traced_to_paid": traced,
        "traced_share": round(traced / organic, 4) if organic else 0.0,
        "by_platform": {
            "meta": int((labelled_organic & meta).sum()),
            "google": int((labelled_organic & google).sum()),
        },
        "by_param": {name: int((labelled_organic & found[name]).sum())
                     for name in ALL_PARAMS},
        # Context: how much of the whole file carries an id at all. Without it
        # a low organic share reads as "barely any leakage" when it might just
        # mean the export is thin on entrance URLs generally.
        "any_click_id_all_channels": int((google | meta).sum()),
        "window": window,
    }

    _CACHE["key"] = key
    _CACHE["value"] = result
    return result


def summarise(result: dict) -> str:
    """One sentence for the agent card."""
    status = result.get("status")
    if status == "NO_EXPORT":
        return "No CRM export available to scan."
    if status in {"PANDAS_MISSING", "UNREADABLE", "COLUMNS_MISSING"}:
        return result.get("detail", "The CRM export could not be read.")
    if not result.get("traced_to_paid"):
        return (f"No organic-labelled lead in {result['export']} carries a paid "
                f"click id.")
    share = result["traced_share"] * 100
    platforms = result["by_platform"]
    return (f"{result['traced_to_paid']} of {result['organic_labelled']} "
            f"organic-labelled leads ({share:.1f}%) arrived on a paid click: "
            f"{platforms['meta']} Meta, {platforms['google']} Google. "
            f"Observed, not modelled.")
