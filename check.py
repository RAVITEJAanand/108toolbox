#!/usr/bin/env python3
"""
108 ToolBox — pre-deploy check.

Run this before you upload. It fails loudly if anything is wrong, so you never
push a site with example.com still in the canonical tags.

  Windows:   python check.py
  Mac/Linux: python3 check.py
"""

import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
problems = []
notes = []


def fail(msg):
    problems.append(msg)


def ok(msg):
    notes.append(msg)


def tool_pages():
    """Every REAL tool page.

    Files whose name starts with "_" are templates, not tools, so they are
    skipped everywhere: they are allowed to keep their TODO placeholders and
    they must never be reported as an unregistered tool.
    """
    return sorted(p for p in (ROOT / "tools").glob("*.html")
                  if not p.name.startswith("_"))


# ---- 1. No placeholders left ----------------------------------------------
PLACEHOLDERS = ["example.com", "hello@example.com"]
for path in list(ROOT.glob("*.html")) + tool_pages() \
        + [ROOT / "robots.txt", ROOT / "sitemap.xml"]:
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    for placeholder in PLACEHOLDERS:
        if placeholder in text:
            fail("%s still contains '%s' — run setup.py" % (path.name, placeholder))

# ---- 2. Registry and files agree ------------------------------------------
registry = (ROOT / "js" / "tools-data.js").read_text(encoding="utf-8")
slugs = set(re.findall(r'slug: "([^"]+)"', registry))
files = set(p.stem for p in tool_pages())

for missing in sorted(slugs - files):
    fail("tools-data.js lists '%s' but tools/%s.html does not exist" % (missing, missing))
for orphan in sorted(files - slugs):
    fail("tools/%s.html exists but is NOT in tools-data.js — nobody can find it" % orphan)
if slugs and slugs == files:
    ok("%d tools, all registered" % len(slugs))

# ---- 3. Sitemap covers every tool -----------------------------------------
sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
missing_from_sitemap = [s for s in sorted(slugs) if "tools/%s.html" % s not in sitemap]
for slug in missing_from_sitemap:
    fail("sitemap.xml is missing tools/%s.html — Google may never find it" % slug)
if slugs and not missing_from_sitemap:
    ok("sitemap.xml covers every tool")

# ---- 4. Per-page SEO basics ------------------------------------------------
# Root pages get the same treatment as tool pages. They used to be skipped,
# and four of them quietly shipped with a meta description less than half the
# length Google wants — about.html was 75 characters. A page nobody validates
# is a page that drifts.
for path in tool_pages() + sorted(ROOT.glob("*.html")):
    text = path.read_text(encoding="utf-8")
    name = path.name
    is_tool = path.parent.name == "tools"

    # Measure what a READER sees, not the raw markup: "&amp;" is one
    # character on the results page, not five.
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    if not title:
        fail("%s has no <title>" % name)
    else:
        shown = html.unescape(title.group(1)).strip()
        if len(shown) > 62:
            fail("%s title is %d chars — Google truncates past ~60" % (name, len(shown)))

    desc = re.search(r'<meta name="description" content="(.*?)"', text, re.S)
    if not desc:
        fail("%s has no meta description" % name)
    else:
        shown = html.unescape(desc.group(1)).strip()
        if not (110 <= len(shown) <= 165):
            fail("%s meta description is %d chars — aim for 140-160" % (name, len(shown)))

    if text.count("<h1>") != 1:
        fail("%s has %d <h1> tags — there must be exactly one" % (name, text.count("<h1>")))

    if 'rel="canonical"' not in text:
        fail("%s has no canonical tag" % name)

    # Only tool pages carry FAQ structured data. Asking about.html for an FAQ
    # it was never meant to have would be noise, not a finding.
    if is_tool:
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
        if not ld:
            fail("%s has no FAQ structured data" % name)
        else:
            try:
                json.loads(ld.group(1))
            except Exception as e:
                fail("%s has invalid JSON-LD: %s" % (name, e))

# ---- 5. No dead internal links --------------------------------------------
for path in list(ROOT.glob("*.html")) + tool_pages():
    text = path.read_text(encoding="utf-8")
    for href in re.findall(r'href="([^"#?:]+\.(?:html|css|js))(?:\?[^"]*)?"', text):
        if not (path.parent / href).resolve().exists():
            fail("%s links to %s which does not exist" % (path.name, href))
    # The (?:\?...)? lets a cache-busting ?v=2 through while still checking
    # that the real file exists.
    for src in re.findall(r'src="([^"#?:]+\.js)(?:\?[^"]*)?"', text):
        if not (path.parent / src).resolve().exists():
            fail("%s loads %s which does not exist" % (path.name, src))

if not any("links to" in p or "loads" in p for p in problems):
    ok("no dead internal links")

# ---- Report ----------------------------------------------------------------
print()
if problems:
    print("FAILED — %d problem%s found:\n" % (len(problems), "" if len(problems) == 1 else "s"))
    for p in problems:
        print("  x  " + p)
    print("\nFix these, then run check.py again.\n")
    sys.exit(1)

print("ALL CHECKS PASSED\n")
for n in notes:
    print("  -  " + n)
print("\nReady to deploy.\n")
sys.exit(0)
