#!/usr/bin/env python3
"""Write data/yields.json from the public BlackRock iBonds ladder feed.

  python3 refresh.py
  python3 refresh.py --json table.jsn
  python3 refresh.py --no-fetch --xls ena-tips.xlsx
  python3 refresh.py --pjtxx 3.55 --pjtxx-asof 2026-10-21

One GET, no login, no cookies, no client data. PJTXX is not in the feed; the
value already in data/yields.json is kept unless --pjtxx is passed.

ENAY matches the BlackRock iBonds tool (previous close, net expense ratio):
    ENAY = (NAV - Price) / (EffDuration * NAV) * 100 + Yield - NetExpenseRatio
Price and NAV are rounded to cents. Yield is yield to worst for corporate,
Treasury, and muni, and weighted average YTM for TIPS.

Series: TIPS IBIx (Oct), Corporate IBDx/IBCx (Dec 15), Treasury IBTx (Dec 15),
Muni IBMx (Dec 1). Long-dated Treasury IBGx and high yield are ignored.
Muni ENAY is the tax-exempt figure, not tax-equivalent.
TIPS real yield is the feed's yieldToWorst.
YTM is weightedAvgYieldToMaturity, or yieldToWorst when the feed omits it.

The file is replaced only after the result validates (non-empty fund lists,
known tickers, yields in range, asOf a real date). A bad fetch exits 2 and
leaves the previous file in place.
"""
import argparse
import datetime
import json
import math
import os
import re
import sys
import urllib.request

def cents(v):
    """Round half up, matching JavaScript Math.round (not banker's rounding)."""
    return math.floor(float(v) * 100 + 0.5) / 100

HERE = os.path.dirname(os.path.abspath(__file__))
YJ = os.path.join(HERE, "data", "yields.json")
BLK_URL = (
    "https://www.blackrock.com/us/financial-professionals/bonds-ladder/table.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/financial-professionals/ibonds"
)
BLK_TOOL = "https://www.blackrock.com/us/financial-professionals/tools/ibonds"
MAX_FEED_BYTES = 20 * 1024 * 1024
MIN_FUNDS = 5
MAX_FUNDS = 80
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}

SERIES = ("corporate", "tips", "treasury", "muni")
FEED_TYPE = {"tips": "Tips", "corporate": "Corporate", "treasury": "Treasury", "muni": "Municipal"}
TICKER_RE = {
    "tips": re.compile(r"IBI[A-Z]\Z"),
    "corporate": re.compile(r"IB[DC][A-Z]\Z"),
    "treasury": re.compile(r"IBT[A-Z]\Z"),
    "muni": re.compile(r"IBM[A-Z]\Z"),
}
TOP_KEYS = {"asOf", "notes", "pjtxx", "corporate", "tips", "treasury", "muni"}
PJ_KEYS = {"ticker", "name", "yield", "asOf", "yieldBasis"}
SERIES_KEYS = {"enayAsOf", "funds"}
TIPS_KEYS = {"enayAsOf", "realYieldAsOf", "funds"}
FUND_KEYS = {"ticker", "maturity", "name", "prevClose", "ytm", "enay"}
TIPS_FUND_KEYS = FUND_KEYS | {"realYield"}

NOTES = (
    "ENAY = BlackRock iBonds ladder tool Estimated Net Acquisition Yield (previous-close basis, net expense ratio), "
    "computed from the tool's data feed with the tool's formula. TIPS realYield = yield to worst (real) from the same feed. "
    "Treasury/muni/corporate ytm = weighted avg YTM (yield to worst where the feed omits it). Muni ENAY is tax-exempt, not tax-equivalent."
)


class RefreshError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def fail(msg, code=2):
    print("REFRESH FAILED: " + msg, file=sys.stderr)
    print(
        "  Nothing was written. Manual fallback: open " + BLK_TOOL
        + ", download the ENAY table XLS (or save " + BLK_URL + ") and run "
        "refresh.py --xls or --json, or use Import in ladder.html.",
        file=sys.stderr,
    )
    raise RefreshError(msg, code)


def series_of(ticker):
    if TICKER_RE["tips"].fullmatch(ticker):
        return "tips"
    if TICKER_RE["corporate"].fullmatch(ticker):
        return "corporate"
    if TICKER_RE["treasury"].fullmatch(ticker):
        return "treasury"
    if TICKER_RE["muni"].fullmatch(ticker):
        return "muni"
    return None


def download_feed():
    req = urllib.request.Request(BLK_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(MAX_FEED_BYTES + 1)
            status = getattr(resp, "status", 200)
            if status != 200:
                fail(f"BlackRock returned HTTP {status}")
    except RefreshError:
        raise
    except Exception as e:
        fail(f"could not download BlackRock data ({e.__class__.__name__}: {e})")
    if len(raw) > MAX_FEED_BYTES:
        fail("BlackRock response too large")
    try:
        return json.loads(raw)
    except Exception:
        fail("BlackRock response was not JSON (site or endpoint changed?) - first bytes: "
             + raw[:80].decode("latin-1", "replace"))


def enay_of(x):
    """BlackRock iBonds tool: (nav-price)/(dur*nav)*100 + yield - net expense ratio."""
    price = cents(x["closingPrice"])
    nav = cents(x["navAmount"])
    yld = x["weightedAvgYieldToMaturity"] if "Tips" in (x.get("types") or []) else x["yieldToWorst"]
    return (nav - price) / (float(x["effectiveDuration"]) * nav) * 100 + float(yld) - float(x["netr"])


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _yield_ok(v):
    return _num(v) and -2 < float(v) < 15


def _price_ok(v):
    return _num(v) and 0 < float(v) < 10000


def _text_ok(v, lo, hi):
    if not isinstance(v, str) or not (lo <= len(v) <= hi):
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 or ch in "<>" for ch in v)


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    if day.year < 2020 or day.year > 2100:
        return None
    return day


def validate_doc(y, now=None):
    """Reject a document that must not replace a known-good yields file."""
    now = now or datetime.date.today()
    if not isinstance(y, dict) or set(y) != TOP_KEYS:
        fail("yield document has missing or unexpected fields")
    as_of = parse_date(y.get("asOf"))
    if as_of is None:
        fail("asOf is not a real YYYY-MM-DD date")
    if as_of > now + datetime.timedelta(days=2):
        fail(f"asOf {y['asOf']} is in the future")
    if not _text_ok(y.get("notes"), 0, 4000):
        fail("notes must be plain text")
    pj = y.get("pjtxx")
    if not isinstance(pj, dict) or set(pj) != PJ_KEYS or pj.get("ticker") != "PJTXX":
        fail("pjtxx is missing or has unexpected fields")
    if not _text_ok(pj.get("name"), 1, 160) or not _text_ok(pj.get("yieldBasis"), 1, 120):
        fail("pjtxx name or yield basis is not plain text")
    if not _yield_ok(pj.get("yield")):
        fail(f"pjtxx yield {pj.get('yield')!r} is implausible")
    pj_asof = parse_date(pj.get("asOf"))
    if pj_asof is None:
        fail("pjtxx asOf is not a real YYYY-MM-DD date")
    if pj_asof > now + datetime.timedelta(days=2):
        fail(f"pjtxx asOf {pj['asOf']} is in the future")
    for series in SERIES:
        block = y.get(series)
        expect = TIPS_KEYS if series == "tips" else SERIES_KEYS
        if not isinstance(block, dict) or set(block) != expect:
            fail(f"{series} is missing or has unexpected fields")
        enay_asof = parse_date(block.get("enayAsOf"))
        if enay_asof is None:
            fail(f"{series} enayAsOf is not a real date")
        if enay_asof > as_of:
            fail(f"{series} enayAsOf is after asOf")
        if series == "tips":
            real_asof = parse_date(block.get("realYieldAsOf"))
            if real_asof is None:
                fail("tips realYieldAsOf is not a real date")
            if real_asof > as_of:
                fail("tips realYieldAsOf is after asOf")
        funds = block.get("funds")
        if not isinstance(funds, list) or not (MIN_FUNDS <= len(funds) <= MAX_FUNDS):
            fail(f"{series} needs {MIN_FUNDS}–{MAX_FUNDS} funds (found {0 if not isinstance(funds, list) else len(funds)})")
        seen_t, seen_m = set(), set()
        fund_keys = TIPS_FUND_KEYS if series == "tips" else FUND_KEYS
        for i, fund in enumerate(funds):
            if not isinstance(fund, dict) or set(fund) != fund_keys:
                fail(f"{series} fund {i} has missing or unexpected fields")
            ticker = fund.get("ticker")
            if not isinstance(ticker, str) or not TICKER_RE[series].fullmatch(ticker):
                fail(f"{series} fund {i} ticker {ticker!r} is not a known iBonds ticker")
            if ticker in seen_t:
                fail(f"duplicate ticker {ticker}")
            seen_t.add(ticker)
            mat = fund.get("maturity")
            if isinstance(mat, bool) or not isinstance(mat, int) or not (2020 <= mat <= 2100):
                fail(f"{ticker} maturity {mat!r} is not a year")
            if mat in seen_m:
                fail(f"{series} duplicate maturity {mat}")
            seen_m.add(mat)
            if not _text_ok(fund.get("name"), 1, 200):
                fail(f"{ticker} name is empty or not plain text")
            if not _price_ok(fund.get("prevClose")):
                fail(f"{ticker} prevClose {fund.get('prevClose')!r} is implausible")
            for key in ("ytm", "enay"):
                if not _yield_ok(fund.get(key)):
                    fail(f"{ticker} {key} {fund.get(key)!r} is implausible")
            if series == "tips" and not _yield_ok(fund.get("realYield")):
                fail(f"{ticker} realYield {fund.get('realYield')!r} is implausible")
    return y


def _clean_name(value):
    name = str(value or "").replace("\u00ae", "")
    name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127 and ch not in "<>")
    return name.strip()


def _pack_fund(fund, series):
    packed = {
        "ticker": fund["ticker"],
        "maturity": int(fund["maturity"]),
        "name": fund["name"],
        "prevClose": fund["prevClose"],
        "ytm": fund["ytm"],
        "enay": fund["enay"],
    }
    if series == "tips":
        packed["realYield"] = fund["realYield"]
    return packed


def canonicalize(y):
    """Copy only the published fields, in a stable order."""
    out = {
        "asOf": y["asOf"],
        "notes": y.get("notes") or NOTES,
        "pjtxx": {
            "ticker": "PJTXX",
            "name": y["pjtxx"]["name"],
            "yield": y["pjtxx"]["yield"],
            "asOf": y["pjtxx"]["asOf"],
            "yieldBasis": y["pjtxx"]["yieldBasis"],
        },
    }
    for series in SERIES:
        block = y[series]
        packed = {
            "enayAsOf": block.get("enayAsOf"),
            "funds": [_pack_fund(fund, series) for fund in sorted(block.get("funds") or [], key=lambda f: f.get("maturity") or 0)],
        }
        if series == "tips":
            packed = {
                "enayAsOf": block.get("enayAsOf"),
                "realYieldAsOf": block.get("realYieldAsOf"),
                "funds": packed["funds"],
            }
        out[series] = packed
    return out


def apply_feed(data, y):
    if not isinstance(data, list) or not data or not all(isinstance(v, dict) for v in data):
        fail("unexpected BlackRock data shape (expected a list of funds)")
    asofs, cnt, seen = {}, {s: 0 for s in SERIES}, {s: set() for s in SERIES}
    base_year = int(str(y.get("asOf") or "2026")[:4])
    for series in SERIES:
        y.setdefault(series, {"enayAsOf": None, "funds": []})
        if series == "tips":
            y[series].setdefault("realYieldAsOf", None)
    old = {fund["ticker"]: dict(fund) for series in SERIES for fund in y[series].get("funds") or []}
    for row in data:
        ticker = str(row.get("ticker") or "").strip().upper()
        series = series_of(ticker)
        if not series:
            continue
        if FEED_TYPE[series] not in (row.get("types") or []):
            continue
        try:
            mat = int(row["maturityYear"])
            enay = enay_of(row)
            ytw = float(row["yieldToWorst"])
            weighted = row.get("weightedAvgYieldToMaturity")
            ytm = float(weighted) if weighted is not None else (ytw if series != "tips" else float(weighted))
            price = cents(row["closingPrice"])
            stamps = row.get("asOfDates") or {}
            ms = stamps.get("closingPriceAsOfDate") or stamps.get("yieldToWorst")
            day = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date().isoformat()
            name = _clean_name(row.get("name"))
        except Exception as ex:
            fail(f"{ticker}: missing/invalid field ({ex.__class__.__name__}: {ex})")
        if not name:
            fail(f"{ticker}: missing fund name")
        for label, value in (("ENAY", enay), ("YTM", ytm)):
            if not _yield_ok(value):
                fail(f"{ticker}: implausible {label} {value:.3f}%")
        if series == "tips" and not _yield_ok(ytw):
            fail(f"{ticker}: implausible real yield {ytw:.3f}%")
        if mat <= base_year:
            continue
        fund = next((item for item in y[series]["funds"] if item.get("ticker") == ticker), None)
        if fund is None:
            fund = {"ticker": ticker, "maturity": mat, "name": "", "ytm": None, "enay": None}
            if series == "tips":
                fund["realYield"] = None
            y[series]["funds"].append(fund)
            print(f"  + new fund {ticker} ({mat})")
        fund["maturity"] = mat
        fund["name"] = name
        fund["prevClose"] = price
        fund["ytm"] = round(ytm, 4)
        fund["enay"] = cents(enay)
        if series == "tips":
            fund["realYield"] = round(ytw, 4)
        asofs.setdefault(series, set()).add(day)
        seen[series].add(ticker)
        cnt[series] += 1
    for series in SERIES:
        if len(seen[series]) < MIN_FUNDS:
            fail(f"only {len(seen[series])} {series} funds found in BlackRock data")
        y[series]["funds"] = [_pack_fund(fund, series) for fund in y[series]["funds"] if fund.get("ticker") in seen[series]]
        y[series]["funds"].sort(key=lambda fund: fund["maturity"])
        day = max(asofs[series])
        y[series]["enayAsOf"] = day
        if series == "tips":
            y["tips"]["realYieldAsOf"] = day
        if len(asofs[series]) > 1:
            print(f"  ! {series}: mixed as-of dates {sorted(asofs[series])}; using {day}")
    for series in SERIES:
        for fund in y[series]["funds"]:
            prev = old.get(fund["ticker"], {})
            extra = f"   real {prev.get('realYield')} -> {fund.get('realYield')}" if series == "tips" else ""
            print(f"  {fund['ticker']} {fund['maturity']}: ENAY {prev.get('enay')} -> {fund['enay']}   YTM {prev.get('ytm')} -> {fund['ytm']}{extra}")
    y["notes"] = NOTES
    print("  BlackRock data as of: " + ", ".join(f"{series} {y[series]['enayAsOf']}" for series in SERIES))
    for series in SERIES:
        years = sorted(fund["maturity"] for fund in y[series]["funds"])
        gaps = [year for year in range(years[0], years[-1] + 1) if year not in years]
        gap_note = f"; NO FUND for {gaps}" if gaps else "; no gaps"
        print(f"  {series}: funds mature {years[0]}-{years[-1]} (distribution years {years[0] + 1}-{years[-1] + 1}){gap_note}")


def load_xls(path, y):
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows = [list(rw) for rw in ws.iter_rows(values_only=True)]
    hi = next(i for i, rw in enumerate(rows) if any(isinstance(c, str) and c.strip().lower() == "ticker" for c in rw))
    header = [str(c or "").lower() for c in rows[hi]]

    def col(pat, notpat=None):
        for i, h in enumerate(header):
            if re.search(pat, h) and not (notpat and re.search(notpat, h)):
                return i
        return -1

    c_ticker = col(r"^\s*ticker")
    c_mat = col("maturity")
    c_name = col("fund name")
    c_price = col("previous close")
    c_ytm = col("yield to maturity")
    c_enay = col("net acquisition yield", "tax equivalent")
    asof = None
    for rw in rows:
        for cell in rw:
            if isinstance(cell, str):
                m = re.search(r"As of\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", cell)
                if m and m.group(1).lower() in MONTHS:
                    asof = f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
                    break
        if asof:
            break
    seen = set()
    for series in SERIES:
        y.setdefault(series, {"enayAsOf": None, "funds": []})
    for rw in rows[hi + 1:]:
        ticker = str(rw[c_ticker] or "").strip().upper()
        if not ticker:
            continue
        if ticker == "TOTAL":
            break
        series = series_of(ticker)
        if not series:
            continue
        fund = next((item for item in y[series]["funds"] if item["ticker"] == ticker), None)
        if fund is None:
            fund = {"ticker": ticker, "maturity": int(rw[c_mat]), "name": "", "ytm": None, "enay": None}
            if series == "tips":
                fund["realYield"] = None
            y[series]["funds"].append(fund)
        if c_name >= 0 and rw[c_name]:
            fund["name"] = _clean_name(rw[c_name])
        if c_price >= 0 and rw[c_price] is not None:
            fund["prevClose"] = float(rw[c_price])
        if series != "tips" and c_ytm >= 0 and rw[c_ytm] is not None:
            fund["ytm"] = round(float(rw[c_ytm]), 4)
        fund["enay"] = round(float(rw[c_enay]), 4)
        seen.add(series)
    for series in seen:
        if asof:
            y[series]["enayAsOf"] = asof
        y[series]["funds"].sort(key=lambda fund: fund["maturity"])
    print(f"  {os.path.basename(path)}: updated ENAY for {', '.join(sorted(seen)) or 'nothing'} (as of {asof})")


def load_doc(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as e:
        fail(f"could not read {path} ({e.__class__.__name__}: {e})")
    if not isinstance(doc, dict) or "pjtxx" not in doc:
        fail(f"{path} is not a yields document (pjtxx is kept from this file)")
    return doc


def write_doc(path, y, now=None):
    doc = canonicalize(y)
    validate_doc(doc, now=now)
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, encoding="utf-8") as fh:
            check = json.load(fh)
        validate_doc(check, now=now)
        if check.get("asOf") != doc["asOf"]:
            fail("re-read yields did not match")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    print(f"  wrote {path}")
    return doc


def business_days_old(as_of, today=None):
    today = today or datetime.date.today()
    start = datetime.date.fromisoformat(as_of)
    return sum(1 for i in range(1, (today - start).days + 1) if (start + datetime.timedelta(i)).weekday() < 5)


def build(argv=None, now=None):
    ap = argparse.ArgumentParser(description="Update data/yields.json from the public BlackRock iBonds feed.")
    ap.add_argument("--json", help="saved copy of the BlackRock table.jsn feed instead of downloading")
    ap.add_argument("--xls", action="append", default=[], help="BlackRock ENAY .xlsx (repeatable)")
    ap.add_argument("--no-fetch", action="store_true", help="do not download (use with --xls / --pjtxx)")
    ap.add_argument("--pjtxx", type=float, help="PJTXX 7-day yield percent (manual; not on the BlackRock feed)")
    ap.add_argument("--pjtxx-asof", help="YYYY-MM-DD")
    ap.add_argument("--in", dest="src", help="existing yields.json to keep PJTXX from (default: data/yields.json)")
    ap.add_argument("--out", dest="dest", help="where to write (default: data/yields.json)")
    args = ap.parse_args(argv)
    src = args.src or args.dest or YJ
    dest = args.dest or YJ
    y = load_doc(src)
    prev = y.get("asOf")
    if args.json:
        try:
            with open(args.json, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            fail(f"could not read {args.json} ({e})")
        print(f"Reading {args.json}…")
        apply_feed(data, y)
    elif not args.no_fetch:
        print("Downloading BlackRock iBonds ladder data…")
        apply_feed(download_feed(), y)
    for path in args.xls:
        load_xls(path, y)
    if args.pjtxx is not None:
        y.setdefault("pjtxx", {})
        y["pjtxx"]["ticker"] = "PJTXX"
        y["pjtxx"]["yield"] = args.pjtxx
        y["pjtxx"]["asOf"] = args.pjtxx_asof or (now or datetime.date.today()).isoformat()
        y["pjtxx"].setdefault("name", "JPMorgan U.S. Treasury Plus MMF (PJTXX)")
        y["pjtxx"].setdefault("yieldBasis", "7-day SEC yield (fact sheet)")
    dates = [y[s].get("enayAsOf") for s in SERIES if isinstance(y.get(s), dict)]
    dates.append(y.get("tips", {}).get("realYieldAsOf") if isinstance(y.get("tips"), dict) else None)
    dates = [d for d in dates if d]
    if not dates:
        fail("no as-of date produced")
    y["asOf"] = max(dates)
    y.setdefault("notes", NOTES)
    doc = write_doc(dest, y, now=now)
    age = business_days_old(doc["asOf"], today=now)
    print(f"OK: yields as of {doc['asOf']} (previous {prev}); {age} business day(s) old"
          + ("  ! older than 2 business days" if age > 2 else ""))
    pj_day = parse_date(doc["pjtxx"]["asOf"])
    today = now or datetime.date.today()
    if pj_day and pj_day < today - datetime.timedelta(days=45):
        print(f"  note: PJTXX yield {doc['pjtxx']['yield']}% is as of {doc['pjtxx']['asOf']} (manual: --pjtxx)")
    return doc


def main(argv=None):
    try:
        build(argv)
    except RefreshError as e:
        sys.exit(e.code)


if __name__ == "__main__":
    main()
