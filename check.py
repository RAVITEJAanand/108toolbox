#!/usr/bin/env python3
"""
108 ToolBox — pre-deploy check.

Run this before you upload. It fails loudly if anything is wrong, so you never
push a site with example.com still in the canonical tags.

  Windows:   python check.py
  Mac/Linux: python3 check.py
"""

import collections
import hashlib
import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
TARGET = 108          # the number in the name; what "on the way" counts down to
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

# ---- 2c. The registry reads in the order the site draws --------------------
# main.js sorts TOOLS A-Z by the name on the card before it paints anything,
# so a tool appended to the bottom of the array still lands in the right place
# on the page. The file would then be the one thing on the site telling a
# different story than the site - 51 objects in the order they happened to be
# written, which nobody can scan and every diff shuffles.
#
# So the file is held to the same order. The comparison is lowercased and code
# point by code point, exactly what main.js does, so the two can never mean
# different things by "A to Z".
name_order = re.findall(r'name: "([^"]+)"', registry)
ordered = sorted(name_order, key=str.lower)
if name_order != ordered:
    for earlier, later in zip(name_order, name_order[1:]):
        if earlier.lower() > later.lower():
            at = ordered.index(later)
            where = ("the top of the array" if at == 0
                     else "right after '%s'" % ordered[at - 1])
            fail("tools-data.js: '%s' is out of A-Z order — it belongs %s"
                 % (later, where))
elif name_order:
    ok("registry reads A-Z, the same order the grids draw")

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

# ---- 4e. The tool-count fallbacks match the registry -----------------------
# main.js fills every data-tool-count span at runtime, so these numbers are
# "only a fallback" - which is exactly why nobody notices when they rot. They
# have now gone stale twice, the second time sitting at 25 while the registry
# held 45. Everyone with JavaScript off, and every crawler that does not
# render, read the wrong number on the homepage, the tools page and the about
# page. A fallback nobody checks is just a wrong number with a delay on it.
counts = {"live": len(slugs), "remaining": TARGET - len(slugs)}
found_stale = False
found_any = False

for path in sorted(ROOT.glob("*.html")):
    text = path.read_text(encoding="utf-8")
    for kind, shown in re.findall(
            r'<span data-tool-count="(live|remaining)">(\d+)</span>', text):
        found_any = True
        if int(shown) != counts[kind]:
            found_stale = True
            fail("%s shows '%s' for tools %s — the registry means %d"
                 % (path.name, shown, kind, counts[kind]))

if found_any and not found_stale:
    ok("tool-count fallbacks all read %d live / %d to go"
       % (counts["live"], counts["remaining"]))
elif not found_any:
    fail("no data-tool-count spans found — has the markup been renamed?")

# ---- 4f. css/ and js/ cannot change unless ?v= moves -----------------------
# Rule 3 was the last thing on this site still guarded only by memory, and it
# failed three times. The worst was the quietest: tools 46 and 47 were added
# to js/tools-data.js without bumping the stamp, so every page still asked for
# tools-data.js?v=9 - the exact URL browsers already held with 45 tools. The
# pages returned 200, the server served the new registry, curl showed 47, and
# the site still looked untouched to anyone who had visited before.
#
# So this check remembers. assets.lock stores the stamp and a hash of every
# file in css/ and js/. If the files move and the stamp does not, that is a
# failure, not a warning.
#
# The hash normalises line endings first. Git rewrites LF to CRLF on checkout
# here, so hashing raw bytes would make the lock disagree with itself between
# one machine and the next.
def asset_fingerprint():
    parts = []
    for folder in ("css", "js"):
        for f in sorted((ROOT / folder).glob("*.*")):
            text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
            parts.append(f.name + "\0" + text)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


stamps = set()
for page in list(ROOT.glob("*.html")) + sorted((ROOT / "tools").glob("*.html")):
    stamps.update(re.findall(r'(?:href|src)="[^"]*\.(?:css|js)\?v=(\d+)"',
                             page.read_text(encoding="utf-8")))

lock_file = ROOT / "assets.lock"

if len(stamps) > 1:
    # A half-finished find-and-replace. Some visitors get the new CSS and the
    # old script, which is worse than either on its own.
    fail("pages disagree about the asset version: %s — finish the bump"
         % ", ".join("?v=" + s for s in sorted(stamps, key=int)))
elif not stamps:
    fail("no ?v= stamps found on any page — has the cache-busting been removed?")
else:
    stamp = stamps.pop()
    fingerprint = asset_fingerprint()

    try:
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lock = None

    if lock is None:
        lock_file.write_text(
            json.dumps({"version": stamp, "hash": fingerprint}, indent=2) + "\n",
            encoding="utf-8")
        ok("assets.lock created at ?v=%s — commit it" % stamp)
    elif lock.get("hash") == fingerprint:
        ok("css/ and js/ unchanged since ?v=%s" % lock.get("version"))
    elif lock.get("version") != stamp:
        lock_file.write_text(
            json.dumps({"version": stamp, "hash": fingerprint}, indent=2) + "\n",
            encoding="utf-8")
        ok("css/ or js/ changed and ?v= moved %s to %s — assets.lock updated"
           % (lock.get("version"), stamp))
    else:
        fail("css/ or js/ changed but ?v=%s did not move — bump every page to "
             "?v=%d, or returning visitors keep the old file"
             % (stamp, int(stamp) + 1))

# ---- 4h. Every function is inside a START/END pair -------------------------
# Rule 9, which existed for months with nothing checking it. The owner asked
# for the markers so that a bug report or an edit request lands on a findable
# point: read the marker names down a file and you know which block to open,
# without reading the code. That only works if it is true of EVERY block -
# one unmarked function and you are back to reading the whole file.
#
# When this was first measured, 4 of 56 pages complied and 197 functions sat
# outside any pair. They were marked in six batches; this keeps it that way.
#
# Nested helpers are ignored on purpose: a function declared inside another
# one is already inside its parent's block, and timestamp-converter quite
# legitimately declares fail() twice in two different scopes.
FUNC_LINE = re.compile(r"^(\s*)function ([A-Za-z_$][\w$]*)\s*\(")
MARK_START = re.compile(r"/\* ---- START: (.*?) ----")
MARK_END = re.compile(r"/\* ---- END: (.*?) ----")

rule9_bad = []
for path in tool_pages():
    body = re.search(r"<script data-tool>(.*?)\n  </script>",
                     path.read_text(encoding="utf-8"), re.S)
    if not body:
        rule9_bad.append("%s has no <script data-tool>" % path.name)
        continue
    script = body.group(1)
    lines = script.split("\n")

    depths = [len(m.group(1)) for m in (FUNC_LINE.match(l) for l in lines) if m]
    if not depths:
        continue                      # a page with no functions of its own
    top = min(depths)

    starts = [(m.start(), m.group(1)) for m in MARK_START.finditer(script)]
    ends = [(m.start(), m.group(1)) for m in MARK_END.finditer(script)]

    # Pair each START with the first later END whose name matches it. The END
    # label is allowed to be a shortened form of the START label, which is the
    # convention already in the files.
    spans, used = [], set()
    for pos, name in starts:
        for i, (epos, ename) in enumerate(ends):
            if i in used or epos <= pos:
                continue
            if name.startswith(ename) or ename.startswith(name):
                used.add(i)
                spans.append((pos, epos))
                break
        else:
            rule9_bad.append("%s: START '%s' is never closed" % (path.name, name))
    for i, (_, ename) in enumerate(ends):
        if i not in used:
            rule9_bad.append("%s: END '%s' has no START" % (path.name, ename))

    at, offsets = 0, []
    for line in lines:
        offsets.append(at)
        at += len(line) + 1

    for i, line in enumerate(lines):
        m = FUNC_LINE.match(line)
        if not m or len(m.group(1)) != top:
            continue
        here = offsets[i]
        if not any(a <= here <= b for a, b in spans):
            rule9_bad.append(
                "%s: %s() is not inside a START/END pair — rule 9"
                % (path.name, m.group(2)))

for problem in rule9_bad[:12]:
    fail(problem)
if len(rule9_bad) > 12:
    fail("...and %d more rule 9 problems" % (len(rule9_bad) - 12))
if not rule9_bad:
    ok("every function on every tool page is inside a START/END pair")

# ---- 4i. The markers must nest, never cross --------------------------------
# 4h pairs each START with the first LATER END whose label matches, so a
# crossed pair satisfies it completely: both markers find a partner, and every
# function still lands inside something. Two pages were crossed exactly that
# way and 4h passed them both -
#
#     /* ---- START: the alphabet ... ---- */     <- opened here
#     /* ---- END: drawing random numbers ---- */ <- closes the block ABOVE
#
# both of them pages the speed work had edited: a block was inserted, and the
# END belonging to the block above ended up below the START of the block
# beside it. Every marker was present and every marker pointed at the wrong
# place, which is worse than having none - rule 9 exists so that a name read
# at a glance can be trusted.
#
# Nesting IS allowed and is used on purpose: roman-numeral-converter marks one
# tricky paragraph inside a larger block. Crossing is what this forbids, and
# stack discipline tells the two apart without banning either.
cross_bad = []
for path in tool_pages():
    body = re.search(r"<script data-tool>(.*?)\n  </script>",
                     path.read_text(encoding="utf-8"), re.S)
    if not body:
        continue
    script = body.group(1)
    marks = sorted(
        [(m.start(), 0, m.group(1)) for m in MARK_START.finditer(script)] +
        [(m.start(), 1, m.group(1)) for m in MARK_END.finditer(script)])

    stack = []
    for _, kind, label in marks:
        if kind == 0:
            stack.append(label)
            continue
        if not stack:
            cross_bad.append("%s: END '%s' closes nothing" % (path.name, label))
            continue
        opened = stack.pop()
        # The END label is allowed to be a shortened form of the START label,
        # which is the convention already in the files.
        if not (opened.startswith(label) or label.startswith(opened)):
            cross_bad.append(
                "%s: END '%s' crosses START '%s' \u2014 the markers interleave "
                "instead of nesting" % (path.name, label, opened))
    for label in stack:
        cross_bad.append("%s: START '%s' is never closed" % (path.name, label))

for problem in cross_bad[:12]:
    fail(problem)
if len(cross_bad) > 12:
    fail("...and %d more crossed markers" % (len(cross_bad) - 12))
if not cross_bad:
    ok("no START/END pair crosses another \u2014 the markers nest cleanly")

# ---- 4g. ROADMAP.md agrees with the registry -------------------------------
# The same rot as the tool counts, one file further out. The "Built" column
# said 45 while 51 tools were live, and the ticks had not moved in three
# batches - so the one document that is supposed to say what is left to do was
# quietly overstating the work remaining. A plan nobody checks stops being a
# plan and becomes a wish.
roadmap = ROOT / "ROADMAP.md"
if roadmap.exists():
    text = roadmap.read_text(encoding="utf-8")
    per_cat = collections.Counter(re.findall(r'category: "([^"]+)"', registry))
    roadmap_ok = True

    for cat, planned, shown in re.findall(
            r"\| ([A-Z][A-Za-z &]*?) \| (\d+) \| (\d+) \|", text):
        if int(shown) != per_cat.get(cat, 0):
            roadmap_ok = False
            fail("ROADMAP.md says %d %s tools are built, the registry has %d"
                 % (int(shown), cat, per_cat.get(cat, 0)))

    total = re.search(r"\| \*\*Total\*\* \| \*\*108\*\* \| \*\*(\d+)\*\* \|", text)
    if not total:
        roadmap_ok = False
        fail("ROADMAP.md has no total row to check")
    elif int(total.group(1)) != len(slugs):
        roadmap_ok = False
        fail("ROADMAP.md totals %s tools built, the registry has %d"
             % (total.group(1), len(slugs)))

    # A tick that is wrong in either direction is worse than no tick: one hides
    # finished work, the other sends you to build something twice.
    for slug, mark in re.findall(r"`([a-z0-9][a-z0-9-]+)`( ✅)? \|", text):
        ticked = bool(mark)
        if ticked != (slug in slugs):
            roadmap_ok = False
            fail("ROADMAP.md %s '%s' — it %s built"
                 % ("ticks" if ticked else "does not tick", slug,
                    "is" if slug in slugs else "is not"))

    if roadmap_ok:
        ok("ROADMAP.md agrees with the registry, ticks included")

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
