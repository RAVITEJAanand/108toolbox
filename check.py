#!/usr/bin/env python3
"""
108 ToolBox — pre-deploy check.

Run this before you upload. It fails loudly if anything is wrong, so you never
push a site with example.com still in the canonical tags.

  Windows:   python check.py
  Mac/Linux: python3 check.py
"""

import ast
import collections
import datetime
import hashlib
import html
import json
import pathlib
import re
import sys
from html.parser import HTMLParser

# ---- START: what every check below shares: the root, the report lists and the list of tool pages ----
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
# ---- END: what every check below shares: the root, the report lists and the list of tool pages ----


# ---- START: 1. No placeholders left ----
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
# ---- END: 1. No placeholders left ----

# ---- START: 2. Registry and files agree ----
registry = (ROOT / "js" / "tools-data.js").read_text(encoding="utf-8")
slugs = set(re.findall(r'slug: "([^"]+)"', registry))
files = set(p.stem for p in tool_pages())

for missing in sorted(slugs - files):
    fail("tools-data.js lists '%s' but tools/%s.html does not exist" % (missing, missing))
for orphan in sorted(files - slugs):
    fail("tools/%s.html exists but is NOT in tools-data.js — nobody can find it" % orphan)
if slugs and slugs == files:
    ok("%d tools, all registered" % len(slugs))
# ---- END: 2. Registry and files agree ----

# ---- START: 2b. Every tool sits in a category that actually exists ----
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
# ---- END: 2b. Every tool sits in a category that actually exists ----

# ---- START: 2c. The registry reads in the order the site draws ----
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
# ---- END: 2c. The registry reads in the order the site draws ----

# ---- START: 3. Sitemap covers every tool ----
sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
missing_from_sitemap = [s for s in sorted(slugs) if "tools/%s.html" % s not in sitemap]
for slug in missing_from_sitemap:
    fail("sitemap.xml is missing tools/%s.html — Google may never find it" % slug)
if slugs and not missing_from_sitemap:
    ok("sitemap.xml covers every tool")
# ---- END: 3. Sitemap covers every tool ----

# ---- START: 4. Per-page SEO basics ----
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
# ---- END: 4. Per-page SEO basics ----

# ---- START: 4b. Every tool page carries the check-the-result notice ----
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
# ---- END: 4b. Every tool page carries the check-the-result notice ----

# ---- START: 4c. The two dark blocks define the same tokens ----
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
# ---- END: 4c. The two dark blocks define the same tokens ----

# ---- START: 4d. CLAUDE.md agrees with the registry ----
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
# ---- END: 4d. CLAUDE.md agrees with the registry ----

# ---- START: 4e. The tool-count fallbacks match the registry ----
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
# ---- END: 4e. The tool-count fallbacks match the registry ----

# ---- START: 4f. css/ and js/ cannot change unless ?v= moves ----
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
# ---- END: 4f. css/ and js/ cannot change unless ?v= moves ----

# ---- START: 4h. Every block of every file is inside a START/END pair ----
# Rule 9, which existed for months with nothing checking it. The owner asked
# for the markers so that a bug report or an edit request lands on a findable
# point: read the marker names down a file and you know which block to open,
# without reading the code. That only works if it is true of EVERY block -
# one unmarked function and you are back to reading the whole file.
#
# When this was first measured, 4 of 56 pages complied and 197 functions sat
# outside any pair. They were marked in six batches, and this check then
# looked at the functions of each tool's own script.
#
# On 3 Oct 2026 the owner asked for the same everywhere: "every function,
# feature, tab, tool - even an HTML file, even a button". A function was not
# enough - the element lookups at the top of a script and the wiring at the
# bottom sat outside any pair on 105 pages, and the HTML, the stylesheets and
# the Python had almost none. 3,585 pairs went into the HTML, about 250 into
# the JavaScript, 47 into the CSS and about 140 into the Python, every one
# checked to have changed no element, rule or statement. This keeps it so:
#
#   JavaScript  every top-level statement of every tool's own script and of
#               js/*.js (the lines that only open or close the page's own
#               (function () { ... })(); wrapper are exempt)
#   CSS         every rule in css/*.css
#   Python      every top-level statement of the scripts here (imports and
#               the opening docstring are exempt)
#   HTML        every element in <head> and <body>; every block of the page
#               content; every section of the text below a tool; every panel
#               of a tool, and every block inside a panel in a pair of its
#               own - a field, a row of buttons, a row of figures, an output
#               (a heading part-way down a panel shares its block's pair)
#
# Nested helpers are ignored on purpose: a function declared inside another
# one is already inside its parent's block, and timestamp-converter quite
# legitimately declares fail() twice in two different scopes.

# ---- START: the marker patterns and how pairs are matched ----
MARK_START = re.compile(r"/\* ---- START: (.*?)(?: ----|$)", re.M)
MARK_END = re.compile(r"/\* ---- END: (.*?)(?: ----|$)", re.M)
JS_MARK = re.compile(r"/\* ---- (START|END): (.*?)(?: ----|$)")
PY_MARK = re.compile(r"^# (?:-{4}|={5}) (START|END): (.*?)(?: [-=]{3,})?\s*$")
HTML_MARK = re.compile(r"^\s*-{4} (START|END): (.*?)(?: -{4}|\n|$)", re.S)
JS_WRAPPER = re.compile(r"^(\(function\s*\(\)\s*\{|\}\)\(\);?|\"use strict\";|'use strict';)$")
JS_CLOSER = re.compile(r"^[)\]}]+[);,]*\s*(/\*.*\*/)?$")
FUNC_LINE = re.compile(r"^(\s*)function ([A-Za-z_$][\w$]*)\s*\(")

rule9_bad = []      # a block outside every pair
cross_bad = []      # a pair that crosses another, an END with no START, ...


def same_block(opened, closing):
    """An END label may be a shortened form of its START label (and the other
    way round) - the convention already in the files."""
    return opened.startswith(closing) or closing.startswith(opened)


def match_pairs(where, marks):
    """marks: [(kind, label)] in file order. Reports crossings, returns the
    depth after each mark."""
    stack, depths = [], []
    for kind, label in marks:
        if kind == "START":
            stack.append(label)
        elif not stack:
            cross_bad.append("%s: END '%s' closes nothing" % (where, label))
        else:
            opened = stack.pop()
            if not same_block(opened, label):
                cross_bad.append(
                    "%s: END '%s' crosses START '%s' — the markers interleave "
                    "instead of nesting" % (where, label, opened))
        depths.append(len(stack))
    for label in stack:
        cross_bad.append("%s: START '%s' is never closed" % (where, label))
    return depths


def block_comment_lines(lines):
    """True for each line that holds only a /* comment */ (or part of one)."""
    out, inside = [], False
    for line in lines:
        s = line.strip()
        if inside:
            out.append(True)
            if "*/" in s:
                inside = False
                out[-1] = not s.split("*/", 1)[1].strip()
            continue
        if s.startswith("//"):
            out.append(True)
        elif s.startswith("/*"):
            if "*/" in s:
                out.append(not s.split("*/", 1)[1].strip())
            else:
                inside = True
                out.append(True)
        else:
            out.append(False)
    return out
# ---- END: the marker patterns and how pairs are matched ----

# ---- START: JavaScript: every top-level statement ----
def check_script(where, script):
    lines = script.split("\n")
    comment = block_comment_lines(lines)
    depths = [len(m.group(1)) for m in (FUNC_LINE.match(l) for l in lines) if m]
    code = [len(l) - len(l.lstrip()) for i, l in enumerate(lines)
            if l.strip() and not comment[i] and not JS_WRAPPER.match(l.strip())]
    top = min(depths) if depths else (min(code) if code else 0)
    marks = [(m.group(1), m.group(2)) for l in lines for m in JS_MARK.finditer(l)]
    match_pairs(where, marks)
    depth = 0
    for i, line in enumerate(lines):
        for m in JS_MARK.finditer(line):
            depth += 1 if m.group(1) == "START" else -1
        s = line.strip()
        if not s or comment[i] or len(line) - len(line.lstrip()) != top:
            continue
        if JS_CLOSER.match(s) or JS_WRAPPER.match(s) or depth > 0:
            continue
        what = FUNC_LINE.match(line)
        rule9_bad.append("%s line %d: %s is not inside a START/END pair — rule 9"
                         % (where, i + 1, (what.group(2) + "()") if what else repr(s[:50])))


for path in tool_pages():
    body = re.search(r"<script data-tool>(.*?)\n  </script>",
                     path.read_text(encoding="utf-8"), re.S)
    if not body:
        rule9_bad.append("%s has no <script data-tool>" % path.name)
        continue
    check_script(path.name, body.group(1))
for path in sorted((ROOT / "js").glob("*.js")):
    check_script("js/" + path.name, path.read_text(encoding="utf-8"))
# ---- END: JavaScript: every top-level statement ----

# ---- START: CSS: every rule ----
for path in sorted((ROOT / "css").glob("*.css")):
    lines = path.read_text(encoding="utf-8").split("\n")
    comment = block_comment_lines(lines)
    where = "css/" + path.name
    match_pairs(where, [(m.group(1), m.group(2)) for l in lines for m in JS_MARK.finditer(l)])
    depth = 0
    for i, line in enumerate(lines):
        for m in JS_MARK.finditer(line):
            depth += 1 if m.group(1) == "START" else -1
        if not line.strip() or comment[i] or line[0] in " \t}" or depth > 0:
            continue
        rule9_bad.append("%s line %d: %r is not inside a START/END pair — rule 9"
                         % (where, i + 1, line.strip()[:50]))
# ---- END: CSS: every rule ----

# ---- START: Python: every top-level statement ----
for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / ".github").rglob("*.py")):
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    where = path.relative_to(ROOT).as_posix()
    marks, depth_at, depth = [], [], 0
    for line in lines:
        m = PY_MARK.match(line)
        if m:
            marks.append((m.group(1), m.group(2)))
            depth += 1 if m.group(1) == "START" else -1
        depth_at.append(depth)
    match_pairs(where, marks)
    for n, node in enumerate(ast.parse(src).body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if n == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                  # the docstring that opens the file
        if depth_at[node.lineno - 1] <= 0:
            rule9_bad.append("%s line %d: %r is not inside a START/END pair — rule 9"
                             % (where, node.lineno, lines[node.lineno - 1].strip()[:50]))
        target = node.targets[0] if isinstance(node, ast.Assign) else None
        if isinstance(target, ast.Subscript) and getattr(target.value, "id", "") == "T":
            # Each tool's test sits in a pair of its own, named for the tool:
            # inside the one big pair round all 108 a missing one goes unseen.
            slug = target.slice.value
            k = node.lineno - 2
            while k > 0 and lines[k].startswith("#") and not PY_MARK.match(lines[k]):
                k -= 1
            below = lines[node.end_lineno] if node.end_lineno < len(lines) else ""
            if not ("START: the test for %s ----" % slug in lines[k]
                    and "END: the test for %s ----" % slug in below):
                rule9_bad.append("%s line %d: the test for %s is not in a pair of its own"
                                 % (where, node.lineno, slug))
# ---- END: Python: every top-level statement ----

# ---- START: HTML: every block of every page ----
# A small tree of the page, built with the standard library's parser, keeping
# comments in place so a pair can be matched among the children of one
# element. A START and its END must be siblings.

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []            # Node, or ("comment", text)

    def els(self):
        return [c for c in self.children if isinstance(c, Node)]

    def cls(self):
        return (self.attrs.get("class") or "").split()


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("#root", [], None)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node
        if tag == "textarea":
            self.set_cdata_mode(tag)  # a sample of HTML inside is only text

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_comment(self, data):
        self.cur.children.append(("comment", data))


def find(node, test):
    for c in node.els():
        if test(c):
            yield c
        yield from find(c, test)


def check_level(where, parent, one_each=False):
    """Every element child of parent sits inside a pair opened among its
    siblings. one_each: and each pair holds exactly one block (a heading
    part-way down a panel may share its block's pair)."""
    marks = [HTML_MARK.match(c[1]) for c in parent.children if isinstance(c, tuple)]
    match_pairs(where, [(m.group(1), m.group(2)) for m in marks if m])
    depth, held = 0, []
    for c in parent.children:
        if isinstance(c, tuple):
            m = HTML_MARK.match(c[1])
            if m and m.group(1) == "START":
                depth += 1
                held.append([])
            elif m and m.group(1) == "END" and depth:
                depth -= 1
                blocks = held.pop()
                if one_each and depth == 0 and len(blocks) > 1:
                    rule9_bad.append("%s: one pair holds %d blocks (%s) — give each its own"
                                     % (where, len(blocks), ", ".join(blocks[:3])))
            continue
        if not isinstance(c, Node):
            continue
        heading = "panel__label" in c.cls()
        if heading and one_each and c is parent.els()[0]:
            continue                  # the panel's own heading
        if depth == 0:
            rule9_bad.append("%s: <%s%s> is not inside a START/END pair — rule 9"
                             % (where, c.tag, ("#" + c.attrs["id"]) if c.attrs.get("id") else
                                ("." + c.cls()[0]) if c.cls() else ""))
        elif held and not heading:
            held[-1].append(c.tag + ("#" + c.attrs["id"] if c.attrs.get("id") else ""))


def content_box(node):
    while len(node.els()) == 1 and node.els()[0].tag == "div":
        node = node.els()[0]
    return node


# The template is checked too; the _test- copies test_tools.py builds for a
# moment while it runs are not, and may vanish mid-read.
html_pages = sorted(ROOT.glob("*.html")) + sorted(
    p for p in (ROOT / "tools").glob("*.html") if not p.name.startswith("_test-"))
for path in html_pages:
    builder = TreeBuilder()
    builder.feed(path.read_text(encoding="utf-8"))
    root = builder.root
    name = path.relative_to(ROOT).as_posix()
    head = next(find(root, lambda n: n.tag == "head"), None)
    body = next(find(root, lambda n: n.tag == "body"), None)
    main = next(find(root, lambda n: n.tag == "main"), None)
    if not (head and body and main):
        rule9_bad.append("%s: no <head>, <body> or <main> to check" % name)
        continue
    check_level(name + " <head>", head)
    check_level(name + " <body>", body)
    box = content_box(main)
    check_level(name + " page content", box)
    for section in box.els():
        if section.tag == "section" and "prose" not in section.cls():
            check_level(name + " section", content_box(section))
    for prose in find(main, lambda n: "prose" in n.cls()):
        if prose is not box:
            check_level(name + " text below the tool", prose)
    for ws in find(main, lambda n: "workspace" in n.cls()):
        check_level(name + " workspace", ws, one_each=True)
    for panel in find(main, lambda n: "panel" in n.cls()):
        check_level(name + " panel", panel, one_each=True)
# ---- END: HTML: every block of every page ----

# ---- START: the verdict for 4h and 4i ----
for problem in rule9_bad[:12]:
    fail(problem)
if len(rule9_bad) > 12:
    fail("...and %d more rule 9 problems" % (len(rule9_bad) - 12))
if not rule9_bad:
    ok("rule 9: every block is inside a START/END pair - the HTML of %d pages, "
       "every tool's script, js/, css/ and the Python" % len(html_pages))
# ---- END: the verdict for 4h and 4i ----
# ---- END: 4h. Every block of every file is inside a START/END pair ----

# ---- START: 4i. The markers must nest, never cross ----
# 4h once paired each START with the first LATER END whose label matched, so
# a crossed pair satisfied it completely: both markers found a partner, and
# every function still landed inside something. Two pages were crossed exactly
# that way and 4h passed them both -
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
# stack discipline tells the two apart without banning either. The matching
# itself happens in 4h, for every kind of file, through match_pairs(); an
# HTML pair must also open and close among the children of one element.
for problem in cross_bad[:12]:
    fail(problem)
if len(cross_bad) > 12:
    fail("...and %d more crossed markers" % (len(cross_bad) - 12))
if not cross_bad:
    ok("no START/END pair crosses another — the markers nest cleanly, in every file")
# ---- END: 4i. The markers must nest, never cross ----

# ---- START: 4l. Every box has a name a screen reader can read ----
# A box inside a sentence - "What is [ ] % of [ ]?" - looks labelled to the
# eye and is announced as "spin button" and nothing else, because no <label>
# points at it. On 3 Oct 2026, 40 of the 467 boxes, choices and tick boxes
# on the tools were like that: the percentage, discount, margin and ratio
# sentences, the inches beside a feet box, the unit next to an amount. They
# carry an aria-label now. A control is named by a <label for> that points
# at it, a <label> around it, or aria-label / aria-labelledby.
unnamed = []
for path in html_pages:
    builder = TreeBuilder()
    builder.feed(path.read_text(encoding="utf-8"))
    pointed = {n.attrs.get("for") for n in find(builder.root, lambda n: n.tag == "label")}
    for ctl in find(builder.root, lambda n: n.tag in ("input", "select", "textarea")):
        if (ctl.attrs.get("type") or "").lower() == "hidden":
            continue
        named = (ctl.attrs.get("id") in pointed or (ctl.attrs.get("aria-label") or "").strip()
                 or (ctl.attrs.get("aria-labelledby") or "").strip())
        up = ctl.parent
        while not named and up is not None:
            named = up.tag == "label"
            up = up.parent
        if not named:
            unnamed.append("%s: <%s id=\"%s\"> has no label a screen reader can read"
                           % (path.relative_to(ROOT).as_posix(), ctl.tag, ctl.attrs.get("id", "")))
for problem in unnamed[:12]:
    fail(problem)
if len(unnamed) > 12:
    fail("...and %d more boxes with no name" % (len(unnamed) - 12))
if not unnamed:
    ok("every box, choice and tick box has a name a screen reader can read")
# ---- END: 4l. Every box has a name a screen reader can read ----

# ---- START: 4g. ROADMAP.md agrees with the registry ----
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
# ---- END: 4g. ROADMAP.md agrees with the registry ----

# ---- START: 4j. The exchange rates are data, and only data ----
# data/rates.js is written by a bot (.github/rates/update_rates.py, run by
# .github/workflows/rates.yml) and loaded as a script by currency-converter.
# It must stay one comment and one assignment of JSON: anything else in it
# would run on the page.
rates_path = ROOT / "data" / "rates.js"
if (ROOT / "tools" / "currency-converter.html").exists() and not rates_path.exists():
    fail("currency-converter is live but data/rates.js does not exist")
if rates_path.exists():
    rates_text = rates_path.read_text(encoding="utf-8")
    m = re.fullmatch(r"/\*[^*]*(?:\*(?!/)[^*]*)*\*/\nwindow\.EXCHANGE_RATES = (\{[^;]*\});\n", rates_text)
    rates_problem = ""
    if not m:
        rates_problem = "is not one comment and one assignment"
    else:
        try:
            rates_data = json.loads(m.group(1))
            datetime.date.fromisoformat(rates_data["date"])
            rates = rates_data["rates"]
            if rates_data.get("base") != "EUR" or set(rates_data) != {"date", "base", "rates"}:
                rates_problem = "has the wrong base or extra fields"
            elif len(rates) < 20 or not {"USD", "INR"} <= set(rates):
                rates_problem = "has too few currencies, or no USD or INR"
            elif not all(re.fullmatch(r"[A-Z]{3}", c) and isinstance(v, (int, float)) and not isinstance(v, bool)
                         and v > 0 for c, v in rates.items()):
                rates_problem = "has a code or a rate that is not right"
        except (ValueError, KeyError, TypeError) as e:
            rates_problem = "does not read: %s" % e
    if rates_problem:
        fail("data/rates.js " + rates_problem)
    else:
        ok("data/rates.js: %d rates of %s, data only" % (len(rates), rates_data["date"]))
# ---- END: 4j. The exchange rates are data, and only data ----

# ---- START: 4k. Advertising: where it is, and where it must never be ----
# One AdSense unit below each tool, and nowhere else. Never on the pages where
# people type passwords or keys - the ad script runs inside the page, so it
# could read them - and not on the homepage, the tools list or the policy
# pages. A page without ads keeps the strict policy, where nothing can be
# sent anywhere; a page with one opens Google's ad servers and nothing more.
AD_CLIENT = "ca-pub-2468238593433239"
AD_SLOT = "3961059137"
NO_ADS = {"protect-pdf", "password-generator", "jwt-decoder", "hash-generator"}
STRICT_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\' \'unsafe-inline\'; '
              'style-src \'self\' \'unsafe-inline\'; img-src \'self\' data: blob:; font-src \'self\'; connect-src \'none\'; '
              'object-src \'none\'; base-uri \'self\'; form-action \'none\'; frame-src \'none\'">')
AD_HOSTS = ("https://*.googlesyndication.com https://*.doubleclick.net https://*.google.com "
            "https://*.gstatic.com https://*.adtrafficquality.google https://adservice.google.co.in")
AD_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; '
          'script-src \'self\' \'unsafe-inline\' ' + AD_HOSTS + '; '
          'style-src \'self\' \'unsafe-inline\'; img-src \'self\' data: blob: https:; '
          'font-src \'self\' https://fonts.gstatic.com; connect-src ' + AD_HOSTS + '; '
          'object-src \'none\'; base-uri \'self\'; form-action \'none\'; frame-src ' + AD_HOSTS + '">')
AD_LOADER = ('<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client='
             + AD_CLIENT + '"')
AD_UNIT = 'data-ad-client="%s" data-ad-slot="%s"' % (AD_CLIENT, AD_SLOT)

ads_bad = 0
if "google.com, %s, DIRECT" % AD_CLIENT[3:] not in (ROOT / "ads.txt").read_text(encoding="utf-8"):
    fail("ads.txt does not name the publisher id the ad code uses (%s)" % AD_CLIENT)
    ads_bad += 1
with_ads = 0
for path in sorted(ROOT.glob("*.html")) + sorted((ROOT / "tools").glob("*.html")):
    text = path.read_text(encoding="utf-8")
    name = path.relative_to(ROOT).as_posix()
    if path.parent.name == "tools" and path.stem not in NO_ADS:      # the template too
        why = []
        if text.count(AD_CSP) != 1 or STRICT_CSP in text:
            why.append("its policy is not the one for ad pages")
        if text.count(AD_LOADER) != 1 or text.count("googlesyndication.com/pagead/js") != 1:
            why.append("it does not load the ad script exactly once")
        if text.count(AD_UNIT) != 1 or text.count('class="adsbygoogle"') != 1:
            why.append("it does not have exactly one ad unit with the right ids")
        notice = text.find("END: the check-the-result notice")
        slot = text.find('class="ad-slot"')
        info = text.find('<section class="tool-info')
        if not 0 <= notice < slot < info:
            why.append("the ad is not between the notice and the text below the tool")
        if why:
            fail("%s: %s" % (name, "; ".join(why)))
            ads_bad += 1
        with_ads += 1
    else:
        if "googlesyndication" in text or "adsbygoogle" in text:
            fail("%s must carry no advertising, and it does" % name)
            ads_bad += 1
        elif text.count(STRICT_CSP) != 1:
            fail("%s: a page without ads must keep the strict policy" % name)
            ads_bad += 1
if not ads_bad:
    ok("advertising: one unit below the tool on %d pages (template included), none on %s "
       "or the site pages, every policy exact" % (with_ads, ", ".join(sorted(NO_ADS))))
# ---- END: 4k. Advertising: where it is, and where it must never be ----

# ---- START: 5. No dead internal links ----
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
# ---- END: 5. No dead internal links ----

# ---- START: Report ----
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
# ---- END: Report ----
