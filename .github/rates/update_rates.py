#!/usr/bin/env python3
"""Write data/rates.js from the euro reference rates of the European Central Bank.

Run by .github/workflows/rates.yml every working day. Visitors' browsers never
fetch anything for the currency converter: they load data/rates.js from this
site like any other file, and this script is the only thing that ever talks
to the outside, from GitHub's machines.

  python .github/rates/update_rates.py            fetch and write
  python .github/rates/update_rates.py FILE.xml   use a saved copy (testing)

Exits 0 having written nothing when the published rates are not newer than
the ones already in the file, and non-zero, writing nothing, when what came
back does not look like the reference rates - a page with yesterday's rates
is better than a page with wrong ones.
"""
import datetime
import json
import pathlib
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

# ---- START: where the rates come from, where they go, and what they must hold ----
URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
OUT = pathlib.Path(__file__).resolve().parents[2] / "data" / "rates.js"
NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
MUST_HAVE = {"USD", "INR", "GBP", "JPY"}
HEADER = (
    "/* Exchange rates for tools/currency-converter.html: the euro reference\n"
    "   rates published by the European Central Bank each working day. Written by\n"
    "   .github/rates/update_rates.py; do not edit by hand. */\n")
# ---- END: where the rates come from, where they go, and what they must hold ----


# ---- START: fetching and reading the reference rates ----
def fetch():
    request = urllib.request.Request(URL, headers={"User-Agent": "108toolbox rates updater"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def parse(xml_bytes):
    """{'date': 'YYYY-MM-DD', 'rates': {'USD': '1.1225', ...}} with the rates as written."""
    root = ET.fromstring(xml_bytes)
    days = [c for c in root.iter(NS + "Cube") if c.get("time")]
    if len(days) != 1:
        raise ValueError("expected one dated set of rates, found %d" % len(days))
    day = days[0]
    rates = {}
    for cube in day.iter(NS + "Cube"):
        code, rate = cube.get("currency"), cube.get("rate")
        if code is None and rate is None:
            continue
        if not re.fullmatch(r"[A-Z]{3}", code or ""):
            raise ValueError("a currency code that is not three capitals: %r" % code)
        if not re.fullmatch(r"\d{1,7}(\.\d{1,8})?", rate or "") or float(rate) <= 0:
            raise ValueError("%s: a rate that is not a positive number: %r" % (code, rate))
        if code in rates:
            raise ValueError("%s is listed twice" % code)
        rates[code] = rate
    return {"date": day.get("time"), "rates": rates}
# ---- END: fetching and reading the reference rates ----


# ---- START: deciding whether the rates can be trusted ----
def check(new, old, today):
    date = new["date"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise ValueError("the date is not YYYY-MM-DD: %r" % date)
    day = datetime.date.fromisoformat(date)
    if day > today + datetime.timedelta(days=1):
        raise ValueError("the rates are dated in the future: %s" % date)
    if day < today - datetime.timedelta(days=14):
        raise ValueError("the rates are more than two weeks old: %s" % date)
    if len(new["rates"]) < 20 or not MUST_HAVE <= set(new["rates"]):
        raise ValueError("too few currencies, or a main one missing: %s" % sorted(new["rates"]))
    if "EUR" in new["rates"]:
        raise ValueError("the euro is the base, it cannot be listed against itself")
    # A rate that halves or doubles overnight is far more likely a mistake in
    # the file than a currency crisis; refuse it and let a person look.
    for code, rate in new["rates"].items():
        before = old.get("rates", {}).get(code) if old else None
        if before and not 0.5 <= float(rate) / float(before) <= 2:
            raise ValueError("%s moved from %s to %s in one step" % (code, before, rate))
# ---- END: deciding whether the rates can be trusted ----


# ---- START: the file the page loads ----
def render(new):
    lines = ['    "%s": %s' % (code, new["rates"][code]) for code in sorted(new["rates"])]
    return (HEADER + "window.EXCHANGE_RATES = {\n"
            '  "date": "%s",\n  "base": "EUR",\n  "rates": {\n%s\n  }\n};\n'
            % (new["date"], ",\n".join(lines)))


def read_existing(path):
    """The rates already in the file, or None; the file is JSON after the '=' sign."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"window\.EXCHANGE_RATES = (\{.*\});\n$", text, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    return {"date": data["date"], "rates": {k: repr(v) for k, v in data["rates"].items()}}
# ---- END: the file the page loads ----


# ---- START: running it: fetch, check, then write ----
def main():
    xml_bytes = pathlib.Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else fetch()
    new = parse(xml_bytes)
    old = read_existing(OUT)
    check(new, old, datetime.date.today())
    if old and old["date"] >= new["date"]:
        print("already up to date: %s" % old["date"])
        return 0
    text = render(new)
    # what was written must read back as exactly what was published
    back = json.loads(re.search(r"= (\{.*\});\n$", text, re.S).group(1))
    if back["date"] != new["date"] or {k: float(v) for k, v in new["rates"].items()} != back["rates"]:
        raise ValueError("the file does not read back as the rates")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("rates of %s written: %d currencies" % (new["date"], len(new["rates"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
# ---- END: running it: fetch, check, then write ----
