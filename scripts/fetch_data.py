#!/usr/bin/env python3
"""
fetch_data.py  —  Builds data.json for the CPI/PPI dashboard.

Runs server-side (GitHub Actions), so there is NO browser / CORS involved.
Pulls CPI + PPI from BLS (one batched POST each) and the Fed Funds rate from
FRED, then writes data.json next to index.html.

Design rules (per spec):
  * Efficient: 2 BLS calls total (CPI batch + PPI batch) + 1 FRED call.
  * No stale / AI fallback. On failure it writes status.ok=false with a NUMBERED
    error code and preserves the last good payload so the page stays usable while
    loudly showing "Data Pull Failed (E###)".
  * Standard library only (urllib/json) — no pip installs to break.

SERVER ERROR CODES (mirrored in README + dashboard):
  101  BLS_API_KEY env var missing
  201  BLS CPI request failed (network / HTTP / timeout)
  202  BLS CPI returned a non-success status
  203  BLS CPI response had no Results.series
  204  BLS CPI returned no usable headline series (CUSR0000SA0)
  301  BLS PPI request failed (network / HTTP / timeout)
  302  BLS PPI returned a non-success status
  303  BLS PPI response had no Results.series
  304  BLS PPI returned no usable headline series (WPSFD4)
  102  FRED_API_KEY missing            (non-fatal -> status.fred_error_code)
  401  FRED request failed             (non-fatal -> status.fred_error_code)
  402  FRED returned no observations    (non-fatal -> status.fred_error_code)
  501  Could not write data.json
  502  Unexpected exception
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "..", "data.json")  # repo root, next to index.html

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ── Series maps: keys MUST match SERIES / PPI_SERIES in index.html ───────────
CPI_SERIES = {
    "headline": "CUSR0000SA0", "core": "CUSR0000SA0L1E", "energy": "CUSR0000SA0E",
    "gasoline": "CUSR0000SETB01", "electricity": "CUSR0000SEHE", "natgas": "CUSR0000SEHF",
    "fueloil": "CUSR0000SEHE02", "food": "CUSR0000SAF", "foodhome": "CUSR0000SAF11",
    "foodaway": "CUSR0000SEFV", "shelter": "CUSR0000SAH1", "oer": "CUSR0000SEHC",
    "rent": "CUSR0000SEHA", "newveh": "CUSR0000SETA01", "usedveh": "CUSR0000SETA02",
    "airline": "CUSR0000SETG01", "mvins": "CUSR0000SETE", "carrental": "CUSR0000SETA04",
    "mvmaint": "CUSR0000SETD", "medcare": "CUSR0000SAM", "apparel": "CUSR0000SAA",
    "recreation": "CUSR0000SAR", "edcomm": "CUSR0000SAE", "hhfurnish": "CUSR0000SAH3",
    "personalcare": "CUSR0000SAG", "tobacco": "CUSR0000SEGA",
}
PPI_SERIES = {
    "ppiHeadline": "WPSFD4", "ppiCore": "WPSFD49104", "ppiCoreLessTr": "WPSFD49116",
    "ppiGoods": "WPSFD41", "ppiFdFoods": "WPSFD411", "ppiFdEnergy": "WPSFD412",
    "ppiGoodsLessFE": "WPSFD413", "ppiServices": "WPSFD42", "ppiTrade": "WPSFD423",
    "ppiTransp": "WPSFD422", "ppiSvcLess": "WPSFD421", "ppiConstr": "WPSFD43",
    "ppiIDProc": "WPSID61", "ppiIDUnproc": "WPSID62", "ppiIDSvc": "WPSID63",
    "ppiRubPlas": "WPU07", "ppiSynthRub": "WPU071102", "ppiTiresTub": "WPU0712",
    "ppiTires": "WPU07120104",
}

# Months of history to request. Dashboard keeps the last 25 (needs >=14).
HIST_YEARS = 3


class PullError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_post_json(url, payload, timeout=40, retries=1):
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "cpi-ppi-dashboard/1.0"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - we re-raise as PullError upstream
            last = e
            if attempt < retries:
                time.sleep(2 + attempt * 2)
    raise last


def _http_get_json(url, timeout=30, retries=1):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "cpi-ppi-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2 + attempt * 2)
    raise last


def fetch_bls_batch(series_map, key, codes):
    """Return {dashboard_key: [ {label,value,year,period}, ... ]} sorted ascending.

    `codes` = (request_err, status_err, no_series_err, no_headline_err).
    """
    req_err, status_err, no_series_err, _ = codes
    now = datetime.now(timezone.utc)
    payload = {
        "seriesid": list(series_map.values()),
        "startyear": str(now.year - HIST_YEARS),
        "endyear": str(now.year),
        "registrationkey": key,
        "catalog": False, "calculations": False, "annualaverage": False,
    }
    try:
        resp = _http_post_json(BLS_URL, payload)
    except Exception as e:  # noqa: BLE001
        raise PullError(req_err, "BLS request failed: %s" % e)

    if resp.get("status") != "REQUEST_SUCCEEDED":
        msgs = "; ".join(resp.get("message", []) or []) or "unknown BLS status"
        raise PullError(status_err, "BLS status %s: %s"
                        % (resp.get("status"), msgs))

    series_list = (resp.get("Results") or {}).get("series")
    if not series_list:
        raise PullError(no_series_err, "BLS response had no Results.series")

    by_id = {s.get("seriesID"): s for s in series_list}
    out = {}
    for dkey, sid in series_map.items():
        s = by_id.get(sid)
        if not s or not s.get("data"):
            continue
        rows = []
        for d in s["data"]:
            period = str(d.get("period", ""))
            if period == "M13":  # annual average — skip
                continue
            try:
                val = float(d.get("value"))
            except (TypeError, ValueError):
                continue
            year = int(d.get("year"))
            mon = MONTHS[int(period[1:])] if period.startswith("M") else ""
            rows.append({"label": "%s %d" % (mon, year), "value": val,
                         "year": year, "period": period})
        rows.sort(key=lambda x: (x["year"], int(x["period"][1:])))
        if rows:
            out[dkey] = rows[-25:]  # last 25 months
    return out


def fetch_fred(key):
    """Return ([{label,rate}], None) on success, or ([], code) on a soft failure."""
    if not key:
        return [], 102
    url = ("%s?series_id=FEDFUNDS&file_type=json&sort_order=desc&limit=13&api_key=%s"
           % (FRED_URL, key))
    try:
        resp = _http_get_json(url)
    except Exception as e:  # noqa: BLE001
        print("WARN FRED request failed: %s" % e, file=sys.stderr)
        return [], 401
    obs = [o for o in resp.get("observations", []) if o.get("value") not in (None, ".")]
    if not obs:
        return [], 402
    arr = []
    for o in obs:
        try:
            dt = datetime.strptime(o["date"], "%Y-%m-%d")
            arr.append({"label": "%s %d" % (MONTHS[dt.month], dt.year),
                        "rate": float(o["value"])})
        except (KeyError, ValueError):
            continue
    arr.reverse()  # ascending
    return arr, None


def report_month_from(cpi):
    head = cpi.get("headline")
    if head:
        return head[-1]["label"]
    return ""


def load_last_good():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def write_json(obj):
    try:
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, os.path.abspath(OUT_PATH))
    except Exception as e:  # noqa: BLE001
        raise PullError(501, "Could not write data.json: %s" % e)


def write_failure(code, stage, message):
    """Preserve last-good payload (if any) and flag the failure loudly."""
    prev = load_last_good() or {}
    payload = {
        "schema": 1,
        "status": {"ok": False, "error_code": code, "stage": stage,
                   "message": message, "generated_at": _now_iso(),
                   "fred_error_code": (prev.get("status") or {}).get("fred_error_code")},
        "report_month": prev.get("report_month", ""),
        "ppi_month": prev.get("ppi_month", ""),
        "cpi": prev.get("cpi", {}),
        "ppi": prev.get("ppi", {}),
        "fed_funds": prev.get("fed_funds"),
    }
    if "commentary" in prev:
        payload["commentary"] = prev["commentary"]
    try:
        write_json(payload)
    except PullError as e:
        print("ERROR E%d %s" % (e.code, e.message), file=sys.stderr)
    print("DATA PULL FAILED  E%d  (stage=%s)  %s" % (code, stage, message),
          file=sys.stderr)


def main():
    bls_key = os.environ.get("BLS_API_KEY", "").strip()
    if not bls_key:
        write_failure(101, "config", "BLS_API_KEY environment variable is not set")
        return 101

    try:
        cpi = fetch_bls_batch(CPI_SERIES, bls_key, (201, 202, 203, 204))
        if "headline" not in cpi:
            raise PullError(204, "CPI headline (CUSR0000SA0) not returned by BLS")

        ppi = fetch_bls_batch(PPI_SERIES, bls_key, (301, 302, 303, 304))
        if "ppiHeadline" not in ppi:
            raise PullError(304, "PPI headline (WPSFD4) not returned by BLS")
    except PullError as e:
        stage = "cpi" if e.code < 300 else "ppi"
        write_failure(e.code, stage, e.message)
        return e.code
    except Exception as e:  # noqa: BLE001
        write_failure(502, "bls", "Unexpected: %s" % e)
        return 502

    # FRED is optional — a soft failure never blocks CPI/PPI.
    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    fed_funds, fred_code = fetch_fred(fred_key)

    cpi_month = report_month_from(cpi)
    ppi_month = ppi.get("ppiHeadline", [{}])[-1].get("label", cpi_month)

    payload = {
        "schema": 1,
        "status": {"ok": True, "error_code": 0, "stage": "complete", "message": "",
                   "generated_at": _now_iso(), "fred_error_code": fred_code},
        "report_month": cpi_month,
        "ppi_month": ppi_month,
        "cpi": cpi,
        "ppi": ppi,
        "fed_funds": fed_funds or None,
    }

    # Preserve any baked-in commentary from a previous run.
    prev = load_last_good()
    if prev and "commentary" in prev:
        payload["commentary"] = prev["commentary"]

    try:
        write_json(payload)
    except PullError as e:
        write_failure(e.code, "write", e.message)
        return e.code

    print("OK  CPI=%s  PPI=%s  series(cpi=%d, ppi=%d)  fed_funds=%d  fred_code=%s"
          % (cpi_month, ppi_month, len(cpi), len(ppi),
             len(fed_funds), fred_code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
