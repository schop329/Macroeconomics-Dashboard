#!/usr/bin/env python3
"""
generate_commentary.py  —  OPTIONAL, failure-isolated commentary step.

Runs AFTER fetch_data.py in the GitHub Action. Reads the freshly written
data.json, asks the Anthropic API to write the same short analysis blurbs the
dashboard used to generate in-browser, and bakes them into data.json under a
`commentary` object keyed by slot id (hc2, mfg2, transp2, fed2, lens2, tire2,
ppi2, ppilens2). The dashboard already renders data.commentary[<key>].

It is deliberately best-effort and NON-FATAL:
  * It only runs if data.json status.ok is true (never narrates failed data).
  * Every API call is wrapped; one slot failing never stops the others.
  * It ALWAYS exits 0 so it can never turn the data pull red. Problems are
    written to data.json status.commentary_error_code and printed as GitHub
    ::warning:: annotations.

COMMENTARY ERROR CODES (all non-fatal):
  701  ANTHROPIC_API_KEY not set            -> commentary skipped
  702  every commentary call failed
  703  data.json missing/invalid, or the data pull itself failed -> skipped
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "..", "data.json")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("COMMENTARY_MODEL", "claude-sonnet-4-6")
ANTHROPIC_VERSION = "2023-06-01"

BULLET = "\u2022"  # •
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Display names (must match SERIES / PPI_SERIES in index.html for faithful prompts)
CPI_NAMES = {
    "headline": "All Items (Headline)", "core": "Core (ex Food & Energy)",
    "energy": "Energy (All)", "gasoline": "Gasoline", "electricity": "Electricity",
    "natgas": "Natural Gas (Piped)", "fueloil": "Fuel Oil", "food": "Food (All)",
    "foodhome": "Food at Home", "foodaway": "Food Away from Home", "shelter": "Shelter",
    "oer": "Owners' Equiv. Rent", "rent": "Rent of Primary Res.", "newveh": "New Vehicles",
    "usedveh": "Used Cars & Trucks", "airline": "Airline Fares",
    "mvins": "Motor Vehicle Insurance", "carrental": "Car & Truck Rental",
    "mvmaint": "MV Maint. & Repair", "medcare": "Medical Care", "apparel": "Apparel",
    "recreation": "Recreation", "edcomm": "Educ. & Communication",
    "hhfurnish": "HH Furnishings & Ops", "personalcare": "Personal Care",
    "tobacco": "Tobacco Products",
}
PPI_NAMES = {
    "ppiHeadline": "Final Demand (Headline PPI)", "ppiCore": "Core (less Foods & Energy)",
    "ppiCoreLessTr": "Core (less Foods, Energy, Trade)", "ppiGoods": "Final Demand Goods",
    "ppiFdFoods": "FD Foods", "ppiFdEnergy": "FD Energy",
    "ppiGoodsLessFE": "FD Goods less Foods & Energy", "ppiServices": "Final Demand Services",
    "ppiTrade": "FD Trade Services", "ppiTransp": "FD Transportation & Warehousing",
    "ppiSvcLess": "FD Services less Trade/Transp.", "ppiConstr": "Final Demand Construction",
    "ppiIDProc": "Processed Goods for Int. Demand",
    "ppiIDUnproc": "Unprocessed Goods for Int. Demand",
    "ppiIDSvc": "Services for Intermediate Demand", "ppiRubPlas": "Rubber & Plastic Products",
    "ppiSynthRub": "Synthetic Rubber", "ppiTiresTub": "Tires & Tubes",
    "ppiTires": "Passenger Car Tires",
}
PPI_NSA = {"ppiRubPlas", "ppiSynthRub", "ppiTiresTub", "ppiTires"}
PPI_DEEP_DIVE = ["ppiSynthRub", "ppiTires", "ppiTiresTub", "ppiRubPlas"]
SNAPSHOT_PICK = ["headline", "core", "energy", "gasoline", "food", "foodhome",
                 "shelter", "electricity", "natgas", "newveh", "usedveh",
                 "mvins", "airline", "mvmaint"]

NEUTRAL_CTX = ('Audience: an informed reader of an economics briefing. Write in neutral, '
               'matter-of-fact language so the reader can draw their own conclusions. Plain '
               'business English, no preamble, never restate the question. State what the data '
               'shows; do not give recommendations or editorialize in the main text. After the '
               'main text, end with exactly ONE bullet that begins "%s Tire industry:" noting '
               'the single most relevant implication for a tire & transportation-equipment '
               'manufacturer.' % BULLET)


def warn(msg):
    print("::warning::commentary: %s" % msg)
    print("WARN commentary: %s" % msg, file=sys.stderr)


def fmt_pct(v):
    return ("+" if v >= 0 else "") + ("%.2f" % v) + "%"


def calc_changes(arr):
    n = len(arr)
    if n < 14:
        return None
    latest, prior_mo, prior_yr = arr[n - 1], arr[n - 2], arr[n - 13]
    return {
        "mom": (latest["value"] - prior_mo["value"]) / prior_mo["value"] * 100,
        "yoy": (latest["value"] - prior_yr["value"]) / prior_yr["value"] * 100,
        "current": latest["value"], "label": latest["label"],
    }


def changes_for(store):
    out = {}
    for k, arr in (store or {}).items():
        if isinstance(arr, list) and len(arr) >= 14:
            ch = calc_changes(arr)
            if ch:
                out[k] = ch
    return out


def g(ch, k):  # safe YoY lookup
    return fmt_pct(ch[k]["yoy"]) if k in ch else "n/a"


def build_snapshot(ch):
    parts = [CPI_NAMES.get(k, k) + " " + fmt_pct(ch[k]["yoy"]) + " YoY / " + fmt_pct(ch[k]["mom"]) + " MoM"
             for k in SNAPSHOT_PICK if k in ch]
    return "; ".join(parts) + "."


def build_ppi_snapshot(pch):
    parts = []
    for k in pch:
        nsa = " (NSA)" if k in PPI_NSA else ""
        parts.append(PPI_NAMES.get(k, k) + " " + fmt_pct(pch[k]["yoy"]) + " YoY / " + fmt_pct(pch[k]["mom"]) + " MoM" + nsa)
    return "; ".join(parts) + "."


def build_prompts(ch, pch, cpi_month, ppi_month):
    """Return ordered list of (cacheKey, system, user, max_tokens)."""
    snap = build_snapshot(ch)
    ppi_snap = build_ppi_snapshot(pch)
    h_yoy = ch["headline"]["yoy"] if "headline" in ch else 0.0
    h_mom = fmt_pct(ch["headline"]["mom"]) if "headline" in ch else "n/a"
    diff = h_yoy - 2
    fed_user = ("Headline CPI %s YoY (%s%.2fpp vs 2%% target), core %s, headline MoM %s. "
                "Read the policy signal factually."
                % (fmt_pct(h_yoy), "+" if diff >= 0 else "", diff, g(ch, "core"), h_mom))
    deep = "; ".join(PPI_NAMES.get(k, k) + " " + fmt_pct(pch[k]["yoy"]) + " YoY"
                     for k in PPI_DEEP_DIVE if k in pch)
    return [
        ("hc2",
         "You are a macro analyst. " + NEUTRAL_CTX + " Lead with ONE plain sentence on the headline-and-core read, then at most 2 short factual follow-on points, then the single tire bullet.",
         "CPI for %s. %s Summarize the headline-and-core picture factually." % (cpi_month, snap), 260),
        ("mfg2",
         "You are a cost analyst. " + NEUTRAL_CTX + " At most 3 factual sentences on what these components show, then the single tire bullet.",
         "Energy & goods cost components for %s: Electricity %s, Natural Gas %s, Food at Home %s, HH Furnishings %s (all YoY). Describe the movement factually."
         % (cpi_month, g(ch, "electricity"), g(ch, "natgas"), g(ch, "foodhome"), g(ch, "hhfurnish")), 220),
        ("transp2",
         "You are a logistics cost analyst. " + NEUTRAL_CTX + " At most 3 factual sentences, then the single tire bullet.",
         "Transportation cost components for %s: Gasoline %s, Fuel Oil %s, Airline Fares %s, MV Insurance %s (YoY). Describe the movement factually."
         % (cpi_month, g(ch, "gasoline"), g(ch, "fueloil"), g(ch, "airline"), g(ch, "mvins")), 220),
        ("fed2",
         "You are a Fed-policy analyst. " + NEUTRAL_CTX + " At most 3 factual sentences on what the data implies for Fed posture and rate direction; no politics, no predictions stated as certainty. Then the single tire bullet.",
         fed_user, 220),
        ("lens2",
         "You are an economics analyst. " + NEUTRAL_CTX + ' Use at most 3 factual bullets (start each with %s) describing where consumer-side price pressure is landing, then the single "%s Tire industry:" bullet last.' % (BULLET, BULLET),
         "CPI snapshot for %s. %s Summarize where price pressure is concentrated." % (cpi_month, snap), 360),
        ("tire2",
         "You are an automotive-sector analyst. " + NEUTRAL_CTX + ' Describe the auto/tire CPI channel factually in at most 3 bullets (start each with %s), then the single "%s Tire industry:" bullet last.' % (BULLET, BULLET),
         "Auto & tire CPI channel for %s: MV Maint %s, New Vehicles %s, Used Vehicles %s, MV Insurance %s (YoY). Read the channel factually."
         % (cpi_month, g(ch, "mvmaint"), g(ch, "newveh"), g(ch, "usedveh"), g(ch, "mvins")), 320),
        ("ppi2",
         "You are a producer-price analyst. " + NEUTRAL_CTX + " Lead with ONE plain sentence on final demand, then at most 2 factual points (cover goods vs services and, where relevant, the upstream intermediate-demand signal). Note that tire-input commodity series are NSA, so cite them YoY. End with the single tire bullet.",
         "PPI for %s. %s Summarize the producer-price picture factually, including the intermediate-demand (pipeline) read." % (ppi_month, ppi_snap), 300),
        ("ppilens2",
         "You are a raw-materials analyst. " + NEUTRAL_CTX + ' Describe the tire-input cost stack factually in at most 3 bullets (start each with %s); cite NSA tire inputs on a YoY basis. End with the single "%s Tire industry:" bullet.' % (BULLET, BULLET),
         "PPI tire-input cost stack for %s: %s. Describe where rubber, tire and plastics input costs sit factually." % (ppi_month, deep), 360),
    ]


def call_anthropic(api_key, system, user, max_tokens):
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8"))
    parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise ValueError("empty completion")
    return text


def load_data():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_data(d):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, os.path.abspath(DATA_PATH))


def set_code(d, code):
    d.setdefault("status", {})["commentary_error_code"] = code
    try:
        save_data(d)
    except Exception as e:  # noqa: BLE001
        warn("could not write status: %s" % e)


def main():
    try:
        d = load_data()
    except Exception as e:  # noqa: BLE001
        warn("data.json missing/invalid (%s) -> skipping commentary (703)" % e)
        return 0

    if not (d.get("status") or {}).get("ok"):
        warn("data pull was not ok -> skipping commentary (703)")
        set_code(d, 703)
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        warn("ANTHROPIC_API_KEY not set -> skipping commentary (701)")
        set_code(d, 701)
        return 0

    ch = changes_for(d.get("cpi", {}))
    pch = changes_for(d.get("ppi", {}))
    if "headline" not in ch:
        warn("no CPI headline changes -> skipping commentary (703)")
        set_code(d, 703)
        return 0

    prompts = build_prompts(ch, pch, d.get("report_month", ""), d.get("ppi_month", ""))
    commentary = {}
    ok = 0
    for key, system, user, max_tokens in prompts:
        for attempt in range(2):  # one retry
            try:
                commentary[key] = call_anthropic(api_key, system, user, max_tokens)
                ok += 1
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    time.sleep(3)
                    continue
                warn("slot %s failed: %s" % (key, e))

    if ok == 0:
        warn("all commentary calls failed (702)")
        set_code(d, 702)
        return 0

    d["commentary"] = commentary
    d.setdefault("status", {})["commentary_error_code"] = None
    d["status"]["commentary_generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        save_data(d)
    except Exception as e:  # noqa: BLE001
        warn("could not write commentary: %s" % e)
        return 0

    print("OK commentary: %d/%d slots written -> %s"
          % (ok, len(prompts), ", ".join(commentary.keys())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
