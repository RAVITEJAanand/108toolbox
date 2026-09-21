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
PLACEHOLDERS = [
    "example.com",
    "hello@example.com",
    # Instructional text written for whoever is building the site, not for a
    # visitor. "(replace this with your real address)" sat on the live contact
    # page next to the email for weeks, because nothing was looking for it.
    "replace this with your real",
    "your-email-here",
    "TODO-slug",
]
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

# ---- 2b. Every tool sits in a category that actually exists ----------------
# A typo here is invisible: the tool still renders on the grid, but no chip
# ever matches it, so the only way to reach it is search. CATEGORIES is
# allowed to run ahead of the build, so an empty category is a note, not a
# failure - main.js simply does not draw a chip for one.
cat_block = re.search(r"const CATEGORIES = \[(.*?)\];", registry, re.S)
if not cat_block:
    fail("tools-data.js has no CATEGORIES array")
else:
    allowed = re.findall(r'"([^"]+)"', cat_block.group(1))
    used = re.findall(r'category: "([^"]+)"', registry)
    for bad in sorted(set(used) - set(allowed)):
        fail("category '%s' is not in CATEGORIES — no chip will ever match it" % bad)
    if set(used) <= set(allowed):
        ok("%d categories, every tool in a real one" % len(allowed))
    empty = [c for c in allowed if c not in used]
    if empty:
        ok("no tools yet in: %s (planned, so no chip is drawn)" % ", ".join(empty))

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

# ---- 4b. Every tool page carries the check-the-result notice ---------------
# The owner asked for this on every single tool, and "every single" is exactly
# the kind of promise that decays one forgotten page at a time. A tool that
# quietly loses its notice looks fine, so only a check like this will ever
# catch it. The link matters as much as the words: without it the notice is a
# dead end, and disclaimer.html is where the full position is set out.
missing_notice = []
for path in tool_pages():
    text = path.read_text(encoding="utf-8")
    if ('class="tool-warn"' not in text
            or "Check the result before you rely on it" not in text
            or "disclaimer.html" not in text):
        missing_notice.append(path.name)
for name in missing_notice:
    fail("%s is missing the check-the-result notice — see rule 10 in CLAUDE.md" % name)
if not missing_notice:
    ok("every tool page carries the check-the-result notice")

# ---- 4c. The two dark blocks define the same tokens ------------------------
# Dark arrives two ways: the OS preference (@media prefers-color-scheme) and
# the site's own toggle (:root[data-theme="dark"]). They are deliberate
# duplicates, so a token added to one and missed in the other is always a bug,
# and a silent one: half the visitors see the right colour and half see the
# light value bleeding through. That is exactly how --warn shipped broken the
# first time - the notice came out cream-on-cream for anyone using the toggle.
css = (ROOT / "css" / "style.css").read_text(encoding="utf-8")
by_media = re.search(r"@media \(prefers-color-scheme: dark\) \{(.*?)\n\}", css, re.S)
by_toggle = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S)
if not by_media or not by_toggle:
    fail("style.css: cannot find both dark theme blocks")
else:
    media_tokens = set(re.findall(r"(--[a-z0-9-]+):", by_media.group(1)))
    toggle_tokens = set(re.findall(r"(--[a-z0-9-]+):", by_toggle.group(1)))
    for token in sorted(media_tokens - toggle_tokens):
        fail("style.css: %s is set for the OS dark preference but not for the "
             "theme toggle" % token)
    for token in sorted(toggle_tokens - media_tokens):
        fail("style.css: %s is set for the theme toggle but not for the OS "
             "dark preference" % token)
    if media_tokens == toggle_tokens:
        ok("both dark blocks define the same %d tokens" % len(media_tokens))

# ---- 4d. CLAUDE.md agrees with the registry --------------------------------
# Rule 2 says never hand-type a tool count into a page, because it goes stale.
# The same thing happened one file further out: CLAUDE.md said "Live (30)"
# above a list of 40 names, and it stayed wrong for a whole batch because
# nothing was reading it. A number that matters is a number worth checking,
# wherever it lives.
claude = ROOT / "CLAUDE.md"
if claude.exists():
    text = claude.read_text(encoding="utf-8")
    stated = re.search(r"\*\*Live \((\d+)\):\*\*", text)
    if not stated:
        fail("CLAUDE.md has no '**Live (n):**' line to check")
    elif int(stated.group(1)) != len(slugs):
        fail("CLAUDE.md says %s tools are live, the registry has %d"
             % (stated.group(1), len(slugs)))
    else:
        named = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", stated.string[
            stated.end():text.index("That count is checked")]))
        for missing in sorted(slugs - named):
            fail("CLAUDE.md does not list the tool '%s'" % missing)
        if slugs <= named:
            ok("CLAUDE.md lists all %d tools" % len(slugs))

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
