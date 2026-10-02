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

  3. Every file input is attacked too. A file's NAME is attacker-controlled
     text just as much as a textarea is, and several tools put it on screen;
     and a tool that parses a file's bytes can be broken by a file built to
     break it. Each is given a picture named as markup, a corrupt PNG and a
     truncated JPEG, and must neither build an element nor throw.

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

# Every Chrome these scripts start is kept away from Google's ad servers.
# The test runs load each page hundreds of times a day; ad requests from
# them would be invalid traffic, which AdSense punishes, and would make
# the runs slow and depend on the network. Names that do not resolve fail
# at once, and the page carries on without its advertisement.
NO_ADS_FLAG = ("--host-resolver-rules="
               "MAP *.googlesyndication.com ~NOTFOUND, MAP *.doubleclick.net ~NOTFOUND, "
               "MAP *.adtrafficquality.google ~NOTFOUND, MAP adservice.google.com ~NOTFOUND, "
               "MAP adservice.google.co.in ~NOTFOUND, MAP fundingchoicesmessages.google.com ~NOTFOUND")
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
def probe_page(path, probe, tag, size="1280,900"):
    """Load `path` with `probe` injected, return what the probe wrote."""
    copy = path.with_name("_sec_" + path.name)
    profile = WORK / (tag + "_" + path.stem)
    html = path.read_text(encoding="utf-8")
    if "</body>" not in html:
        return "NO BODY"
    # The LAST </body>, never the first: a page that builds an HTML document in
    # its script (the markdown previewer) has "</body>" inside a string, and
    # putting the probe there cuts the string in half.
    head, closing, tail = html.rpartition("</body>")
    copy.write_text(head + probe + closing + tail,
                    encoding="utf-8", newline="\n")
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", NO_ADS_FLAG,
             "--user-data-dir=" + str(profile),
             "--window-size=" + size, "--virtual-time-budget=20000",
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


# ---- START: part 4, hand every file input a hostile file ------------------
FILE_PROBE = r"""
<div id="secverdict">not run</div>
<script>
(function () {
  window.__ran = "no";
  var errors = [];
  window.addEventListener("error", function (e) {
    if (String(e.message).indexOf("zq") > -1) { window.__ran = "YES"; }
    else { errors.push(String(e.message).slice(0, 70)); }
  });
  window.addEventListener("unhandledrejection", function (e) {
    errors.push("rejection: " + String(e.reason).slice(0, 60));
  });

  /* Nothing reaches the disk while a machine presses every button. */
  window.downloadBlob = function () {};
  window.downloadText = function () {};

  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function pressEverything() {
    Array.prototype.forEach.call(document.querySelectorAll("button"), function (b) {
      var label = (b.textContent || "") + " " + (b.id || "");
      if (/reset|clear|download|save/i.test(label)) { return; }
      try { b.click(); } catch (err) { errors.push("click: " + String(err).slice(0, 60)); }
    });
  }

  window.addEventListener("load", function () {
    var before = document.querySelectorAll("iframe, object, embed").length;

    var c = document.createElement("canvas");
    c.width = 64; c.height = 64;
    var ctx = c.getContext("2d");
    ctx.fillStyle = "#3366aa"; ctx.fillRect(0, 0, 64, 64);

    c.toBlob(function (blob) {
      /* A real, decodable picture - so the tool goes all the way through its
         normal path - with a name that is markup if anything treats it so. */
      feed(new File([blob], "<iframe onload=zq>.png", { type: "image/png" }));

      setTimeout(function () {
        pressEverything();

        /* The signature of a PNG and then nothing sensible. */
        var junk = new Uint8Array(300);
        var sig = [137, 80, 78, 71, 13, 10, 26, 10];
        for (var i = 0; i < junk.length; i++) { junk[i] = (i * 37 + 11) & 255; }
        for (var s = 0; s < 8; s++) { junk[s] = sig[s]; }
        feed(new File([junk], "broken.png", { type: "image/png" }));

        /* And a JPEG marker that promises a segment longer than the file. */
        setTimeout(function () {
          feed(new File([new Uint8Array([255, 216, 255, 225, 255, 255, 69, 120, 105, 102, 0, 0])],
                        "cut-short.jpg", { type: "image/jpeg" }));

          setTimeout(function () {
            var after = document.querySelectorAll("iframe, object, embed").length;
            document.getElementById("secverdict").textContent =
              "NEW_ELEMENTS=" + (after - before) + " HANDLER_RAN=" + window.__ran +
              " ERRORS=" + errors.length + (errors.length ? " (" + errors[0] + ")" : "");
          }, 700);
        }, 700);
      }, 900);
    }, "image/png");
  });
})();
</script>
"""


def check_tools_survive_hostile_files(tools):
    print("\n4. Every file input survives a hostile file")
    with_file = [p for p in tools if 'type="file"' in p.read_text(encoding="utf-8")]
    bad = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(probe_page, p, FILE_PROBE, "file"): p for p in with_file}
        for job in concurrent.futures.as_completed(jobs):
            page = jobs[job]
            verdict = job.result()
            if verdict.startswith(("TIMEOUT", "PROBE", "NO BODY")):
                fail("%s could not be given a file: %s" % (page.name, verdict))
                bad += 1
            elif ("NEW_ELEMENTS=0" not in verdict or "HANDLER_RAN=no" not in verdict
                  or "ERRORS=0" not in verdict):
                fail("%s: %s" % (page.name, verdict))
                bad += 1
    if not bad:
        ok("%d tools with a file input given a file named as markup, a "
           "corrupt PNG and a truncated JPEG - none built an element, none "
           "threw" % len(with_file))
# ---- END: part 4, hand every file input a hostile file --------------------


# ---- START: part 5, every page fits a phone's screen -----------------------
# Sixteen tool pages scrolled sideways on a 375px phone and nothing noticed:
# a bare 1fr grid column cannot be narrower than its widest content (the
# longest option of a <select>, a big rupee figure), so the panel pushed the
# page wider than the screen. A plain headless window cannot be made that
# narrow (Chrome keeps it at 485px or more, and it then caught only six of
# the sixteen), so each page is opened as a phone: Chrome's own device
# emulation, over the DevTools protocol, driven by Node's built-in
# WebSocket. 320px is the narrowest phone still sold; 375px the commonest.
# A table that scrolls inside its own .table-wrap is fine.
PHONE_JS = r"""
const { spawn } = require("child_process");
const fs = require("fs"), path = require("path"), os = require("os");
const [chrome, noAds, ...urls] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const port = 9400 + Math.floor(Math.random() * 400);
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "phone-"));
  const proc = spawn(chrome, ["--headless=new", noAds, "--remote-debugging-port=" + port, "--user-data-dir=" + prof,
                              "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
  let ver = null;
  for (let i = 0; i < 150 && !ver; i++) {
    try { ver = await (await fetch("http://127.0.0.1:" + port + "/json/version")).json(); } catch (e) { await sleep(100); }
  }
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) { const w = waiting.get(m.id); waiting.delete(m.id); m.error ? w.rej(new Error(JSON.stringify(m.error))) : w.res(m.result); }
  };
  const send = (method, params, sessionId) => new Promise((res, rej) => {
    const msg = { id: ++id, method, params: params || {} };
    if (sessionId) { msg.sessionId = sessionId; }
    waiting.set(msg.id, { res, rej });
    ws.send(JSON.stringify(msg));
  });
  try {
    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
    await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 }, sessionId);
    for (const width of [320, 375]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 2, mobile: true }, sessionId);
      for (const url of urls) {
        await send("Page.navigate", { url }, sessionId);
        await sleep(700);
        const r = await send("Runtime.evaluate", { returnByValue: true, expression: `(() => {
          const root = document.documentElement, vw = root.clientWidth, sw = root.scrollWidth;
          let worst = "", max = 0;
          if (sw > vw) {
            document.querySelectorAll("body *").forEach(el => {
              const b = el.getBoundingClientRect();
              if (b.width > 0 && b.right > vw + 1 && b.right > max) {
                max = b.right;
                worst = el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
                        (typeof el.className === "string" && el.className ? "." + el.className.split(" ").join(".") : "");
              }
            });
          }
          return sw > vw ? "WIDE: " + sw + "px on a " + vw + "px screen, " + worst : "FITS";
        })()` }, sessionId);
        console.log(JSON.stringify([width, url, r.result.value]));
      }
    }
  } finally {
    ws.close();
    proc.kill();
    await sleep(300);
    try { fs.rmSync(prof, { recursive: true, force: true }); } catch (e) { /* still held */ }
  }
})().catch(e => { console.log(JSON.stringify(["error", "", String(e && e.stack || e)])); process.exit(2); });
"""


def check_pages_fit_phone(pages):
    print("\n5. Every page fits a phone's screen")
    node = shutil.which("node")
    if not node:
        fail("Node is needed to open the pages as a phone, and it is not installed")
        return
    script = WORK / "phone.js"
    script.write_text(PHONE_JS, encoding="utf-8")
    out = subprocess.run([node, str(script), CHROME, NO_ADS_FLAG] + [p.as_uri() for p in pages],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200).stdout
    import json
    seen, bad = 0, 0
    for line in out.splitlines():
        width, url, verdict = json.loads(line)
        if width == "error":
            fail("the phone check could not run: %s" % verdict[:200])
            return
        seen += 1
        if verdict != "FITS":
            fail("%s at %dpx: %s" % (url.rsplit("/", 1)[-1], width, verdict))
            bad += 1
    if seen != 2 * len(pages):
        fail("the phone check saw %d of %d page loads" % (seen, 2 * len(pages)))
    elif not bad:
        ok("%d pages opened as a 320px and a 375px phone - none wider than the screen" % len(pages))
# ---- END: part 5, every page fits a phone's screen -------------------------


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
        ok("no file of ours makes a network call: connect-src is 'none', "
           "or Google's ad servers alone on a page with an advertisement")

    # A link out is only a link. A file LOADED from elsewhere runs in the page,
    # so exactly one is allowed: Google's ad script, on the pages check.py
    # lists as carrying an advertisement.
    AD_SCRIPT = ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"
                 "?client=ca-pub-2468238593433239")
    links, loaded, ad_pages = set(), [], 0
    for p in pages:
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'href="(?:https?:)?//([^/"]+)', text):
            if "108toolbox.in" not in m.group(1):
                links.add(m.group(1))
        for m in re.finditer(r'src="((?:https?:)?//[^"]+)"', text):
            if "108toolbox.in" in m.group(1):
                continue
            if m.group(1) == AD_SCRIPT:
                ad_pages += 1
            else:
                loaded.append("%s loads %s" % (p.name, m.group(1)[:80]))
    if loaded:
        fail("a third-party file is loaded: %s" % loaded[0])
    else:
        notes.append("pages link out to: %s" % ", ".join(sorted(links)))
        ok("no third-party file is loaded but Google's ad script, on %d tool pages "
           "(links out only: %s)" % (ad_pages, ", ".join(sorted(links)) or "none"))
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
        check_tools_survive_hostile_files(tools)
        check_pages_fit_phone(pages)
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
