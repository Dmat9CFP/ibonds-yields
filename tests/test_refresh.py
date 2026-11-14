#!/usr/bin/env python3
"""Updater and published-file checks. No network."""
import datetime
import io
import json
import pathlib
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import refresh  # noqa: E402

NOW = datetime.date(2026, 11, 9)
ASOF = "2026-11-06"
ASOF_MS = int(datetime.datetime(2026, 11, 6, 20, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def fund(ticker, year, kind, ytw=4.2, ytm=4.4, price=24.126, nav=24.204, dur=2.0, netr=0.10, name=None, with_ytm=True):
    row = {
        "ticker": ticker,
        "maturityYear": year,
        "name": name or f"iShares iBonds {ticker}",
        "types": [kind, "iBonds"],
        "closingPrice": price,
        "navAmount": nav,
        "effectiveDuration": dur,
        "yieldToWorst": ytw,
        "netr": netr,
        "asOfDates": {"closingPriceAsOfDate": ASOF_MS, "yieldToWorst": ASOF_MS},
    }
    if with_ytm:
        row["weightedAvgYieldToMaturity"] = ytm
    return row


def sample_feed(start=2027):
    rows = []
    tips = ["IBID", "IBIE", "IBIF", "IBIG", "IBIH"]
    corp = ["IBDS", "IBDT", "IBDU", "IBDV", "IBDW"]
    treas = ["IBTH", "IBTI", "IBTJ", "IBTK", "IBTL"]
    muni = ["IBMP", "IBMQ", "IBMR", "IBMS", "IBMT"]
    for i, ticker in enumerate(tips):
        rows.append(fund(ticker, start + i, "Tips", ytw=1.25 + i / 100, ytm=3.5, price=25.1, nav=25.2, dur=4))
    for i, ticker in enumerate(corp):
        rows.append(fund(ticker, start + i, "Corporate", ytw=4.5, ytm=4.8))
    for i, ticker in enumerate(treas):
        rows.append(fund(ticker, start + i, "Treasury", ytw=4.1, ytm=4.3, with_ytm=(ticker != "IBTK")))
    for i, ticker in enumerate(muni):
        rows.append(fund(ticker, start + i, "Municipal", ytw=3.1, ytm=3.4))
    rows.append(fund("IBGA", 2044, "Treasury", name="ignored long treasury"))
    rows.append(fund("IBXX", 2030, "High Yield", name="ignored"))
    return rows


def ibmo_row(year=2026, ticker="IBMO", dur=0.00597):
    """Live IBMO print on 2026-11-13: a 2-cent gap over a tiny duration."""
    return fund(
        ticker, year, "Municipal",
        ytw=2.63, ytm=2.8, price=25.61, nav=25.627495, dur=dur, netr=0.1,
        name="iShares iBonds Dec 2026 Term Muni Bond ETF",
    )


def stamp(rows, day):
    ms = int(datetime.datetime(day.year, day.month, day.day, 21, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    out = []
    for row in rows:
        row = dict(row)
        row["asOfDates"] = {"closingPriceAsOfDate": ms, "yieldToWorst": ms}
        out.append(row)
    return out


def build_feed(rows, doc=None, now=NOW):
    """Refresh against an in-memory feed.

    Returns (doc or None, error or None, dest bytes, dest bytes before, stdout, stderr).
    """
    import tempfile
    base = doc if doc is not None else json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / "in.json"
        src.write_text(json.dumps(base), encoding="utf-8")
        dest = td / "out.json"
        dest.write_text('{"keep": true}\n', encoding="utf-8")
        before = dest.read_bytes()
        feed = td / "feed.json"
        feed.write_text(json.dumps(rows), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                built = refresh.build(["--json", str(feed), "--in", str(src), "--out", str(dest)], now=now)
        except refresh.RefreshError as exc:
            return None, exc, dest.read_bytes(), before, out.getvalue(), err.getvalue()
        return built, None, dest.read_bytes(), before, out.getvalue(), err.getvalue()


class RefreshTests(unittest.TestCase):
    def test_enay_matches_tool_formula(self):
        row = fund("IBDS", 2027, "Corporate", ytw=4.5, ytm=4.8, price=24.126, nav=24.204, dur=2, netr=0.10)
        price, nav = 24.13, 24.20
        expected = (nav - price) / (2 * nav) * 100 + 4.5 - 0.10
        self.assertAlmostEqual(refresh.enay_of(row), expected, places=9)
        self.assertEqual(refresh.cents(expected), 4.54)

    def test_near_maturity_funds_do_not_fail_the_refresh(self):
        # 2026-11-13 live IBMO: duration 0.00597 turns a $0.02 gap into ENAY 15.601%.
        blown = ibmo_row()
        enay = refresh.enay_of(blown)
        self.assertEqual(f"{enay:.3f}", "15.601")
        self.assertFalse(refresh._yield_ok(enay))
        zero = ibmo_row(ticker="IBTG", dur=0)
        zero["types"] = ["Treasury"]
        with self.assertRaises(ZeroDivisionError):
            refresh.enay_of(zero)
        negative = fund("IBDR", 2026, "Corporate", price=30, nav=25, dur=0.01, ytw=4.07, netr=0.1)
        self.assertLess(refresh.enay_of(negative), -2)
        # Equal cents and a negative duration: the spread term is 0, so the
        # yield lands in range. The duration itself is still not usable.
        flipped = fund("IBDQ", 2026, "Corporate", price=24.13, nav=24.13, dur=-1, ytw=4.5, netr=0.1)
        self.assertTrue(refresh._yield_ok(refresh.enay_of(flipped)))
        unpriceable = fund("IBDP", 2024, "Corporate", name="already matured")
        unpriceable.pop("effectiveDuration")
        unpriceable.pop("closingPrice")
        unpriceable.pop("navAmount")

        rows = sample_feed() + [blown, zero, negative, flipped, unpriceable]
        doc, err, written, before, stdout, stderr = build_feed(rows)
        self.assertIsNone(err, stderr)
        self.assertNotEqual(written, before)
        self.assertNotIn("implausible", stderr)
        self.assertNotIn("implausible", stdout)
        for ticker in ("IBMO", "IBTG", "IBDR", "IBDQ", "IBDP"):
            self.assertIn(f"{ticker} matures", stdout)
            self.assertNotIn(ticker, json.dumps(doc))
        self.assertEqual(doc["asOf"], ASOF)
        for series in refresh.SERIES:
            years = [f["maturity"] for f in doc[series]["funds"]]
            self.assertGreaterEqual(min(years), 2027)
            self.assertGreaterEqual(len(years), 5)
        ibds = next(f for f in doc["corporate"]["funds"] if f["ticker"] == "IBDS")
        self.assertEqual(ibds["enay"], 4.54)
        ibmp = next(f for f in doc["muni"]["funds"] if f["ticker"] == "IBMP")
        self.assertEqual(ibmp["maturity"], 2027)
        self.assertEqual(ibmp["enay"], 3.14)
        refresh.validate_doc(doc, now=NOW)

        # The same print on a rung that is still in the ladder fails closed.
        poison = sample_feed()
        for row in poison:
            if row["ticker"] == "IBMP":
                row.update(effectiveDuration=0.00597, closingPrice=25.61, navAmount=25.627495, yieldToWorst=2.63, netr=0.1)
        _, err, written, before, _, stderr = build_feed(poison)
        self.assertIsNotNone(err)
        self.assertEqual(written, before)
        self.assertIn("IBMP: implausible ENAY 15.601%", str(err))
        self.assertIn("IBMP: implausible ENAY 15.601%", stderr)

        # A non-positive duration on a kept fund is rejected even when the
        # formula would return a yield inside the published range.
        sign = sample_feed()
        for row in sign:
            if row["ticker"] == "IBDS":
                row.update(effectiveDuration=-1, closingPrice=24.13, navAmount=24.13)
        _, err, written, before, _, stderr = build_feed(sign)
        self.assertIsNotNone(err)
        self.assertEqual(written, before)
        self.assertIn("IBDS: effectiveDuration -1 is not positive", str(err))
        self.assertIn("Nothing was written", stderr)

        missing = sample_feed() + [ibmo_row(dur=0)]
        missing[-1].pop("maturityYear")
        _, err, written, before, _, _ = build_feed(missing)
        self.assertIsNotNone(err)
        self.assertEqual(written, before)
        self.assertIn("IBMO", str(err))
        self.assertIn("maturity", str(err))

    def test_base_year_is_the_input_asof_year(self):
        # November of the following year: the file's as-of year has rolled,
        # so the fund maturing that year is cash even with IBMO's ENAY.
        rolled = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
        rolled["asOf"] = "2027-11-12"
        rows = stamp(sample_feed(2028), datetime.date(2027, 11, 13))
        rows.append(ibmo_row(year=2027))
        rows.append(ibmo_row(year=2027, ticker="IBTG", dur=0))
        rows[-1]["types"] = ["Treasury"]
        now = datetime.date(2027, 11, 13)
        doc, err, _, _, stdout, stderr = build_feed(rows, doc=rolled, now=now)
        self.assertIsNone(err, stderr)
        self.assertEqual(doc["asOf"], "2027-11-13")
        self.assertIn("IBMO matures 2027", stdout)
        self.assertIn("IBTG matures 2027", stdout)
        tickers = [f["ticker"] for series in refresh.SERIES for f in doc[series]["funds"]]
        self.assertNotIn("IBMO", tickers)
        self.assertNotIn("IBTG", tickers)
        for series in refresh.SERIES:
            self.assertTrue(all(f["maturity"] >= 2028 for f in doc[series]["funds"]))
        refresh.validate_doc(doc, now=now)

        # First refresh after New Year still uses the previous file's year, so
        # the new year's rungs stay in the file and are range-checked.
        prior = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
        prior["asOf"] = "2026-12-31"
        january = stamp(sample_feed(2027), datetime.date(2027, 1, 2))
        doc, err, _, _, stdout, stderr = build_feed(january, doc=prior, now=datetime.date(2027, 1, 5))
        self.assertIsNone(err, stderr)
        self.assertEqual(doc["asOf"], "2027-01-02")
        self.assertIn(2027, [f["maturity"] for f in doc["muni"]["funds"]])
        self.assertNotIn("not a ladder rung", stdout)

        # No asOf on the input uses the same 2026 fallback as ladder.html.
        undated = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
        del undated["asOf"]
        doc, err, _, _, stdout, stderr = build_feed(sample_feed() + [ibmo_row()], doc=undated)
        self.assertIsNone(err, stderr)
        self.assertIn("IBMO matures 2026; not a ladder rung (as-of year 2026)", stdout)
        self.assertNotIn("IBMO", [f["ticker"] for f in doc["muni"]["funds"]])

    def test_published_file_validates_and_matches_embedded(self):
        doc = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
        refresh.validate_doc(doc, now=NOW)
        html = (ROOT / "ladder.html").read_text(encoding="utf-8")
        blob = re.search(
            r'<script id="embedded-yields" type="application/json">(.*?)</script>', html, re.S
        ).group(1)
        embedded = json.loads(blob)
        refresh.validate_doc(embedded, now=NOW)
        self.assertRegex(doc["asOf"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(embedded["asOf"], r"^\d{4}-\d{2}-\d{2}$")

    def test_good_feed_writes_schema_and_bad_feed_does_not_overwrite(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            base = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
            base["corporate"]["funds"].append({
                "ticker": "IBDZ", "maturity": 2099, "name": "stale", "prevClose": 20, "ytm": 4, "enay": 4,
            })
            src = td / "in.json"
            src.write_text(json.dumps(base), encoding="utf-8")
            dest = td / "out.json"
            dest.write_text('{"keep": true}\n', encoding="utf-8")
            before = dest.read_bytes()
            feed = td / "feed.json"
            feed.write_text(json.dumps(sample_feed()), encoding="utf-8")

            doc = refresh.build(["--json", str(feed), "--in", str(src), "--out", str(dest)], now=NOW)
            self.assertEqual(doc["asOf"], ASOF)
            self.assertEqual(doc["pjtxx"], base["pjtxx"])
            for series in refresh.SERIES:
                self.assertGreaterEqual(len(doc[series]["funds"]), 5)
                self.assertTrue(all(refresh.TICKER_RE[series].fullmatch(f["ticker"]) for f in doc[series]["funds"]))
                self.assertNotIn(2099, [f["maturity"] for f in doc[series]["funds"]])
            ibds = next(f for f in doc["corporate"]["funds"] if f["ticker"] == "IBDS")
            self.assertEqual(ibds["enay"], 4.54)
            self.assertEqual(ibds["ytm"], 4.8)
            ibtk = next(f for f in doc["treasury"]["funds"] if f["ticker"] == "IBTK")
            self.assertEqual(ibtk["ytm"], 4.1)
            ibid = next(f for f in doc["tips"]["funds"] if f["ticker"] == "IBID")
            self.assertEqual(ibid["realYield"], 1.25)
            refresh.validate_doc(json.loads(dest.read_text(encoding="utf-8")), now=NOW)

            again = dest.read_bytes()
            refresh.build(["--json", str(feed), "--in", str(dest), "--out", str(dest)], now=NOW)
            self.assertEqual(dest.read_bytes(), again)

            bad = json.loads(feed.read_text(encoding="utf-8"))
            for row in bad:
                if row["ticker"] == "IBDS":
                    row["yieldToWorst"] = 80
            bad_path = td / "bad.json"
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            kept = dest.read_bytes()
            with self.assertRaises(refresh.RefreshError):
                refresh.build(["--json", str(bad_path), "--in", str(src), "--out", str(dest)], now=NOW)
            self.assertEqual(dest.read_bytes(), kept)

            proc = subprocess.run(
                [sys.executable, str(ROOT / "refresh.py"), "--json", str(td / "missing.json"), "--in", str(src), "--out", str(dest)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("Nothing was written", proc.stderr)
            self.assertEqual(dest.read_bytes(), kept)

            short = sample_feed()[:3]
            short_path = td / "short.json"
            short_path.write_text(json.dumps(short), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "refresh.py"), "--json", str(short_path), "--in", str(src), "--out", str(dest)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(dest.read_bytes(), kept)
            self.assertNotEqual(before, kept)

    def test_validator_rejects_bad_shapes(self):
        doc = json.loads((ROOT / "data" / "yields.json").read_text(encoding="utf-8"))
        cases = []
        broken = json.loads(json.dumps(doc))
        broken["asOf"] = "2026-02-31"
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["asOf"] = "not-a-date"
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["tips"]["funds"] = []
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["tips"]["funds"][0]["ytm"] = 40
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["tips"]["funds"][0]["ticker"] = "IBGX"
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["extra"] = 1
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        del broken["tips"]["funds"][0]["realYield"]
        cases.append(broken)
        broken = json.loads(json.dumps(doc))
        broken["corporate"]["funds"][0]["name"] = "<img src=x>"
        cases.append(broken)
        future = json.loads(json.dumps(doc))
        future["asOf"] = "2099-01-01"
        for series in refresh.SERIES:
            future[series]["enayAsOf"] = "2099-01-01"
        future["tips"]["realYieldAsOf"] = "2099-01-01"
        cases.append(future)
        for bad in cases:
            with self.assertRaises(refresh.RefreshError):
                refresh.validate_doc(bad, now=NOW)

    def test_workflow_permissions_and_pins(self):
        text = (ROOT / ".github" / "workflows" / "refresh-yields.yml").read_text(encoding="utf-8")
        self.assertIn("contents: write", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("30 22 * * 1-5", text)
        uses = re.findall(r"uses:\s*(\S+)", text)
        self.assertEqual(uses, [
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
        ])
        self.assertNotIn("pull-requests:", text)
        self.assertNotIn("id-token:", text)
        html = (ROOT / "ladder.html").read_text(encoding="utf-8")
        self.assertIn(
            'content="connect-src https://raw.githubusercontent.com; form-action \'none\'; img-src \'self\' data: blob:; base-uri \'none\'"',
            html,
        )
        self.assertEqual(len(re.findall(r"\bfetch\s*\(", html)), 1)
        self.assertIn("https://raw.githubusercontent.com/Dmat9CFP/ibonds-yields/main/data/yields.json", html)
        self.assertNotIn("/workspace/ladder-tool", html)


if __name__ == "__main__":
    unittest.main()
