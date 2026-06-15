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
  102  FRED_API_KEY missing            (non-fatal -> ALL FRED sections unavailable:
                                       fred_/gdp_/payrolls_/sentiment_error_code = 102)
  401  Fed Funds request failed        (non-fatal -> status.fred_error_code)
  402  Fed Funds returned no observations (non-fatal -> status.fred_error_code)
  411  GDP request failed              (non-fatal -> status.gdp_error_code)
  412  GDP returned no usable observations (non-fatal -> status.gdp_error_code)
  421  Payrolls request failed         (non-fatal -> status.payrolls_error_code)
  422  Payrolls returned no usable observations (non-fatal -> status.payrolls_error_code)
  431  Sentiment request failed        (non-fatal -> status.sentiment_error_code)
  432  Sentiment returned no usable observations (non-fatal -> status.sentiment_error_code)
  501  Could not write data.json
  502  Unexpected exception

FRED-family note: every section above is pulled server-side from FRED with the
SAME key and is NON-FATAL — a failure in any one of them sets only its own
*_error_code and leaves CPI/PPI (and the other FRED sections) intact. GDP is
QUARTERLY (labels like "Q1 2025"); payrolls/sentiment are MONTHLY. The headline
series gatekeeps each section: GDP growth (A191RL1Q225SBEA), PAYEMS, UMCSENT.
Secondary series (GDP level, UNRATE, AHE, participation) failing is non-fatal and
simply yields an empty array, so a section still renders its headline.
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

# ── FRED series for the expansion sections (all same key, all pulled here) ───
# GDP (QUARTERLY): headline = annualized real GDP growth; level = chained-$ context.
FRED_GDP_GROWTH = "A191RL1Q225SBEA"   # Real GDP, % change, annualized — the news number
FRED_GDP_LEVEL = "GDPC1"              # Real GDP level, chained 2017 $ (context)
# Nonfarm Payrolls (MONTHLY, SA)
FRED_PAYROLLS = "PAYEMS"              # Total nonfarm employment, thousands (headline)
FRED_UNRATE = "UNRATE"               # Unemployment rate, %
FRED_AHE = "CES0500000003"           # Avg hourly earnings, total private $ (optional)
FRED_PARTRATE = "CIVPART"            # Labor force participation rate, % (optional)
# Consumer Sentiment (MONTHLY). FRED publishes UMCSENT with a ~1-month lag at the
# source's request, so this legitimately trails the other series — not a bug.
FRED_UMCSENT = "UMCSENT"             # UMich Consumer Sentiment index (headline)

# How many observations to request per FRED series.
FRED_MONTHLY_LIMIT = 25   # mirrors the BLS "last 25 months" (calcChanges needs >=14)
FRED_QUARTERLY_LIMIT = 12  # 3 years of quarters for the GDP trend + YoY (n-4)


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


def _fred_label(dt, freq):
    """Format a FRED observation date as a dashboard label.
    freq "M" -> "May 2025" (monthly) ; "Q" -> "Q1 2025" (quarterly).
    FRED dates quarters by the first month of the quarter (Q1=Jan, Q2=Apr, ...)."""
    if freq == "Q":
        return "Q%d %d" % ((dt.month - 1) // 3 + 1, dt.year)
    return "%s %d" % (MONTHS[dt.month], dt.year)


def fetch_fred_series(series_id, key, limit, freq="M"):
    """Pull ONE FRED series server-side. Generalized from the old single-series
    Fed-Funds call so every expansion section reuses the same path.

    Returns (arr, reason):
      arr    -> [{"label","value"}] ascending; [] on any soft failure.
      reason -> None on success, "request" on network/HTTP/timeout failure,
                "empty" when FRED returns no usable observations.
    The caller maps the reason string to its section-specific numeric code, so
    each failure mode stays one debuggable number. NEVER raises and NEVER
    fabricates: a failed pull yields [] + a reason, not estimated data.
    """
    url = ("%s?series_id=%s&file_type=json&sort_order=desc&limit=%d&api_key=%s"
           % (FRED_URL, series_id, limit, key or ""))
    try:
        resp = _http_get_json(url)
    except Exception as e:  # noqa: BLE001
        print("WARN FRED %s request failed: %s" % (series_id, e), file=sys.stderr)
        return [], "request"
    obs = [o for o in resp.get("observations", []) if o.get("value") not in (None, ".")]
    if not obs:
        return [], "empty"
    arr = []
    for o in obs:
        try:
            dt = datetime.strptime(o["date"], "%Y-%m-%d")
            arr.append({"label": _fred_label(dt, freq), "value": float(o["value"])})
        except (KeyError, ValueError):
            continue
    if not arr:
        return [], "empty"
    arr.reverse()  # FRED returns newest-first; the dashboard wants ascending
    return arr, None


def fetch_fred_fedfunds(key):
    """Fed Funds rate. Preserves the established {label, rate} client contract
    (index.html reads RAW.fed_funds[].rate) and the existing 102/401/402 codes.
    Returns ([{label,rate}], None) on success or ([], code) on a soft failure."""
    if not key:
        return [], 102
    arr, reason = fetch_fred_series("FEDFUNDS", key, 13, "M")
    if reason == "request":
        return [], 401
    if reason == "empty" or not arr:
        return [], 402
    # remap value -> rate so the existing front-end keeps working unchanged
    return [{"label": a["label"], "rate": a["value"]} for a in arr], None


def fetch_gdp(key):
    """GDP block (QUARTERLY). Returns (block, code, quarter_label).
      block = {"growth":[...], "level":[...]} ; code None on success else 41x/102.
    Headline = annualized real GDP growth (gatekeeper). The level series is
    context only, so its failure is non-fatal and just leaves level=[]. Soft-fails
    (returns {} + code) rather than raising, so GDP can be down without touching
    CPI/PPI or the other FRED sections."""
    if not key:
        return {}, 102, ""
    growth, reason = fetch_fred_series(FRED_GDP_GROWTH, key, FRED_QUARTERLY_LIMIT, "Q")
    if reason == "request":
        return {}, 411, ""
    if reason == "empty" or not growth:
        return {}, 412, ""
    level, _lr = fetch_fred_series(FRED_GDP_LEVEL, key, FRED_QUARTERLY_LIMIT, "Q")  # context
    return {"growth": growth, "level": level}, None, growth[-1]["label"]


def fetch_payrolls(key):
    """Nonfarm Payrolls block (MONTHLY). Returns (block, code, month_label).
      block = {"nonfarm":[...], "unrate":[...], "ahe":[...], "partrate":[...]}.
    Headline = PAYEMS (gatekeeper); the front-end derives jobs-added as the MoM
    change. UNRATE + the optional AHE/participation series are non-fatal extras
    (empty array if they fail). Soft-fails rather than raising."""
    if not key:
        return {}, 102, ""
    nonfarm, reason = fetch_fred_series(FRED_PAYROLLS, key, FRED_MONTHLY_LIMIT, "M")
    if reason == "request":
        return {}, 421, ""
    if reason == "empty" or not nonfarm:
        return {}, 422, ""
    unrate, _u = fetch_fred_series(FRED_UNRATE, key, FRED_MONTHLY_LIMIT, "M")
    ahe, _a = fetch_fred_series(FRED_AHE, key, FRED_MONTHLY_LIMIT, "M")          # optional
    partrate, _p = fetch_fred_series(FRED_PARTRATE, key, FRED_MONTHLY_LIMIT, "M")  # optional
    return ({"nonfarm": nonfarm, "unrate": unrate, "ahe": ahe, "partrate": partrate},
            None, nonfarm[-1]["label"])


def fetch_sentiment(key):
    """Consumer Sentiment block (MONTHLY). Returns (block, code, month_label).
      block = {"umich":[...]}.
    Headline = UMCSENT. NOTE: FRED lags this ~1 month at UMich's request, so it
    legitimately trails the other monthly series (the UI notes it). Soft-fails
    rather than raising."""
    if not key:
        return {}, 102, ""
    umich, reason = fetch_fred_series(FRED_UMCSENT, key, FRED_MONTHLY_LIMIT, "M")
    if reason == "request":
        return {}, 431, ""
    if reason == "empty" or not umich:
        return {}, 432, ""
    return {"umich": umich}, None, umich[-1]["label"]


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
    prev_status = prev.get("status") or {}
    payload = {
        "schema": 1,
        "status": {"ok": False, "error_code": code, "stage": stage,
                   "message": message, "generated_at": _now_iso(),
                   "fred_error_code": prev_status.get("fred_error_code"),
                   "gdp_error_code": prev_status.get("gdp_error_code"),
                   "payrolls_error_code": prev_status.get("payrolls_error_code"),
                   "sentiment_error_code": prev_status.get("sentiment_error_code")},
        "report_month": prev.get("report_month", ""),
        "ppi_month": prev.get("ppi_month", ""),
        "cpi": prev.get("cpi", {}),
        "ppi": prev.get("ppi", {}),
        "fed_funds": prev.get("fed_funds"),
        # Carry the FRED-section blocks forward too, so a BLS failure leaves the
        # GDP / Payrolls / Sentiment tabs on their last-good data behind the same
        # non-blocking failure banner CPI/PPI use.
        "gdp": prev.get("gdp"),
        "gdp_quarter": prev.get("gdp_quarter", ""),
        "payrolls": prev.get("payrolls"),
        "payrolls_month": prev.get("payrolls_month", ""),
        "sentiment": prev.get("sentiment"),
        "sentiment_month": prev.get("sentiment_month", ""),
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

    # FRED is optional — every FRED section soft-fails independently and never
    # blocks CPI/PPI. One shared key drives Fed Funds + the three new sections.
    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    fed_funds, fred_code = fetch_fred_fedfunds(fred_key)
    gdp_block, gdp_code, gdp_quarter = fetch_gdp(fred_key)
    payrolls_block, payrolls_code, payrolls_month = fetch_payrolls(fred_key)
    sentiment_block, sentiment_code, sentiment_month = fetch_sentiment(fred_key)

    cpi_month = report_month_from(cpi)
    ppi_month = ppi.get("ppiHeadline", [{}])[-1].get("label", cpi_month)

    payload = {
        "schema": 1,
        "status": {"ok": True, "error_code": 0, "stage": "complete", "message": "",
                   "generated_at": _now_iso(), "fred_error_code": fred_code,
                   "gdp_error_code": gdp_code,
                   "payrolls_error_code": payrolls_code,
                   "sentiment_error_code": sentiment_code},
        "report_month": cpi_month,
        "ppi_month": ppi_month,
        "cpi": cpi,
        "ppi": ppi,
        "fed_funds": fed_funds or None,
        # Expansion sections. A failed section is None + a numbered code (no stale
        # data); the front-end shows "Data Pull Failed (E4xx)" for that tab only.
        "gdp": gdp_block or None,
        "gdp_quarter": gdp_quarter,
        "payrolls": payrolls_block or None,
        "payrolls_month": payrolls_month,
        "sentiment": sentiment_block or None,
        "sentiment_month": sentiment_month,
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

    print("OK  CPI=%s  PPI=%s  series(cpi=%d, ppi=%d)  fed_funds=%d  fred_code=%s\n"
          "    gdp=%s(E%s)  payrolls=%s(E%s)  sentiment=%s(E%s)"
          % (cpi_month, ppi_month, len(cpi), len(ppi),
             len(fed_funds), fred_code,
             gdp_quarter or "-", gdp_code,
             payrolls_month or "-", payrolls_code,
             sentiment_month or "-", sentiment_code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
