#!/usr/bin/env python3
"""security.py - the third script, and the only one that attacks the site.

check.py reads the files. test_tools.py drives each tool the way a visitor
would. Neither asks the question this one asks: what happens when the text a
visitor types is HOSTILE?

That question had a real answer. json-formatter printed the browser's own
JSON.parse error into an innerHTML message box, and V8 quotes a piece of the
input back inside that error - so pasting

    <iframe onload=zq>

put a live iframe on the page and ran its handler. Eighteen characters, on a
page whose whole promise is that your text stays yours. Reading the code had
not found it; a machine typing an attack into every box did, in one pass.

So this runs in two parts.

  1. Every page loads clean. No JavaScript error, no Content-Security-Policy
     violation. This is what catches a CSP that quietly breaks a tool, which
     is the usual way a security header does harm.

  2. Every tool is attacked. Each text box is filled with a payload that runs
     if it is ever treated as markup, every button is pressed, and the DOM is
     then asked whether an element appeared that the page never wrote, or
     whether a handler from that payload ran.

It needs Chrome and takes about a minute. Run it before a deploy that touched
any tool's own code, and always after adding a tool.
"""
import concurrent.futures
import pathlib
import re
import shutil
import subprocess
import sys

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "_securitywork"

failures = []
notes = []


def fail(msg):
    failures.append(msg)
    print("  x  " + msg)


def ok(msg):
    print("  -  " + msg)


# ---- START: running one page in a real browser with a probe attached -------
def probe_page(path, probe, tag):
    """Load `path` with `probe` injected, return what the probe wrote."""
    copy = path.with_name("_sec_" + path.name)
    profile = WORK / (tag + "_" + path.stem)
    html = path.read_text(encoding="utf-8")
    if "</body>" not in html:
        return "NO BODY"
    copy.write_text(html.replace("</body>", probe + "</body>"),
                    encoding="utf-8", newline="\n")
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + str(profile),
             "--window-size=1280,900", "--virtual-time-budget=20000",
             "--dump-dom", copy.as_uri()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120).stdout
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    finally:
        copy.unlink(missing_ok=True)
        shutil.rmtree(profile, ignore_errors=True)

    found = re.search(r'<div id="secverdict">(.*?)</div>', dom, re.S)
    return found.group(1).strip() if found else "PROBE DID NOT RUN"
# ---- END: running one page in a real browser with a probe attached ---------


# ---- START: part 1, every page loads without an error or a CSP violation ---
CLEAN_PROBE = r"""
<div id="secverdict">not run</div>
<script>
(function () {
  var problems = [];
  window.addEventListener("error", function (e) {
    problems.push("js: " + (e.message || "error"));
  });
  document.addEventListener("securitypolicyviolation", function (e) {
    /* The directive matters more than the URL: it says which exit was shut. */
    problems.push("csp: " + e.violatedDirective + " blocked " +
                  String(e.blockedURI).slice(0, 60));
  });
  window.addEventListener("load", function () {
    setTimeout(function () {
      document.getElementById("secverdict").textContent =
        problems.length ? problems.slice(0, 4).join(" | ") : "CLEAN";
    }, 500);
  });
})();
</script>
"""


def check_pages_load_clean(pages):
    print("\n1. Every page loads with no error and no policy violation")
    bad = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(probe_page, p, CLEAN_PROBE, "load"): p for p in pages}
        for job in concurrent.futures.as_completed(jobs):
            page = jobs[job]
            verdict = job.result()
            if verdict != "CLEAN":
                fail("%s: %s" % (page.name, verdict))
                bad += 1
    if not bad:
        ok("%d pages, every one silent - no script error, no CSP violation"
           % len(pages))
# ---- END: part 1, every page loads without an error or a CSP violation -----


# ---- START: part 2, type an attack into every box on every tool ------------
ATTACK_PROBE = r"""
<div id="secverdict">not run</div>
<script>
(function () {
  window.__ran = "no";
  window.addEventListener("error", function (e) {
    if (String(e.message).indexOf("zq") > -1) { window.__ran = "YES"; }
  });

  /* Nothing reaches the disk while a machine mashes every button. */
  window.downloadBlob = function () {};
  window.downloadText = function () {};

  function fill(payload) {
    var fields = document.querySelectorAll(
      "textarea, input[type=text], input[type=search], input[type=url], input:not([type])");
    Array.prototype.forEach.call(fields, function (f) {
      f.value = payload;
      f.dispatchEvent(new Event("input", { bubbles: true }));
      f.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  window.addEventListener("load", function () {
    /* Eighteen characters on purpose. Short enough that a message quoting the
       input back keeps the whole tag, and an iframe's onload fires even when
       the element arrives as markup rather than as a real page. */
    var PAYLOAD = "<iframe onload=zq>";
    var before = document.querySelectorAll("iframe, object, embed").length;

    fill(PAYLOAD);
    Array.prototype.forEach.call(document.querySelectorAll("button"), function (b) {
      var label = (b.textContent || "") + " " + (b.id || "");
      /* Reset would wipe the payload before the page had done anything with
         it, and download has nowhere useful to go. */
      if (/reset|clear|download/i.test(label)) { return; }
      fill(PAYLOAD);
      try { b.click(); } catch (err) {}
    });

    setTimeout(function () {
      var after = document.querySelectorAll("iframe, object, embed").length;
      document.getElementById("secverdict").textContent =
        "NEW_ELEMENTS=" + (after - before) + " HANDLER_RAN=" + window.__ran;
    }, 500);
  });
})();
</script>
"""


def check_tools_reject_markup(tools):
    print("\n2. No tool turns hostile text into markup")
    bad = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(probe_page, p, ATTACK_PROBE, "xss"): p for p in tools}
        for job in concurrent.futures.as_completed(jobs):
            page = jobs[job]
            verdict = job.result()
            if verdict.startswith(("TIMEOUT", "PROBE", "NO BODY")):
                fail("%s could not be attacked: %s" % (page.name, verdict))
                bad += 1
            elif "NEW_ELEMENTS=0" not in verdict or "HANDLER_RAN=no" not in verdict:
                fail("%s: %s" % (page.name, verdict))
                bad += 1
    if not bad:
        ok("%d tools attacked in every text box - none of them built an "
           "element out of it" % len(tools))
# ---- END: part 2, type an attack into every box on every tool --------------


# ---- START: part 3, the static promises that keep the exits shut -----------
def check_policy_present(pages):
    print("\n3. The policy is on every page")
    missing_csp = [p.name for p in pages
                   if "Content-Security-Policy" not in p.read_text(encoding="utf-8")]
    if missing_csp:
        fail("%d page(s) carry no Content-Security-Policy, e.g. %s"
             % (len(missing_csp), missing_csp[0]))
    else:
        ok("all %d pages declare a Content-Security-Policy" % len(pages))

    # connect-src 'none' is the directive doing the real work here: no tool on
    # this site makes a network call, so shutting the door costs nothing and
    # means injected code cannot send anybody's text anywhere.
    talkers = []
    for folder in ("tools", "js"):
        for f in sorted((ROOT / folder).glob("*.*")):
            if re.search(r"\bfetch\(|XMLHttpRequest|sendBeacon|new WebSocket"
                         r"|EventSource|importScripts",
                         f.read_text(encoding="utf-8")):
                talkers.append(f.name)
    if talkers:
        fail("connect-src is 'none' but %s makes a network call" % talkers[0])
    else:
        ok("no file makes a network call, so connect-src 'none' holds")

    outside = set()
    for p in pages:
        for m in re.finditer(r'(?:src|href)="(?:https?:)?//([^/"]+)',
                             p.read_text(encoding="utf-8")):
            if "108toolbox.in" not in m.group(1):
                outside.add(m.group(1))
    if outside:
        notes.append("pages link out to: %s" % ", ".join(sorted(outside)))
        ok("no third-party file is loaded (links out only: %s)"
           % ", ".join(sorted(outside)))
    else:
        ok("no third-party file is loaded and nothing links outside")
# ---- END: part 3, the static promises that keep the exits shut -------------


if __name__ == "__main__":
    pages = sorted(ROOT.glob("*.html")) + sorted(
        p for p in (ROOT / "tools").glob("*.html") if not p.name.startswith("_"))
    registry = (ROOT / "js" / "tools-data.js").read_text(encoding="utf-8")
    slugs = re.findall(r'slug: "([^"]+)"', registry)
    tools = [ROOT / "tools" / (s + ".html") for s in slugs]

    WORK.mkdir(parents=True, exist_ok=True)
    try:
        check_policy_present(pages)
        check_pages_load_clean(pages)
        check_tools_reject_markup(tools)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        for leftover in list(ROOT.glob("_sec_*.html")) + list(
                (ROOT / "tools").glob("_sec_*.html")):
            leftover.unlink(missing_ok=True)

    print("\n" + "=" * 62)
    if failures:
        print("%d SECURITY PROBLEM(S). Fix these before deploying." % len(failures))
        sys.exit(1)
    print("SECURITY CLEAN - %d pages load silently, %d tools refuse to turn "
          "text into markup." % (len(pages), len(tools)))
