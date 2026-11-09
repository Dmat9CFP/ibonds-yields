#!/usr/bin/env python3
"""Updater and published-file checks. No network."""
import datetime
import json
import pathlib
import re
import subprocess
import sys
import unittest

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


def sample_feed():
    rows = []
    tips = ["IBID", "IBIE", "IBIF", "IBIG", "IBIH"]
    corp = ["IBDS", "IBDT", "IBDU", "IBDV", "IBDW"]
    treas = ["IBTH", "IBTI", "IBTJ", "IBTK", "IBTL"]
    muni = ["IBMP", "IBMQ", "IBMR", "IBMS", "IBMT"]
    for i, ticker in enumerate(tips):
        rows.append(fund(ticker, 2027 + i, "Tips", ytw=1.25 + i / 100, ytm=3.5, price=25.1, nav=25.2, dur=4))
    for i, ticker in enumerate(corp):
        rows.append(fund(ticker, 2027 + i, "Corporate", ytw=4.5, ytm=4.8))
    for i, ticker in enumerate(treas):
        rows.append(fund(ticker, 2027 + i, "Treasury", ytw=4.1, ytm=4.3, with_ytm=(ticker != "IBTK")))
    for i, ticker in enumerate(muni):
        rows.append(fund(ticker, 2027 + i, "Municipal", ytw=3.1, ytm=3.4))
    rows.append(fund("IBGA", 2044, "Treasury", name="ignored long treasury"))
    rows.append(fund("IBXX", 2030, "High Yield", name="ignored"))
    return rows


class RefreshTests(unittest.TestCase):
    def test_enay_matches_tool_formula(self):
        row = fund("IBDS", 2027, "Corporate", ytw=4.5, ytm=4.8, price=24.126, nav=24.204, dur=2, netr=0.10)
        price, nav = 24.13, 24.20
        expected = (nav - price) / (2 * nav) * 100 + 4.5 - 0.10
        self.assertAlmostEqual(refresh.enay_of(row), expected, places=9)
        self.assertEqual(refresh.cents(expected), 4.54)

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
