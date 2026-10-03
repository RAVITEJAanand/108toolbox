#!/usr/bin/env python3
"""
108 ToolBox — browser test suite.

  Windows:   python test_tools.py
  One tool:  python test_tools.py gst-calculator

`check.py` reads the files. This one RUNS them. It opens every tool page in
real headless Chrome, drives the real controls, and compares what the page
prints against values worked out independently. Rule 4 in CLAUDE.md exists
because the two are not the same job: a tool whose JavaScript throws on the
first keystroke still has a perfect title, a valid canonical and clean JSON-LD.

It fails if a registered tool has no test at all, so coverage cannot quietly
rot as tools are added.

Each test page is written to tools/_test-<slug>.html and deleted straight
after. check.py skips files starting with "_", so a crash mid-run leaves
nothing that breaks the next validation.
"""

import concurrent.futures
import functools
import http.server
import pathlib
import re
import subprocess
import sys
import threading
import urllib.parse
import html as htmllib

# ---- START: where Chrome is, how it starts with no ads, and how many pages run at once ----
SITE = pathlib.Path(__file__).parent
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

# Four at a time. Chrome is heavy and the machine still has to be usable.
WORKERS = 4
# ---- END: where Chrome is, how it starts with no ads, and how many pages run at once ----


# ===== START: the in-page harness ==========================================
# Every helper any test body uses lives here. The bodies below were written
# against several older harnesses, so this one is deliberately the union of
# all of them rather than the smallest set that would do.
HARNESS = r"""
<script>
(function () {
  /* ---- START: what the harness keeps: the results, and a heartbeat that paces the clock ---- */
  var results = [];
  var done = false;
  /* Headless Chrome runs on a virtual clock that jumps to the next timer whenever
     the page is idle. Waiting for a blob to be read or an image to decode is
     real work on another thread, and to the page it looks idle - so the clock
     leapt straight to the test's overall time limit and the test was declared
     stuck while the read was a millisecond from finishing. A timer that ticks
     every millisecond makes the clock move in small steps instead, which gives
     that work the real time it needs. It stops when the test finishes. */
  /* The heartbeat also spends a little real time on each tick. Without it
     the clock still ran about a hundred times faster than work on other
     threads (decoding and encoding pictures, pdf.js's worker): on a busy
     machine a 120-second wait in compress-pdf ran out while a photo was
     still being encoded, about one run in three under load, and the heavy
     image and PDF tests could do the same. Pacing every tick keeps the
     clock closer to real time for every test at once; a quick test barely
     notices, since it finishes in a few hundred virtual milliseconds. */
  var heartbeat = setInterval(function () {
    var x = 0;
    for (var i = 0; i < 100000; i++) { x = (x + i) % 9973; }
    window.__heartbeat = x;
  }, 1);
  /* ---- END: what the harness keeps ---- */

  /* ---- START: recording a result ---- */
  function record(pass, line) {
    results.push((pass ? "PASS  " : "FAIL  ") + line);
  }
  function check(name, got, want) {
    var pass = JSON.stringify(got) === JSON.stringify(want);
    record(pass, name + (pass ? "" :
      "\n        got:  " + JSON.stringify(got) +
      "\n        want: " + JSON.stringify(want)));
  }
  function ok(name, condition, detail) {
    record(!!condition, name +
      (condition ? "" : "\n        " + (detail || "condition was false")));
  }
  function rec(pass, label, got, want) {
    record(!!pass, label +
      (pass ? "" : "\n        got:  " + got + "\n        want: " + want));
  }
  function eq(label, actual, expected) {
    rec(String(actual) === String(expected), label, actual, expected);
  }
  function has(label, haystack, needle) {
    rec(String(haystack).indexOf(needle) > -1, label,
        String(haystack).slice(0, 80), "contains " + needle);
  }
  function n(text) {
    /* U+2212 MINUS SIGN is not ASCII "-". Tools print it because it is the
       correct typographic minus, and a test that simply stripped it would
       read a temperature of -40 as 40 and then pass on the wrong number. */
    return parseFloat(String(text)
      .replace(new RegExp(String.fromCharCode(0x2212), "g"), "-")
      .replace(/[^0-9.-]/g, ""));
  }
  function near(label, actual, expected, tol) {
    var a = n(actual);
    rec(isFinite(a) && Math.abs(a - expected) <= (tol || 0.01),
        label, a, expected);
  }
  /* ---- END: recording a result ---- */

  /* ---- START: reading the page ---- */
  function txt(id) { return document.getElementById(id).textContent; }
  function val(id) { return document.getElementById(id).value; }
  function out() { return document.getElementById("output").textContent; }
  function rows() { return document.querySelectorAll("#rows tr"); }
  function cell(r, c) { return rows()[r].children[c].textContent; }

  /* .stats is display:grid, which beats the browser rule for the hidden
     attribute, so "is it hidden" has to ask the computed style too. */
  function gone(label, id) {
    var e = document.getElementById(id);
    rec(e.hidden === true && getComputedStyle(e).display === "none", label,
        "hidden=" + e.hidden + " display=" + getComputedStyle(e).display,
        "hidden and display none");
  }
  function shown(label, id) {
    var e = document.getElementById(id);
    rec(e.hidden === false && getComputedStyle(e).display !== "none", label,
        "hidden=" + e.hidden + " display=" + getComputedStyle(e).display,
        "visible");
  }
  /* ---- END: reading the page ---- */

  /* ---- START: driving the page ---- */
  /* Fire both events: some tools listen for input, some for change, and a
     test should not have to know which. */
  function set(id, value) {
    var el = document.getElementById(id);
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function tick(id, on) {
    var el = document.getElementById(id);
    el.checked = on;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function click(id) { document.getElementById(id).click(); }
  /* ---- END: driving the page ---- */

  /* ---- START: images and waiting ---- */
  /* Build a real PNG and hand it to a file input the same way a drop does,
     so the tool runs its whole decode and re-encode path, not a stub. */
  function makeImage(id, w, h, then) {
    var canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    var ctx = canvas.getContext("2d");
    for (var x = 0; x < w; x += 8) {
      for (var y = 0; y < h; y += 8) {
        ctx.fillStyle = "rgb(" + (x % 256) + "," + (y % 256) + ",128)";
        ctx.fillRect(x, y, 8, 8);
      }
    }
    canvas.toBlob(function (blob) {
      var file = new File([blob], "test.png", { type: "image/png" });
      var dt = new DataTransfer();
      dt.items.add(file);
      var input = document.getElementById(id);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      then(file);
    }, "image/png");
  }

  /* Canvas decode and encode are asynchronous. A fixed setTimeout is a guess
     that passes on a fast machine and fails on a slow one; this waits for the
     real thing and reports honestly if it never arrives. */
  function waitFor(label, test, then, budgetMs) {
    var waited = 0;
    var step = 100;
    (function poll() {
      if (test()) { ok(label, true); then(); return; }
      waited += step;
      if (waited >= (budgetMs || 15000)) {
        ok(label, false, "timed out after " + waited + "ms");
        then();
        return;
      }
      setTimeout(poll, step);
    })();
  }
  /* ---- END: images and waiting ---- */

  /* ---- START: finishing ---- */
  function finish() {
    if (done) return;
    done = true;
    clearInterval(heartbeat);
    var pre = document.createElement("pre");
    pre.id = "RESULTS";
    pre.textContent = "\n>>> __SLUG__\n" + results.join("\n") + "\n<<< end\n";
    document.body.appendChild(pre);
  }

  /* A body that hangs must still report. Without this the run looks like a
     page that produced nothing, which reads as a missing test rather than a
     stuck one. */
  setTimeout(function () {
    if (!done) { record(false, "test body never finished - timed out"); }
    finish();
  }, __BUDGET__);

  /* Any uncaught error anywhere on the page is itself a failure */
  window.addEventListener("error", function (e) {
    record(false, "page threw: " + e.message + " @ " + (e.filename || "?") +
           ":" + e.lineno);
  });
  window.addEventListener("unhandledrejection", function (e) {
    record(false, "unhandled rejection: " + e.reason);
  });
  /* ---- END: finishing ---- */

  /* ---- START: running the tool's own test body ---- */
  try {
    __TESTS__
  } catch (e) {
    record(false, "threw: " + (e && e.message ? e.message : e));
    finish();
  }
  /* ---- END: running the tool's own test body ---- */
})();
</script>
"""
# ===== END: the in-page harness ============================================


# ===== START: what every tool is checked for ===============================
# Runs before each tool's own body, on all of them. check.py can see that the
# check-the-result notice is in the markup; only a browser can say whether it
# is on the screen. A display:none in a stylesheet, a parent that collapsed to
# zero height, a colour that matched the background - each of those leaves a
# page that passes check.py and shows the visitor nothing.
#
# It does not call finish(); the tool's own body does that.
COMMON = r"""
    /* ---- START: the check-the-result notice ---- */
    (function () {
      var note = document.querySelector(".tool-warn");
      ok("the check-the-result notice is on the page", !!note,
         "no element with class tool-warn");
      if (!note) { return; }
      var box = note.getBoundingClientRect();
      var style = getComputedStyle(note);
      ok("and it is really on the screen, not just in the markup",
         box.width > 0 && box.height > 0 && style.visibility !== "hidden" &&
         style.display !== "none" && Number(style.opacity) > 0,
         "w=" + Math.round(box.width) + " h=" + Math.round(box.height) +
         " display=" + style.display + " visibility=" + style.visibility +
         " opacity=" + style.opacity);
      has("and it tells the reader to check the result", note.textContent,
          "Check the result before you rely on it");
      ok("and it links to the full disclaimer",
         !!note.querySelector("a[href$='disclaimer.html']"),
         "the notice has no link to disclaimer.html");
    })();
    /* ---- END: the check-the-result notice ---- */

"""
# ===== END: what every tool is checked for =================================


# ===== START: the test bodies ==============================================
# One entry per tool, keyed by slug. A body drives the page and calls finish()
# when it is done; the generator appends finish() to any body that does not.
T = {}

# ---- START: the test for add-line-numbers ----
T["add-line-numbers"] = r"""
    set("input", "a\nb\nc");
    check("basic numbering", out(), "1. a\n2. b\n3. c");
    check("lines counted", txt("cLines"), "3");
    check("numbered counted", txt("cNumbered"), "3");
    check("last number", txt("cLast"), "3");

    /* Ten lines means the numbers need two columns */
    var ten = [];
    for (var i = 0; i < 10; i++) ten.push("x");
    set("input", ten.join("\n"));
    check("zero padded to width", out().split("\n")[0], "01. x");
    check("last line not padded", out().split("\n")[9], "10. x");

    tick("optPad", false);
    check("padding off", out().split("\n")[0], "1. x");
    tick("optPad", true);

    set("input", "a\nb\nc");
    set("start", "5");
    set("step", "5");
    check("start and step", out(), "05. a\n10. b\n15. c");
    set("start", "1");
    set("step", "1");

    set("input", "a\n\nb");
    check("empty line skipped", out(), "1. a\n\n2. b");
    check("only real lines numbered", txt("cNumbered"), "2");
    tick("optSkipEmpty", false);
    check("empty line numbered when asked", out(), "1. a\n2. \n3. b");
    tick("optSkipEmpty", true);

    set("input", "a\nb");
    set("sep", "bracket");
    check("bracket format", out(), "[1] a\n[2] b");
    set("sep", "tab");
    check("tab format", out(), "1\ta\n2\tb");
    set("sep", "dot");

    /* Renumbering something that arrived already numbered */
    set("input", "5) first\n7) second");
    tick("optStrip", true);
    check("existing numbering replaced", out(), "1. first\n2. second");

    set("input", "1998 was a good year");
    check("a bare year is not numbering", out(), "1. 1998 was a good year");
    tick("optStrip", false);

    set("input", "a\nb");
    set("start", "-2");
    check("negative start keeps its sign", out(), "-2. a\n-1. b");
    set("start", "1");

    set("input", "");
    check("empty input gives nothing", out(), "");
    check("last number shows a dash", txt("cLast"), String.fromCharCode(0x2014));
    finish();
"""
# ---- END: the test for add-line-numbers ----

# ---- START: the test for age-calculator ----
T["age-calculator"] = r"""
    set("dob", "2000-01-01");
    set("asOf", "2026-01-01");
    ok("26 years old", txt("mainAge").indexOf("26") > -1, txt("mainAge"));
    set("asOf", "2025-12-31");
    ok("a day early is 25", txt("mainAge").indexOf("25") > -1, txt("mainAge"));
    set("dob", "2026-01-01"); set("asOf", "2000-01-01");
    ok("a future birth date is refused",
       document.getElementById("msg").textContent.length > 0);
    set("dob", "2000-02-29"); set("asOf", "2026-03-01");
    ok("leap day birthday does not crash", txt("mainAge").length > 0, txt("mainAge"));
    finish();
"""
# ---- END: the test for age-calculator ----

# ---- START: the test for average-calculator ----
T["average-calculator"] = r"""
    set("input", "12, 7, 19, 3, 7, 22, 15");
    check("mean", txt("mean"), "12.14");
    check("median", txt("median"), "12");
    check("mode", txt("mode"), "7");
    check("count", txt("count"), "7");
    check("sum", txt("sum"), "85");
    check("smallest", txt("min"), "3");
    check("largest", txt("max"), "22");
    check("range", txt("range"), "19");

    /* An even count averages the two middle values */
    set("input", "1 2 3 4");
    check("median of an even list", txt("median"), "2.5");

    set("input", "1\n2\n3");
    check("new lines work", txt("mean"), "2");
    set("input", "1;2;3");
    check("semicolons work", txt("mean"), "2");
    set("input", "1\t2\t3");
    check("tabs work", txt("mean"), "2");

    set("input", "-5, 5");
    check("negatives work", txt("mean"), "0");
    set("input", "1.5, 2.5");
    check("decimals work", txt("mean"), "2");

    set("input", "1, 2, 3");
    check("no repeat means no mode", txt("mode"), "none");
    set("input", "1, 1, 2, 2");
    check("two modes are listed", txt("mode"), "1, 2");

    /* Standard deviation of 2,4,4,4,5,5,7,9 is exactly 2 */
    set("input", "2,4,4,4,5,5,7,9");
    check("standard deviation", txt("sd"), "2");

    set("places", "0");
    check("decimal places respected", txt("mean"), "5");
    set("places", "2");

    set("input", "10, apple, 20");
    check("text is skipped", txt("mean"), "15");
    check("and reported", document.getElementById("msg").textContent.indexOf("skipped") > -1, true);

    /* "apple" was caught only because it has an e in it. A word without
       one vanished: "5, abc, 7" counted two numbers and said nothing. */
    set("input", "5, abc, 7");
    check("a word with no e is counted out", txt("count"), "2");
    check("and named", txt("msg"), "1 entry is not a number and was skipped: abc.");
    set("input", "Rs.500, 1.2.3, 5-3, Q1, 2026-10-03, 4");
    check("what is not one number is never half-read", txt("count"), "1");
    check("each is named", txt("msg"),
          "5 entries are not numbers and were skipped: Rs.500, 1.2.3, 5-3, Q1, 2026-10-03.");
    set("input", String.fromCharCode(0x20B9) + "500, 50%, 3.5kg, (2), +1, .5, 1e3, " + String.fromCharCode(0x2212) + "4");
    check("a symbol before or a unit after still counts", txt("count"), "8");
    check("and is read right, typographic minus included", txt("sum"), "1,553");
    check("with nothing reported", txt("msg"), "");
    set("input", "3.5 kg, 4 kg, 5 kg");
    check("a unit after a space is named once", txt("msg"), "3 entries are not numbers and were skipped: kg.");
    check("while the numbers count", txt("count"), "3");
    set("input", "1 a b c d e f g 2");
    check("a long list of words is cut short", txt("msg"),
          "7 entries are not numbers and were skipped: a, b, c, d, e and 2 more.");
    set("input", "1, " + "x".repeat(40));
    check("a long entry is cut short too", txt("msg"),
          "1 entry is not a number and was skipped: " + "x".repeat(24) + String.fromCharCode(0x2026) + ".");
    set("input", "<img src=x onerror=zq>, 1");
    check("a skipped entry is shown as text, never as markup",
          document.getElementById("msg").children.length, 0);

    /* Both standard deviations: 2,4,4,4,5,5,7,9 has sigma 2 and
       s = sqrt(32 / 7) = 2.138... */
    set("input", "2,4,4,4,5,5,7,9");
    check("population standard deviation", txt("sd"), "2");
    check("sample standard deviation", txt("sdSample"), "2.14");
    set("input", "42");
    check("one number has a population spread of 0", txt("sd"), "0");
    check("and no sample spread at all", txt("sdSample"), String.fromCharCode(0x2014));

    set("input", "");
    check("empty shows a dash", txt("mean"), String.fromCharCode(0x2014));
    check("and zero count", txt("count"), "0");

    /* A value that collides with a built-in property name */
    set("input", "1, 1, 2");
    check("mode still works", txt("mode"), "1");
    finish();
"""
# ---- END: the test for average-calculator ----

# ---- START: the test for bmi-calculator ----
T["bmi-calculator"] = r"""
    set("cm", "170"); set("kg", "65");
    check("metric BMI", txt("bmi"), "22.5");
    check("healthy label", txt("category"), "Healthy weight");

    set("kg", "80");
    check("overweight by WHO", txt("category"), "Overweight");

    set("kg", "65");
    tick("optAsian", true);
    check("same BMI, Asian cutoffs", txt("bmi"), "22.5");
    check("still healthy at 22.5", txt("category"), "Healthy weight");
    set("kg", "68");
    check("BMI 23.5 is overweight for Asian cutoffs", txt("category"), "Overweight");
    tick("optAsian", false);
    check("but healthy on WHO cutoffs", txt("category"), "Healthy weight");

    set("kg", "45");
    check("underweight", txt("category"), "Underweight");
    set("kg", "100");
    check("obese", txt("category"), "Obese");

    /* 5 ft 7 in and 143 lb is the same person as 170 cm / 65 kg */
    set("units", "imperial");
    set("ft", "5"); set("inch", "7"); set("lb", "143");
    check("imperial agrees with metric", txt("bmi"), "22.4");
    check("imperial healthy range in lb", txt("healthyRange").indexOf("lb") > -1, true);

    set("units", "metric");
    set("cm", "170"); set("kg", "65");
    check("healthy range in kg", txt("healthyRange"), "53.5 to 72.3 kg");
    check("inside the range", txt("toChange"), "inside it");
    set("kg", "80");
    check("distance above", txt("toChange"), "7.8 kg above");

    set("cm", ""); set("kg", "");
    check("empty input shows a dash", txt("bmi"), String.fromCharCode(0x2014));
        /* -1 ft and 80 in used to make 68 inches and a BMI beside it. */
    var DASH = String.fromCharCode(0x2014);
    set("units", "imperial"); set("ft", "-1"); set("inch", "80"); set("lb", "150");
    eq("a minus height gives no BMI", txt("bmi"), DASH);
    has("and says why", txt("category"), "cannot be below zero");
    set("ft", "5"); set("inch", "8");
    ok("an ordinary height gives one", txt("bmi") !== DASH, txt("bmi"));
    set("units", "metric"); set("cm", "170"); set("kg", "-65");
    eq("a minus weight is refused too", txt("bmi"), DASH);
    set("kg", "65");
    eq("and a real one works again", txt("bmi"), "22.5");
    finish();
"""
# ---- END: the test for bmi-calculator ----

# ---- START: the test for case-converter ----
T["case-converter"] = r"""
    set("input", "hello world example");
    document.querySelector("[data-mode='upper']").click();
    check("uppercase", txt("output"), "HELLO WORLD EXAMPLE");
    document.querySelector("[data-mode='lower']").click();
    check("lowercase", txt("output"), "hello world example");
    document.querySelector("[data-mode='title']").click();
    check("title case", txt("output"), "Hello World Example");
    document.querySelector("[data-mode='camel']").click();
    check("camelCase", txt("output"), "helloWorldExample");
    document.querySelector("[data-mode='snake']").click();
    check("snake_case", txt("output"), "hello_world_example");
    document.querySelector("[data-mode='kebab']").click();
    check("kebab-case", txt("output"), "hello-world-example");
    set("input", "hello. world here.");
    document.querySelector("[data-mode='sentence']").click();
    check("sentence case", txt("output"), "Hello. World here.");
        /* An identifier is letters and digits. Splitting at spaces alone kept
       the punctuation: "don't stop, NASA." became don't_stop,_nasa. */
    var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    function mode(m) { document.querySelector("[data-mode='" + m + "']").click(); return txt("output"); }
    set("input", "don't stop, NASA. It" + String.fromCharCode(0x2019) + "s 3.5 km! " + TE + " " + String.fromCharCode(0xD83D, 0xDE00));
    check("snake_case keeps letters and digits only", mode("snake"), "dont_stop_nasa_its_3_5_km_" + TE);
    check("kebab-case too", mode("kebab"), "dont-stop-nasa-its-3-5-km-" + TE);
    check("camelCase too", mode("camel"), "dontStopNasaIts35Km" + TE);
    set("input", "XMLHttpRequest parseJSONData");
    check("a run of capitals is one word", mode("snake"), "xml_http_request_parse_json_data");
    set("input", "user_first-name.value");
    check("snake, kebab and dots all come apart", mode("camel"), "userFirstNameValue");
    finish();
"""
# ---- END: the test for case-converter ----

# ---- START: the test for character-frequency-counter ----
T["character-frequency-counter"] = r"""
    function rows() {
      return Array.prototype.map.call(
        document.querySelectorAll("#rows tr"),
        function (tr) {
          return Array.prototype.map.call(tr.children, function (td) {
            return td.textContent;
          });
        });
    }

    set("input", "hello");
    check("most common first", rows()[0], ["l", "2", "40.0%"]);
    check("four different characters", txt("cUnique"), "4");
    check("five counted", txt("cTotal"), "5");
    check("top tile", txt("cTop"), "l");
    check("ties are alphabetical", rows()[1][0], "e");

    set("input", "Aa");
    check("case folded by default", rows()[0], ["a", "2", "100.0%"]);
    tick("optCase", false);
    check("case kept when asked", rows().length, 2);
    tick("optCase", true);

    set("input", "a b");
    check("spaces ignored by default", txt("cTotal"), "2");
    tick("optSpaces", false);
    check("space counted when asked", txt("cTotal"), "3");
    var labels = rows().map(function (r) { return r[0]; });
    check("space is labelled", labels.indexOf("(space)") > -1, true);
    tick("optSpaces", true);

    set("input", "a.b.");
    check("punctuation counted by default", txt("cUnique"), "3");
    tick("optPunct", true);
    check("punctuation ignored when asked", txt("cUnique"), "2");
    tick("optPunct", false);

    set("mode", "words");
    set("input", "the cat the dog");
    check("word mode counts words", rows()[0], ["the", "2", "50.0%"]);
    check("four words counted", txt("cTotal"), "4");

    set("input", "word, word (word)");
    check("punctuation splits words apart", rows()[0][1], "1");
    tick("optPunct", true);
    check("edge punctuation stripped", rows()[0], ["word", "3", "100.0%"]);
    tick("optPunct", false);

    /* Visitor text must never be treated as markup */
    set("input", "<b>x</b>");
    check("markup shown as text", rows()[0][0], "<b>x</b>");
    check("no element was created", document.querySelectorAll("#rows b").length, 0);

    set("mode", "chars");
    set("input", "ab" + String.fromCodePoint(0x1F600));
    check("emoji counts as one character", txt("cTotal"), "3");

    /* A word that collides with a built-in object property */
    set("mode", "words");
    set("input", "constructor constructor toString");
    check("built-in names are safe", rows()[0], ["constructor", "2", "66.7%"]);

    set("mode", "chars");
    set("input", "");
    check("empty input gives no rows", rows().length, 0);
    check("top tile shows a dash", txt("cTop"), String.fromCharCode(0x2014));

    set("input", "hello");
    var threw = "";
    try { document.getElementById("copyBtn").click(); } catch (e) { threw = String(e.message); }
    check("copy button does not throw", threw, "");
        var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    var THUMB = String.fromCharCode(0xD83D, 0xDC4D, 0xD83C, 0xDFFD);
    set("mode", "chars"); tick("optSpaces", true); tick("optCase", true);
    tick("optPunct", false);
    set("input", "a" + THUMB + "a");
    eq("a thumb and its skin tone are one character", txt("cUnique"), "2");
    /* Every Telugu letter used to count as punctuation, so this box
       deleted Telugu text. */
    tick("optPunct", true);
    set("input", TE + "!");
    eq("ignoring punctuation keeps Telugu", txt("cTotal"), "3");
    has("a letter with its vowel sign is one row", txt("rows"), String.fromCharCode(0x0C24, 0x0C46));
    set("mode", "words");
    set("input", TE + ", " + TE + ".");
    eq("and in words, a Telugu word survives its comma", txt("cUnique"), "1");
    eq("twice", txt("cTotal"), "2");
    tick("optPunct", false); set("mode", "chars");
    finish();
"""
# ---- END: the test for character-frequency-counter ----

# ---- START: the test for compound-interest-calculator ----
T["compound-interest-calculator"] = r"""
  near("100000 at 8% for 10y yearly", txt("finalOut"), 215892.50, 0.5);
  near("interest earned", txt("earned"), 115892.50, 0.5);
  near("simple interest comparison", txt("simple"), 80000);
  near("compounding edge", txt("edge"), 35892.50, 0.5);
  eq("ten rows", rows().length, 10);
  near("row 1 balance", cell(0, 2), 108000);
  near("row 1 interest", cell(0, 1), 8000);
  near("row 10 balance", cell(9, 2), 215892.50, 0.5);
  has("doubling time is exact", txt("doubleNote"), "9.01");

  set("freq", "12");
  near("monthly compounding", txt("finalOut"), 221964.02, 1);

  set("freq", "365");
  near("daily compounding", txt("finalOut"), 222534.58, 1);

  set("freq", "1"); set("rate", 0);
  near("zero rate stays flat", txt("finalOut"), 100000);
  has("zero rate never doubles", txt("doubleNote"), "never");

  set("rate", 8); set("years", 1);
  near("one year equals simple interest", txt("edge"), 0, 0.01);

  set("years", 0);
  near("zero years returns principal", txt("finalOut"), 100000);
  eq("zero years builds no table", rows().length, 0);

  set("years", 10); set("principal", "");
  near("empty principal does not crash", txt("finalOut"), 0);
        /* 0.1 years was printed as "0 years", compounding "added ₹-27.42",
       and a minus rate was quietly worked out as 0%. */
    var R = String.fromCharCode(0x20B9), M = String.fromCharCode(0x2212), DASH = String.fromCharCode(0x2014);
    set("principal", "100000"); set("rate", "8"); set("freq", "1"); set("years", "0.1");
    has("a tenth of a year is written as one", txt("formula"), "over 0.1 years at");
    /* Worked in Python: 100000 x 1.08^0.1 - 100000 - 800 = -27.42 */
    eq("part of a period pays less than simple interest", txt("edge"), R + "27.42");
    eq("and the label says so the right way round",
       document.getElementById("edge").nextElementSibling.textContent, "Compounding pays less by");
    set("years", "2.5");
    has("2.5 years stays 2.5", txt("formula"), "over 2.5 years at");
    eq("over a whole period the label is the usual one",
       document.getElementById("edge").nextElementSibling.textContent, "Compounding adds");
    set("years", "1");
    has("one year is a year", txt("formula"), "over 1 year at");

    set("years", "10"); set("rate", "-5");
    /* Worked in Python: 100000 x 0.95^10 = 59,873.69 */
    near("a minus rate is worked out, not read as 0%", txt("finalOut"), 59873.69, 0.01);
    has("as money that shrinks", txt("formula"), "shrinks to");
    eq("the loss reads with its minus first", txt("earned").charAt(0) + txt("earned").charAt(1), M + R);
    has("and the note says it shrinks", txt("doubleNote"), "shrinks every year");
    set("rate", "-100");
    eq("-100% is refused", txt("finalOut"), DASH);
    set("rate", "8"); set("principal", "-5");
    eq("so is a minus principal", txt("finalOut"), DASH);
    has("with the reason", txt("formula"), "cannot be below zero");
    set("principal", "100000");
    near("and back", txt("finalOut"), 215892.5, 0.01);
      set("principal", "1000000"); set("rate", "8"); set("years", "10");
  ok("rupees are grouped in lakhs", /^\d{1,2}(,\d\d)+,\d{3}(\.\d\d)?$/.test(txt("finalOut").slice(1)), txt("finalOut"));
  finish();
"""
# ---- END: the test for compound-interest-calculator ----

# ---- START: the test for discount-calculator ----
T["discount-calculator"] = r"""
    set("a1", "2000"); set("a2", "25");
    check("sale price", txt("aOut"), "1,500");
    check("amount saved", txt("aSaved"), "500");

    set("a2", "0");
    check("zero discount", txt("aOut"), "2,000");
    set("a2", "100");
    check("free", txt("aOut"), "0");

    set("b1", "1500"); set("b2", "25");
    check("original price backwards", txt("bOut"), "2,000");
    set("b2", "100");
    check("100 percent off has no answer", txt("bOut"), String.fromCharCode(0x2014));
    set("b2", "50"); set("b1", "1000");
    check("half price backwards", txt("bOut"), "2,000");

    /* The headline case: 50% then 20% is 60% off, not 70% */
    set("c1", "2000"); set("c2", "50"); set("c3", "20");
    check("stacked final price", txt("cOut"), "800");
    check("real combined discount", txt("cReal"), "60%");
    check("what it looks like", txt("cNaive"), "70%");
    check("the gap", txt("cGap"), "200");

    set("c2", "70"); set("c3", "30");
    check("70 then 30 is not free", txt("cOut"), "420");
    check("which is 79 percent", txt("cReal"), "79%");

    set("c1", "0");
    check("zero price is safe", txt("cOut"), "0");
        /* 150% off printed a price of -1,000. */
    var DASH = String.fromCharCode(0x2014);
    set("a1", "2000"); set("a2", "150");
    check("more than 100% off has no price", txt("aOut"), DASH);
    check("and nothing saved either", txt("aSaved"), DASH);
    has("the line under it says why", txt("aFormula"), "from 0% to 100%");
    set("a2", "100");
    check("exactly 100% off is free", txt("aOut"), "0");
    set("a2", "-10");
    check("a minus discount is refused too", txt("aOut"), DASH);
    set("a1", "-2000"); set("a2", "10");
    check("and a minus price", txt("aOut"), DASH);
    has("with its own reason", txt("aFormula"), "price cannot be below zero");
    set("a1", "2000"); set("a2", "25");
    check("back to the example", txt("aOut"), "1,500");

    set("b1", "1500"); set("b2", "120");
    check("working back from 120% off has no answer", txt("bOut"), DASH);
    has("and says why", txt("bFormula"), "from 0% to 100%");
    set("b2", "25");

    set("c1", "2000"); set("c2", "50"); set("c3", "120");
    check("a second discount over 100% is refused", txt("cOut"), DASH);
    check("in every tile", txt("cGap"), DASH);
    has("with the reason", txt("cFormula"), "from 0% to 100%");
    set("c3", "20");
    check("and back", txt("cOut"), "800");
    finish();
"""
# ---- END: the test for discount-calculator ----

# ---- START: the test for emi-calculator ----
T["emi-calculator"] = r"""
    set("amount", "1000000"); set("rate", "8.5"); set("years", "20");
    ok("an EMI is produced", txt("emi").replace(/[^0-9]/g, "").length >= 4, txt("emi"));
    var first = txt("emi");
    set("rate", "12");
    ok("a higher rate costs more", txt("emi") !== first, "EMI did not move");
    set("amount", "0");
    ok("zero loan does not crash", txt("emi").length > 0, txt("emi"));
    set("amount", "500000"); set("rate", "10"); set("years", "10");
    ok("schedule has rows",
       document.querySelectorAll("#schedule tr").length > 1,
       "rows: " + document.querySelectorAll("#schedule tr").length);
    var DASH = String.fromCharCode(0x2014);

    /* A refused loan used to leave the last EMI, totals and schedule. */
    set("amount", "1000000");
    ok("a loan has an EMI", txt("emi") !== DASH, txt("emi"));
    set("amount", "0");
    eq("a refused loan leaves no EMI behind", txt("emi"), DASH);
    eq("nor a total", txt("total"), DASH);
    eq("nor a schedule", document.querySelectorAll("#schedule tbody tr").length, 0);
    set("amount", "1000000");
    ok("and a real loan works again", txt("emi") !== DASH, txt("emi"));
      set("amount", "2500000"); set("rate", "8.5"); set("years", "20");
  eq("the loan is grouped in lakhs", txt("principal"), "25,00,000");
  ok("and so is the total", /^\d{1,2}(,\d\d)+,\d{3}$/.test(txt("total")), txt("total"));
  ok("and each year's balance in the table",
     /^\d{1,2}(,\d\d)+,\d{3}$/.test(document.querySelector("#schedule tbody tr td:last-child").textContent),
     document.querySelector("#schedule tbody tr td:last-child").textContent);
  finish();
"""
# ---- END: the test for emi-calculator ----

# ---- START: the test for find-and-replace ----
T["find-and-replace"] = r"""
    set("input", "the cat sat on the mat");
    set("findBox", "cat"); set("replaceBox", "dog");
    check("simple replace", txt("output"), "the dog sat on the mat");
    set("findBox", "the"); set("replaceBox", "a");
    check("replaces every match", txt("output"), "a cat sat on a mat");
    set("input", "Cat cat"); set("findBox", "cat"); set("replaceBox", "x");
    check("case insensitive by default", txt("output"), "x x");
    tick("optCase", true);
    check("case sensitive", txt("output"), "Cat x");
    tick("optCase", false);
    set("input", "cat cats"); set("findBox", "cat"); set("replaceBox", "dog");
    tick("optWord", true);
    check("whole word only", txt("output"), "dog cats");
    tick("optWord", false);
    set("input", "a1b2c3"); set("findBox", "[0-9]"); set("replaceBox", "#");
    tick("optRegex", true);
    check("regex works", txt("output"), "a#b#c#");
    set("findBox", "[unclosed");
    ok("bad regex is reported, not thrown",
       document.getElementById("status").textContent.length > 0);
    tick("optRegex", false);
        /* "Whole words only" was \b, which knows only A-Z, 0-9 and _: it never
       found a Telugu word, nor an accented one, nor C++. */
    var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    var TE2 = String.fromCharCode(0x0C2D, 0x0C3E, 0x0C37);
    tick("optRegex", false); tick("optCase", false); tick("optWord", true);
    set("input", TE + " " + TE2 + " " + TE + "x " + TE + ".");
    set("findBox", TE); set("replaceBox", "T");
    eq("a Telugu word is found whole", txt("output"), "T " + TE2 + " " + TE + "x T.");
    has("twice, not inside a longer word", txt("status"), "2 replacements");
    set("input", TE + String.fromCharCode(0x0C41) + " " + TE);
    eq("a vowel sign after it makes it part of a longer word", txt("output"),
       TE + String.fromCharCode(0x0C41) + " T");

    var CAFE = "caf" + String.fromCharCode(0xE9);
    set("input", CAFE + " " + CAFE + "s " + CAFE);
    set("findBox", CAFE); set("replaceBox", "tea");
    eq("an accented word is found whole", txt("output"), "tea " + CAFE + "s tea");
    set("input", "C++ and C++11 and C++");
    set("findBox", "C++"); set("replaceBox", "Java");
    eq("so is a word that ends in symbols", txt("output"), "Java and C++11 and Java");
    set("input", "cat concat cat_x cat.");
    set("findBox", "cat"); set("replaceBox", "dog");
    eq("plain English is unchanged", txt("output"), "dog concat cat_x dog.");
    set("input", "CAT Cat cat");
    eq("and ignoring case still works", txt("output"), "dog dog dog");

    tick("optRegex", true);
    set("input", TE + " " + TE + "x");
    set("findBox", TE + "|" + TE2); set("replaceBox", "[$&]");
    eq("whole words work with a pattern too", txt("output"), "[" + TE + "] " + TE + "x");
    set("input", "a-b a-bc");
    set("findBox", "a\\-b"); set("replaceBox", "X");
    eq("a pattern the Unicode rules refuse falls back to the old edges", txt("output"), "X a-bc");
    tick("optRegex", false); tick("optWord", false);
    finish();
"""
# ---- END: the test for find-and-replace ----

# ---- START: the test for fraction-calculator ----
T["fraction-calculator"] = r"""
  eq("3/4 + 1/6", txt("ansFrac"), "11/12");
  eq("mixed form", txt("mixed"), "11/12");
  near("decimal", txt("dec"), 0.9167, 0.0001);
  eq("percentage", txt("perc"), "91.67%");
  has("working shows unsimplified", txt("working"), "22/24");
  has("working shows the divisor", txt("working"), "divide both by 2");
  gone("no error on a valid sum", "errWrap");

  set("op", "sub"); eq("3/4 - 1/6", txt("ansFrac"), "7/12");
  set("op", "mul"); eq("3/4 x 1/6", txt("ansFrac"), "1/8");
  set("op", "div"); eq("3/4 / 1/6", txt("ansFrac"), "9/2");
  eq("improper shown as mixed", txt("mixed"), "4 1/2");
  near("division decimal", txt("dec"), 4.5, 0.0001);
  has("division explains the flip", txt("working"), "flip");

  set("op", "add"); set("aw", 2); set("an", 1); set("ad", 2);
  eq("2 1/2 + 1/6", txt("ansFrac"), "8/3");
  eq("2 1/2 + 1/6 as mixed", txt("mixed"), "2 2/3");

  set("aw", -2); set("bw", 0); set("bn", 0); set("bd", 1);
  eq("negative whole number", txt("ansFrac"), "-5/2");

  set("aw", 0); set("an", 1); set("ad", 0);
  shown("zero denominator is an error", "errWrap");
  has("error explains itself", txt("err"), "zero");

  set("ad", 4); set("an", 3); set("bn", 0); set("bd", 6); set("op", "div");
  shown("divide by zero is an error", "errWrap");

  set("op", "add"); set("bn", 1);
  gone("error clears once valid", "errWrap");
  eq("back to 11/12", txt("ansFrac"), "11/12");

  set("an", 2); set("ad", 4); set("bn", 0); set("bd", 1);
  eq("2/4 reduces to 1/2", txt("ansFrac"), "1/2");
  has("working says already lowest", txt("working"), "divide both by 2");

  eq("126/294 simplifies", txt("sAns"), "3/7");
  eq("gcd is 42", txt("sGcd"), "42");
  near("simplify decimal", txt("sDec"), 0.4286, 0.0001);
  set("sn", 7); set("sd", 13);
  eq("prime pair is untouched", txt("sAns"), "7/13");
  has("says already lowest terms", txt("sHow"), "already in lowest terms");
  set("sd", 0);
  has("zero denominator handled", txt("sHow"), "cannot be zero");
        /* parseInt read 1.5 as 1, so 1.5/2 + 1/2 quietly came out as 1. */
    var DASH = String.fromCharCode(0x2014);
    set("op", "add");
    set("aw", "0"); set("an", "1.5"); set("ad", "2");
    set("bw", "0"); set("bn", "1"); set("bd", "2");
    shown("a decimal on top is refused", "errWrap");
    has("with the reason", txt("err"), "whole numbers only");
    eq("and no answer, shown as a dash rather than a minus sign", txt("ansFrac"), DASH);
    eq("in every tile", txt("dec"), DASH);
    set("an", "3"); set("ad", "4");
    eq("written as 3/4 instead it works", txt("ansFrac"), "5/4");
    gone("with the error gone", "errWrap");
    set("bd", "2.0");
    shown("a decimal point anywhere is refused, even .0", "errWrap");
    set("bd", "2");
    /* Letters never reach the page: these are number boxes, and the
       browser empties one that holds letters. */
    set("aw", "");
    gone("an empty box is still a zero", "errWrap");

    /* Past 2^53 the arithmetic stops being exact. */
    set("an", "99999999999"); set("ad", "99999999998"); set("bn", "99999999997"); set("bd", "99999999996");
    shown("a sum too big to be exact is refused", "errWrap");
    has("and says so", txt("err"), "too big to work out exactly");
    set("an", "99999999999999999999");
    shown("so is a number too big to type exactly", "errWrap");
    set("an", "3"); set("ad", "4"); set("bn", "1"); set("bd", "2");

    set("sn", "1.5"); set("sd", "2");
    has("the simplify box refuses a decimal too", txt("sHow"), "whole numbers only");
    eq("with a dash", txt("sAns"), DASH);
    set("sn", "99999999999999999999"); set("sd", "2");
    has("and a number too big to hold exactly", txt("sHow"), "too big to work out exactly");
    set("sn", "6"); set("sd", "0");
    eq("a zero denominator shows a dash there as well", txt("sAns"), DASH);
    set("sn", "126"); set("sd", "294");
    eq("and back to the example", txt("sAns"), "3/7");
    finish();
"""
# ---- END: the test for fraction-calculator ----

# ---- START: the test for gst-calculator ----
T["gst-calculator"] = r"""
  near("default total 1180", txt("total"), 1180);
  near("default base 1000", txt("base"), 1000);
  near("default GST 180", txt("tax"), 180);
  near("default CGST 90", txt("cgst"), 90);
  near("default SGST 90", txt("sgst"), 90);
  eq("CGST label halves the rate", txt("cgstLabel"), "CGST 9%");
  gone("custom rate box hidden by default", "customWrap");
  gone("inter-state block hidden by default", "interWrap");

  set("amount", 1180); set("mode", "inclusive");
  near("reverse GST base", txt("base"), 1000);
  near("reverse GST tax", txt("tax"), 180);
  near("reverse GST total unchanged", txt("total"), 1180);
  has("formula divides, not subtracts", txt("formula"), "1.18");

  set("mode", "exclusive"); set("amount", 1000); set("rate", "5");
  near("5% tax", txt("tax"), 50);
  near("5% total", txt("total"), 1050);
  eq("5% splits to 2.5", txt("cgstLabel"), "CGST 2.5%");
  set("rate", "18");
  eq("18% splits to 9 with no trailing zero", txt("cgstLabel"), "CGST 9%");
  set("rate", "5");

  set("rate", "0");
  near("0% tax", txt("tax"), 0);
  near("0% total", txt("total"), 1000);

  set("rate", "40");
  near("40% tax", txt("tax"), 400);

  set("rate", "custom"); set("customRate", 3);
  shown("custom box appears", "customWrap");
  near("custom 3% tax", txt("tax"), 30);

  set("rate", "18"); set("supply", "inter");
  gone("custom box hides again", "customWrap");
  gone("intra block hides", "intraWrap");
  shown("inter block appears", "interWrap");
  near("IGST is the whole tax", txt("igst"), 180);
  eq("IGST label", txt("igstLabel"), "IGST 18%");

  set("amount", "");
  near("empty amount does not crash", txt("total"), 0);
    var DASH = String.fromCharCode(0x2014);

    /* 5% of 101 is 5.05; its halves are 2.525 each. They were shown as
       2.53 + 2.53 = 5.06 beside a total of 5.05. */
    var R = String.fromCharCode(0x20B9);
    set("rate", "5"); set("mode", "exclusive"); set("supply", "intra"); set("amount", "101");
    eq("the GST", txt("tax"), R + "5.05");
    eq("CGST is exactly half", txt("cgst"), R + "2.525");
    eq("and so is SGST", txt("sgst"), R + "2.525");
    has("with a word on invoices that round each half", txt("splitNote"), R + "2.53 twice");
    set("amount", "100");
    eq("an even number of paise halves cleanly", txt("cgst"), R + "2.50");
    eq("with nothing more to say", txt("splitNote").indexOf("twice"), -1);
    set("rate", "custom"); set("customRate", "-5");
    eq("a minus rate is refused, not read as 0%", txt("total"), DASH);
    has("with the reason", txt("formula"), "cannot be below zero");
    set("rate", "18");
      set("mode", "exclusive"); set("rate", "18"); set("amount", "1234567");
  eq("rupees are grouped in lakhs, whatever the browser's language", txt("base"),
     String.fromCharCode(0x20B9) + "12,34,567");
  set("amount", "1000");
  finish();
"""
# ---- END: the test for gst-calculator ----

# ---- START: the test for image-compressor ----
T["image-compressor"] = r"""
    makeImage("file", 200, 200, function (file) {
      /* Decode and re-encode are both asynchronous - wait for the real
         result instead of guessing a delay. */
      waitFor("compressed size appears",
        function () { return txt("sAfter").length > 1 && txt("sAfter") !== "—"; },
        function () {
          ok("original size shown", txt("sBefore").length > 1, txt("sBefore"));
          ok("a preview image appeared",
             document.querySelectorAll("#preview img, #preview canvas").length > 0);
          ok("download button is enabled",
             !document.getElementById("dlBtn").disabled);
          ok("a saving is reported", txt("sSaved").length > 0, txt("sSaved"));

          var before = txt("sAfter");
          var quality = document.getElementById("quality");
          quality.value = "30";
          quality.dispatchEvent(new Event("input", { bubbles: true }));
          waitFor("re-encodes at a new quality",
            function () { return txt("sAfter").length > 1; },
            function () {
              ok("size still reported after requality", txt("sAfter").length > 1, before);
                /* A PNG with see-through parts saved as JPG used to come out on a BLACK
     square: the canvas hands transparent pixels to the JPEG encoder as
     black. The tool now puts white behind the picture for JPG. */
  (function () {
    window.__saved = window.__saved || [];
    window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
    click("resetBtn");
    
    var c = document.createElement("canvas"); c.width = 120; c.height = 80;
    var x = c.getContext("2d"); x.fillStyle = "#ff0000"; x.fillRect(40, 20, 40, 40);
    c.toBlob(function (png) {
      var dt = new DataTransfer();
      dt.items.add(new File([png], "logo.png", { type: "image/png" }));
      var input = document.getElementById("file");
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      waitFor("a transparent PNG is taken in", function () {
        return !document.getElementById("dlBtn").disabled;
      }, function () {
        var count = window.__saved.length;
        click("dlBtn");
        waitFor("and saved as a JPG", function () { return window.__saved.length > count; }, function () {
          var saved = window.__saved[window.__saved.length - 1];
          eq("compressor: the file is a JPEG", saved.blob.type, "image/jpeg");
          createImageBitmap(saved.blob).then(function (bmp) {
            var k = document.createElement("canvas"); k.width = bmp.width; k.height = bmp.height;
            var kx = k.getContext("2d"); kx.drawImage(bmp, 0, 0);
            var d = kx.getImageData(2, 2, 1, 1).data;
            ok("compressor: a see-through corner comes out white, not black",
               d[0] > 245 && d[1] > 245 && d[2] > 245, Array.prototype.join.call(d, ","));
            var m = kx.getImageData(bmp.width >> 1, bmp.height >> 1, 1, 1).data;
            ok("compressor: and the picture itself is unchanged",
               m[0] > 220 && m[1] < 40 && m[2] < 40, Array.prototype.join.call(m, ","));
            has("compressor: and the page says the see-through parts are white now",
                txt("msg"), "see-through parts are white");
            finish();
          });
        }, 20000);
      }, 20000);
    }, "image/png");
  })();
            });
        });
    });
"""
# ---- END: the test for image-compressor ----

# ---- START: the test for image-converter ----
T["image-converter"] = r"""
    makeImage("file", 160, 120, function (file) {
      waitFor("dimensions appear",
        function () { return txt("sDims").indexOf("160") > -1; },
        function () {
          ok("original size shown", txt("sBefore").length > 1, txt("sBefore"));
          ok("dimensions are 160 wide", txt("sDims").indexOf("160") > -1, txt("sDims"));
          ok("a preview appeared",
             document.querySelectorAll("#preview img, #preview canvas").length > 0);

          set("format", "image/jpeg");
          waitFor("converts to JPG",
            function () { return !document.getElementById("dlBtn").disabled; },
            function () {
              ok("converted size shown", txt("sAfter").length > 1, txt("sAfter"));
              ok("download enabled", !document.getElementById("dlBtn").disabled);

              /* Resizing is the other half of this tool */
              set("width", "80");
              waitFor("resizes to 80 wide",
                function () { return txt("sDims").indexOf("80") > -1; },
                function () {
                  ok("resized dimensions", txt("sDims").indexOf("80") > -1, txt("sDims"));
                  finish();
                });
            });
        });
    });
"""
# ---- END: the test for image-converter ----

# ---- START: the test for json-formatter ----
T["json-formatter"] = r"""
    set("input", '{"b":1,"a":[1,2]}');
    click("beautifyBtn");
    ok("beautified onto several lines", txt("output").split("\n").length > 3, txt("output"));
    click("minifyBtn");
    check("minified", txt("output"), '{"b":1,"a":[1,2]}');

    set("input", '{"bad": }');
    click("beautifyBtn");
    ok("invalid JSON is reported",
       document.getElementById("msg").textContent.length > 0);
    ok("and marked as an error",
       document.getElementById("msg").className.indexOf("bad") > -1,
       document.getElementById("msg").className);

    set("input", '{"a":{"b":{"c":1}}}');
    click("beautifyBtn");
    ok("depth is measured", txt("sDepth") !== "0", txt("sDepth"));
    ok("keys are counted", txt("sKeys") !== "0", txt("sKeys"));

    /* The message box is built as markup so it can carry a <strong> and a
       <br>, and the browser's own JSON.parse error quotes a piece of the
       input back inside it. Eighteen characters used to be enough to put a
       live iframe on this page. security.py attacks every tool for this;
       this keeps the specific one honest in the fast suite too. */
    set("input", "<iframe onload=zq>");
    click("beautifyBtn");
    var box = document.getElementById("msg");
    ok("a pasted tag stays text and never becomes an element",
       box.querySelectorAll("iframe, svg, img, object, script").length === 0,
       box.innerHTML.slice(0, 120));
    ok("and the visitor still sees what they typed, as characters",
       box.textContent.indexOf("<iframe") > -1, box.textContent.slice(0, 90));
        /* The result used to be JSON.stringify(JSON.parse(text)), which changed
       what it printed: big numbers rounded, 1e400 became null, and a key
       written twice lost its first value - all under "Valid JSON". The text
       is now laid out token by token. For everyday JSON that must give
       exactly what JSON.stringify gives, so 300 random documents, each
       pasted with different spacing, are compared with it. */
    var seed = 12345;
    function rnd(n) { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; }
    var PIECES = ["a", "b", "{", "}", "[", "]", ",", ":", " ", "\"", "\\", "\n", "\t",
                  String.fromCharCode(0x0C24), String.fromCharCode(0xD83D, 0xDE00), "/", "x y"];
    function randomString() {
      var s = "", k = rnd(6);
      while (k--) { s += PIECES[rnd(PIECES.length)]; }
      return s;
    }
    function randomValue(depth) {
      var kind = rnd(depth > 3 ? 4 : 7);
      if (kind === 0) return rnd(2000) - 1000;
      if (kind === 1) return (rnd(100000) - 50000) / 7;
      if (kind === 2) return randomString();
      if (kind === 3) return [true, false, null][rnd(3)];
      if (kind === 4) { var a = [], m = rnd(4); while (m--) a.push(randomValue(depth + 1)); return a; }
      var o = {}, k = rnd(4);
      while (k--) o["k" + rnd(50) + randomString()] = randomValue(depth + 1);
      return o;
    }
    var SPACING = [0, 1, 3, "\t", " \n "];
    var wrong = [];
    for (var doc = 0; doc < 300; doc++) {
      var v = randomValue(0);
      set("input", JSON.stringify(v, null, SPACING[doc % SPACING.length]));
      click("beautifyBtn");
      if (txt("output") !== JSON.stringify(v, null, 2)) { wrong.push("beautify " + JSON.stringify(v)); }
      click("minifyBtn");
      if (txt("output") !== JSON.stringify(v)) { wrong.push("minify " + JSON.stringify(v)); }
    }
    eq("300 random documents laid out exactly as JSON.stringify would", wrong.length, 0);
    if (wrong.length) { ok("first difference", false, wrong[0].slice(0, 200)); }

    set("indent", "tab");
    set("input", '{"a":[1,{"b":2}]}');
    click("beautifyBtn");
    eq("a tab indent", txt("output"), '{\n\t"a": [\n\t\t1,\n\t\t{\n\t\t\t"b": 2\n\t\t}\n\t]\n}');
    set("indent", "4");
    click("beautifyBtn");
    eq("a four-space indent", txt("output"), '{\n    "a": [\n        1,\n        {\n            "b": 2\n        }\n    ]\n}');
    set("indent", "2");

    set("input", '{"id":12345678901234567890,"x":1.0,"y":1e400,"z":-0,"w":2E-3}');
    click("beautifyBtn");
    eq("every number comes out exactly as it was written",
       txt("output"), '{\n  "id": 12345678901234567890,\n  "x": 1.0,\n  "y": 1e400,\n  "z": -0,\n  "w": 2E-3\n}');
    has("and the page says which ones other programs would change",
        txt("msg"), "12345678901234567890, 1e400 are kept exactly as written");
    click("minifyBtn");
    eq("minifying keeps them too", txt("output"), '{"id":12345678901234567890,"x":1.0,"y":1e400,"z":-0,"w":2E-3}');

    set("input", '{"a":1,"a":2,"b":{"c":1,"\\u0063":2}}');
    click("beautifyBtn");
    eq("a key written twice keeps both values", txt("output"),
       '{\n  "a": 1,\n  "a": 2,\n  "b": {\n    "c": 1,\n    "\\u0063": 2\n  }\n}');
    has("and the page names it", txt("msg"), 'The keys "a", "c" appear twice in one object');
    eq("every key is counted, both copies included", txt("sKeys"), "5");
    eq("the depth is three", txt("sDepth"), "3");

    set("input", '{"a":1}');
    click("beautifyBtn");
    eq("ordinary JSON draws no caveat", txt("msg"), "Valid JSON · formatted with 2 spaces");

    set("input", ' [ {} , [ ] , "x" ] ');
    click("beautifyBtn");
    eq("empty containers stay on one line", txt("output"), '[\n  {},\n  [],\n  "x"\n]');
    set("input", "42");
    click("beautifyBtn");
    eq("a bare number is valid JSON too", txt("output"), "42");
    eq("with no keys", txt("sKeys"), "0");
    eq("and a depth of one", txt("sDepth"), "1");

    set("input", '{"<img src=x onerror=zq>":1,"<img src=x onerror=zq>":2}');
    click("beautifyBtn");
    eq("a duplicate key is named as text, never as markup",
       document.getElementById("msg").querySelectorAll("img").length, 0);
    has("and is still readable", txt("msg"), "<img src=x onerror=zq>");
    finish();
"""
# ---- END: the test for json-formatter ----

# ---- START: the test for lorem-ipsum-generator ----
T["lorem-ipsum-generator"] = r"""
    set("unit", "words"); set("count", "10");
    click("genBtn");
    ok("ten words", txt("output").split(/\s+/).filter(Boolean).length === 10,
       "got " + txt("output").split(/\s+/).filter(Boolean).length);
    set("unit", "paragraphs"); set("count", "3");
    click("genBtn");
    ok("three paragraphs", txt("output").split(/\n\n+/).filter(Boolean).length === 3,
       "got " + txt("output").split(/\n\n+/).filter(Boolean).length);
    set("unit", "sentences"); set("count", "4");
    click("genBtn");
    ok("four sentences", txt("output").split(".").filter(function (s) {
       return s.trim() !== ""; }).length === 4);
        /* 0 and -3 used to make one word; the box was rewritten while typed in. */
    set("count", "0");
    eq("nought makes nothing", txt("output"), "");
    has("and says what it takes", txt("msg"), "a whole number from 1 to 200");
    eq("the box is not typed over", val("count"), "0");
    set("count", "-3");
    eq("nor does a minus number", txt("output"), "");
    set("count", "500"); set("unit", "words");
    eq("500 words is cut to the most", txt("output").split(/\s+/).filter(Boolean).length, 200);
    has("and says so", txt("msg"), "Made 200");
    set("count", "4");
    eq("an ordinary count has nothing to say", txt("msg"), "");
    finish();
"""
# ---- END: the test for lorem-ipsum-generator ----

# ---- START: the test for margin-markup-calculator ----
T["margin-markup-calculator"] = r"""
  near("cost 100 at 40% margin", txt("aOut"), 166.67, 0.01);
  near("profit per unit", txt("aProfit"), 66.67, 0.01);
  eq("40% margin is 66.67% markup", txt("aMarkup"), "66.67%");
  eq("margin echoed back", txt("aMargin"), "40%");

  near("cost 100 at 40% markup", txt("bOut"), 140);
  near("markup profit", txt("bProfit"), 40);
  eq("40% markup is 28.57% margin", txt("bMargin"), "28.57%");

  near("100 to 150 profit", txt("cOut"), 50);
  eq("that is a 33.33% margin", txt("cMargin"), "33.33%");
  eq("and a 50% markup", txt("cMarkup"), "50%");
  has("multiple shown", txt("cMultiple"), "1.5");

  set("a2", 100);
  eq("100% margin is impossible", txt("aOut").charCodeAt(0), 8212);
  has("and says why", txt("aFormula"), "impossible");

  set("a2", 0);
  near("zero margin sells at cost", txt("aOut"), 100);
  near("zero margin makes no profit", txt("aProfit"), 0);

  set("b2", 0);
  near("zero markup sells at cost", txt("bOut"), 100);

  set("c1", 150); set("c2", 100);
  has("selling below cost is called out", txt("cFormula"), "loss");
  near("the loss amount", txt("cOut"), -50);

  set("c1", 0); set("c2", 100);
  eq("zero cost gives no markup", txt("cMarkup").charCodeAt(0), 8212);

  set("c1", 100); set("c2", 0);
  eq("zero price gives no margin", txt("cMargin").charCodeAt(0), 8212);
        set("c1", "150"); set("c2", "100");
    eq("a loss reads minus first", txt("cOut"), String.fromCharCode(0x2212, 0x20B9) + "50");
    set("c1", "100"); set("c2", "150");
      set("a1", "100000"); set("a2", "40");
  eq("rupees are grouped in lakhs", txt("aOut"), String.fromCharCode(0x20B9) + "1,66,666.67");
  finish();
"""
# ---- END: the test for margin-markup-calculator ----

# ---- START: the test for password-generator ----
T["password-generator"] = r"""
    set("length", "16"); set("howMany", "3");
    click("genBtn");
    var rows = document.querySelectorAll("#list button");
    check("three passwords", rows.length, 3);
    ok("each is 16 characters", rows[0].textContent.trim().length === 16,
       "got " + rows[0].textContent.trim().length);
    ok("two runs differ", rows[0].textContent !== rows[1].textContent);

    /* Only digits selected: the output must contain nothing else */
    tick("optUpper", false); tick("optLower", false);
    tick("optDigit", true); tick("optSymbol", false);
    click("genBtn");
    var digits = document.querySelectorAll("#list button")[0].textContent.trim();
    ok("digits only respected", /^[0-9]+$/.test(digits), digits);

    tick("optLower", true);
    click("genBtn");
    ok("strength is reported", txt("strengthWord").length > 0);
    var DASH = String.fromCharCode(0x2014);

    /* With every box unticked the last batch stayed, rated "Very strong". */
    ["optUpper", "optLower", "optDigit", "optSymbol"].forEach(function (id) { tick(id, false); });
    eq("no character type, no passwords", document.querySelectorAll("#list button").length, 0);
    eq("and no strength", txt("strengthWord"), DASH);
    tick("optLower", true);
    ok("one type ticked makes passwords again", document.querySelectorAll("#list button").length > 0);
    ["optUpper", "optDigit", "optSymbol"].forEach(function (id) { tick(id, true); });
        /* The box used to be rewritten on every key: emptied to type 10, it
       became 1 at once, then 110, then 50 without a word. */
    set("howMany", "");
    eq("an emptied box is left empty while typing", val("howMany"), "");
    has("and asks for a number", txt("msg"), "a whole number from 1 to 50");
    eq("with no passwords under it", document.querySelectorAll("#list button").length, 0);
    set("howMany", "10");
    eq("typing 10 makes ten", document.querySelectorAll("#list button").length, 10);
    eq("and the box says 10, not 110", val("howMany"), "10");
    set("howMany", "500");
    eq("500 makes the most it can", document.querySelectorAll("#list button").length, 50);
    has("and says that is the most", txt("msg"), "Made 50");
    set("howMany", "0");
    eq("nought makes none", document.querySelectorAll("#list button").length, 0);
    set("howMany", "5");
    eq("five again", document.querySelectorAll("#list button").length, 5);
    eq("with nothing to say", txt("msg"), "");
    finish();
"""
# ---- END: the test for password-generator ----

# ---- START: the test for percentage-calculator ----
T["percentage-calculator"] = r"""
    set("a1", "15"); set("a2", "200");
    check("15% of 200", txt("aOut"), "30");
    set("b1", "30"); set("b2", "200");
    check("30 is 15% of 200", txt("bOut"), "15%");
    set("c1", "200"); set("c2", "250");
    ok("200 to 250 is +25%", txt("cOut").indexOf("25") > -1, txt("cOut"));
    set("c1", "250"); set("c2", "200");
    ok("250 to 200 is a fall", txt("cOut").indexOf("20") > -1, txt("cOut"));
    finish();
"""
# ---- END: the test for percentage-calculator ----

# ---- START: the test for ratio-calculator ----
T["ratio-calculator"] = r"""
    set("a1", "1920"); set("a2", "1080");
    check("simplify to 16:9", txt("aOut"), "16 : 9");
    check("the factor used", txt("aHcf"), "120");
    check("as a decimal", txt("aDecimal"), "1.78");

    set("a1", "4"); set("a2", "2");
    check("simplify 4:2", txt("aOut"), "2 : 1");
    set("a1", "7"); set("a2", "13");
    check("already simplest", txt("aOut"), "7 : 13");
    set("a1", "2.5"); set("a2", "1");
    check("decimals scale up", txt("aOut"), "5 : 2");
    set("a1", "0"); set("a2", "5");
    check("zero side refuses", txt("aOut"), String.fromCharCode(0x2014));

    set("b1", "3"); set("b2", "4"); set("b3", "15");
    check("solve the proportion", txt("bOut"), "20");
    set("b1", "2"); set("b2", "3"); set("b3", "10");
    check("another proportion", txt("bOut"), "15");
    set("b1", "0");
    check("zero first term refuses", txt("bOut"), String.fromCharCode(0x2014));

    function rows() {
      return Array.prototype.map.call(
        document.querySelectorAll("#splitRows tr"),
        function (tr) {
          return Array.prototype.map.call(tr.children, function (td) {
            return td.textContent;
          });
        });
    }

    set("c1", "5000"); set("c2", "2:3");
    check("two shares", rows().length, 2);
    check("first share", rows()[0], ["Share 1", "2", "2,000", "40.0%"]);
    check("second share", rows()[1], ["Share 2", "3", "3,000", "60.0%"]);

    set("c2", "1:1:2");
    check("three shares", rows().length, 3);
    check("last share is half", rows()[2][2], "2,500");

    set("c2", "5");
    check("one part is refused", document.getElementById("cMsg").className, "msg msg--bad");
    set("c2", "2:3");
    check("recovers", rows().length, 2);
    finish();
"""
# ---- END: the test for ratio-calculator ----

# ---- START: the test for remove-duplicate-lines ----
T["remove-duplicate-lines"] = r"""
    set("input", "a\nb\na\nc\nb");
    check("duplicates removed", txt("output"), "a\nb\nc");
    check("lines in", txt("cIn"), "5");
    check("lines out", txt("cOut"), "3");
    check("removed", txt("cGone"), "2");
    set("input", "A\na");
    check("case ignored by default", txt("output"), "A");
    tick("optCase", false);
    check("case respected", txt("output"), "A\na");
    tick("optCase", true);
    set("input", "");
    check("empty is empty", txt("output"), "");
    finish();
"""
# ---- END: the test for remove-duplicate-lines ----

# ---- START: the test for remove-line-breaks ----
T["remove-line-breaks"] = r"""
    set("input", "line one\nline two\nline three");
    ok("breaks removed", txt("output").indexOf("\n") === -1, txt("output"));
    ok("words still separated", txt("output").indexOf("one line") > -1, txt("output"));
    check("lines in", txt("cIn"), "3");
    finish();
"""
# ---- END: the test for remove-line-breaks ----

# ---- START: the test for reverse-text ----
T["reverse-text"] = r"""
    set("input", "Hello world");
    check("whole text reversed", out(), "dlrow olleH");
    check("characters counted", txt("cChars"), "11");
    check("words counted", txt("cWords"), "2");
    check("lines counted", txt("cLines"), "1");

    /* The emoji case: split("") would return two broken halves here */
    set("input", "ab" + String.fromCodePoint(0x1F600));
    check("emoji stays whole", out(), String.fromCodePoint(0x1F600) + "ba");

    set("input", "ab\ncd");
    set("mode", "line-chars");
    check("characters per line", out(), "ba\ndc");

    set("input", "one two three");
    set("mode", "word-line");
    check("word order per line", out(), "three two one");

    set("input", "one  two");
    check("original spacing kept", out(), "two  one");

    set("input", "a\nb\nc");
    set("mode", "line-order");
    check("line order flipped", out(), "c\nb\na");

    set("mode", "word-all");
    set("input", "x y");
    check("word order whole text", out(), "y x");

    /* Windows line endings must not leave a stray carriage return */
    set("mode", "all");
    set("input", "ab\r\ncd");
    check("CRLF handled", out(), "dc\nba");

    document.getElementById("clearBtn").click();
    check("clear empties output", out(), "");
    check("clear resets counters", txt("cChars"), "0");
        /* By what a reader sees as one character. */
    var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    var TE_REV = String.fromCharCode(0x0C17, 0x0C41, 0x0C32, 0x0C41, 0x0C24, 0x0C46);
    var THUMB = String.fromCharCode(0xD83D, 0xDC4D, 0xD83C, 0xDFFD);
    var FAMILY = String.fromCharCode(0xD83D, 0xDC68, 0x200D, 0xD83D, 0xDC69, 0x200D, 0xD83D, 0xDC67);
    set("mode", "all");
    set("input", TE);
    eq("Telugu letters keep their vowel signs", txt("output"), TE_REV);
    eq("and count as three", txt("cChars"), "3");
    set("input", "ab" + THUMB + FAMILY);
    eq("a skin tone stays on its thumb, a family stays together", txt("output"), FAMILY + THUMB + "ba");
    eq("four characters", txt("cChars"), "4");
    finish();
"""
# ---- END: the test for reverse-text ----

# ---- START: the test for simple-interest-calculator ----
T["simple-interest-calculator"] = r"""
  near("50000 at 8% for 3y", txt("interest"), 12000);
  near("total repayable", txt("tOut"), 62000);
  eq("three rows", rows().length, 3);
  near("row 1 balance", cell(0, 2), 54000);
  near("row 1 interest", cell(0, 1), 4000);
  near("row 3 balance", cell(2, 2), 62000);

  set("unit", "months"); set("period", 6);
  near("6 months interest", txt("interest"), 2000);
  eq("part year is one row", rows().length, 1);
  has("note converts to years", txt("yearsNote"), "0.5");

  set("unit", "days"); set("period", 365);
  near("365 days equals one year", txt("interest"), 4000);

  set("unit", "years"); set("period", 3); set("rate", 0);
  near("zero rate earns nothing", txt("interest"), 0);
  near("zero rate total is principal", txt("tOut"), 50000);

  set("rate", 8); set("principal", 0);
  eq("zero principal builds no table", rows().length, 0);

  set("principal", 50000); set("period", 100);
  eq("long period is capped at 40 rows", rows().length, 40);

  set("period", "");
  near("empty period does not crash", txt("interest"), 0);
      set("principal", "500000"); set("rate", "8"); set("period", "3");
  ok("rupees are grouped in lakhs", /^\d{1,2}(,\d\d)+,\d{3}$/.test(txt("tOut").slice(1)), txt("tOut"));
  finish();
"""
# ---- END: the test for simple-interest-calculator ----

# ---- START: the test for slug-generator ----
T["slug-generator"] = r"""
    set("input", "10 Best Caf" + String.fromCharCode(0xE9) + "s in Hyderabad (2026 Guide)");
    check("accents and punctuation", out(), "10-best-cafes-in-hyderabad-2026-guide");
    check("one slug counted", txt("cSlugs"), "1");

    /* The same letter written as base + combining accent must match */
    set("input", "cafe" + String.fromCharCode(0x301));
    check("combining accent stripped", out(), "cafe");

    set("input", "Stra" + String.fromCharCode(0xDF) + "e");
    check("sharp s becomes ss", out(), "strasse");

    set("input", "India's Best");
    check("apostrophe closes up", out(), "indias-best");

    set("input", "India" + String.fromCharCode(0x2019) + "s Best");
    check("curly apostrophe closes up", out(), "indias-best");

    set("input", "Hello World");
    set("sep", "_");
    check("underscore separator", out(), "hello_world");
    set("sep", "-");

    tick("optLower", false);
    check("lowercase off", out(), "Hello-World");
    tick("optLower", true);

    set("input", "The State of the Art");
    tick("optStop", true);
    check("stop words removed", out(), "state-art");
    set("input", "The Of And");
    check("all stop words keeps the title", out(), "the-of-and");
    tick("optStop", false);

    set("input", "abc 123");
    tick("optNumbers", false);
    check("numbers dropped", out(), "abc");
    tick("optNumbers", true);
    check("numbers kept", out(), "abc-123");

    set("input", "hello world foo");
    set("maxLen", "11");
    check("cut at a whole word", out(), "hello-world");
    set("maxLen", "5");
    check("tighter limit", out(), "hello");
    set("maxLen", "3");
    check("first word longer than limit", out(), "hel");
    set("maxLen", "0");

    set("input", "One Title\nAnother Title");
    check("a list gives a slug per line", out(), "one-title\nanother-title");
    check("two slugs counted", txt("cSlugs"), "2");
    check("longest measured", txt("cLongest"), "13");

    set("input", "Same Title\nSame Title!");
    check("duplicate slugs flagged", document.getElementById("msg").className, "msg msg--bad");

    set("input", "Fine\nDifferent");
    check("no false duplicate warning", document.getElementById("msg").textContent, "");

    set("input", "  ");
    check("blank input gives nothing", out(), "");
        var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    tick("optAccents", true); set("maxLen", "0");
    set("input", TE + " 2026");
    has("Telugu letters left out are explained", txt("msg"), "Letters outside A to Z");
    set("input", "Caf" + String.fromCharCode(0xE9) + " Menu");
    eq("an accent that becomes a plain letter draws no note", txt("msg"), "");
    /* "constructor" used to be reported as a duplicate of nothing. */
    set("input", "Constructor");
    eq("a slug called constructor is not a duplicate", txt("msg"), "");
    finish();
"""
# ---- END: the test for slug-generator ----

# ---- START: the test for sort-text-lines ----
T["sort-text-lines"] = r"""
    set("input", "banana\napple\ncherry");
    check("A to Z", txt("output"), "apple\nbanana\ncherry");
    set("order", "za");
    check("Z to A", txt("output"), "cherry\nbanana\napple");
    set("order", "az");
    set("input", "item10\nitem2\nitem1");
    check("natural sort", txt("output"), "item1\nitem2\nitem10");
    set("input", "9 kg\n80 kg\n-3 kg");
    set("order", "num-asc");
    check("numeric sort", txt("output"), "-3 kg\n9 kg\n80 kg");
    set("order", "len-asc");
    set("input", "ccc\na\nbb");
    check("by length", txt("output"), "a\nbb\nccc");
    set("order", "reverse");
    set("input", "1\n2\n3");
    check("just reverse", txt("output"), "3\n2\n1");
        /* 1,000 and 1,00,000 used to be read as 1. */
    set("order", "num-asc");
    set("input", "10\n1,000\n9\n1,00,000\n2.5\n1,234.5");
    eq("grouped numbers sort by their whole value", txt("output"), "2.5\n9\n10\n1,000\n1,234.5\n1,00,000");
    set("order", "az");
    finish();
"""
# ---- END: the test for sort-text-lines ----

# ---- START: the test for text-repeater ----
T["text-repeater"] = r"""
    set("input", "ab");
    set("copies", "3");
    check("three copies on new lines", out(), "ab\nab\nab");
    check("copies counted", txt("cCopies"), "3");
    check("characters counted", txt("cChars"), "8");

    set("sep", "comma");
    check("comma separator", out(), "ab, ab, ab");

    set("sep", "none");
    check("no separator", out(), "ababab");

    set("sep", "nl");
    tick("optNumber", true);
    check("numbered copies", out(), "1. ab\n2. ab\n3. ab");
    tick("optNumber", false);

    tick("optTrail", true);
    check("trailing separator", out(), "ab\nab\nab\n");
    tick("optTrail", false);

    set("sep", "custom");
    check("custom field appears", document.getElementById("customWrap").hidden, false);
    set("sepCustom", " | ");
    check("custom separator", out(), "ab | ab | ab");

    set("sep", "nl");
    set("copies", "0");
    check("zero copies gives nothing", out(), "");

    set("copies", "");
    check("empty count gives nothing", out(), "");

    /* The guard: this would be ~300 million characters */
    set("copies", "100000");
    set("input", "x".repeat(3000));
    check("oversize refused", out(), "");
    check("oversize warns", document.getElementById("msg").className, "msg msg--bad");

    set("input", "ab");
    set("copies", "2");
    check("recovers after refusal", out(), "ab\nab");
    check("warning cleared", document.getElementById("msg").textContent, "");
        /* Ten million copies came back as 100,000 without a word; 2.7 as 2;
       0 and -2 as nothing, also without a word. */
    set("input", "hi"); set("sep", "nl");
    set("copies", "10000000");
    eq("too many copies makes nothing", txt("output"), "");
    has("and says the limit", txt("msg"), "Up to 100,000 copies at a time");
    set("copies", "100000");
    eq("the limit itself works", txt("cCopies"), "100,000");
    eq("with nothing to say", txt("msg"), "");
    set("copies", "2.7");
    eq("a fraction makes nothing", txt("output"), "");
    has("and asks for a whole number", txt("msg"), "whole number");
    set("copies", "0");
    has("zero asks for at least one", txt("msg"), "at least one copy");
    set("copies", "-2");
    has("so does a minus count", txt("msg"), "at least one copy");
    set("copies", "");
    eq("an empty box is quietly nothing", txt("msg"), "");
    set("copies", "3");
    eq("and three is three", txt("output"), "hi\nhi\nhi");
    finish();
"""
# ---- END: the test for text-repeater ----

# ---- START: the test for tip-calculator ----
T["tip-calculator"] = r"""
    set("bill", "1200"); set("tip", "10"); set("people", "1");
    check("total with tip", txt("total"), "1,320");
    check("tip amount", txt("tipAmount"), "120");
    check("one person pays all", txt("perPerson"), "1,320");

    set("people", "4");
    check("split four ways", txt("perPerson"), "330");
    check("tip each", txt("tipEach"), "30");

    set("people", "1"); set("tip", "0");
    check("no tip", txt("total"), "1,200");
    check("zero tip amount", txt("tipAmount"), "0");

    set("tip", "15");
    check("15 percent", txt("total"), "1,380");

    /* Tip on the pre-tax amount only */
    set("tip", "10");
    tick("optNoTax", true);
    set("tax", "200");
    check("tip excludes tax", txt("tipAmount"), "100");
    check("total still includes tax", txt("total"), "1,300");
    set("tax", "5000");
    check("tax larger than bill falls back", txt("tipAmount"), "120");
    tick("optNoTax", false);

    /* Rounding up sends the difference to the tip */
    set("bill", "1234"); set("tip", "10");
    check("unrounded total", txt("total"), "1,357.40");
    tick("optRound", true);
    check("rounded up", txt("total"), "1,358");
    check("rounding goes to the tip", txt("tipAmount"), "124");
    tick("optRound", false);

    /* The quick buttons must actually move the slider */
    document.querySelector("[data-tip='20']").click();
    check("quick button sets 20 percent", txt("tipVal"), "20");
    check("and recalculates", txt("total"), "1,480.80");
        /* A minus bill or tax used to give a minus tip without a word. */
    var DASH = String.fromCharCode(0x2014);
    tick("optRound", false);
    set("bill", "-100");
    eq("a minus bill gives no total", txt("total"), DASH);
    has("and says why", txt("msg"), "The bill cannot be below zero.");
    set("bill", "1000"); set("tip", "10"); set("people", "1");
    tick("optNoTax", true);
    set("tax", "-50");
    has("a minus tax is refused", txt("msg"), "The tax cannot be below zero.");
    eq("with no tip shown", txt("tipAmount"), DASH);
    set("tax", "1500");
    has("a tax bigger than the bill is called a slip", txt("msg"), "is not less than the bill");
    eq("and the tip is on the whole bill", txt("tipAmount"), "100");
    set("tax", "100");
    eq("an ordinary tax is taken off first", txt("tipAmount"), "90");
    has("and said", txt("msg"), "the bill without tax");
    tick("optNoTax", false); set("tax", "0");
    finish();
"""
# ---- END: the test for tip-calculator ----

# ---- START: the test for whitespace-remover ----
T["whitespace-remover"] = r"""
    set("input", "hello    world");
    check("runs collapsed", txt("output"), "hello world");
    set("input", "  padded  ");
    check("trimmed", txt("output"), "padded");
    set("input", "a\n\n\nb");
    tick("optBlankLines", true);
    check("blank lines gone", txt("output"), "a\nb");
    tick("optBlankLines", false);
    set("input", "tab\there");
    check("tab becomes a space", txt("output"), "tab here");
    /* The U+00A0 that breaks so many pasted spreadsheets */
    set("input", "a" + String.fromCharCode(0x00A0) + "b");
    check("non-breaking space handled", txt("output"), "a b");
    set("input", "z" + String.fromCharCode(0x200B) + "z");
    check("zero-width space removed", txt("output"), "zz");
    finish();
"""
# ---- END: the test for whitespace-remover ----

# ---- START: the test for word-counter ----
T["word-counter"] = r"""
    set("input", "Hello world. This is a test.");
    check("words", txt("cWords"), "6");
    check("characters", txt("cChars"), "28");
    check("without spaces", txt("cNoSpace"), "23");
    check("sentences", txt("cSentences"), "2");
    set("input", "one two\n\nthree four");
    check("paragraphs", txt("cParas"), "2");
    set("input", "");
    check("empty resets", txt("cWords"), "0");
    ok("reading time exists", txt("cRead").length > 0);
        /* A dash, "--" or "..." standing alone is not a word. */
    set("input", "e-mail don't 3.14 U.S.A. " + String.fromCharCode(0x2014) + " dash -- x ... ok");
    eq("only pieces with a letter or digit are words", txt("cWords"), "7");
    set("input", "It costs 3.14 today. That is all.");
    eq("the point in 3.14 does not end a sentence", txt("cSentences"), "2");
    var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    set("input", TE + " " + TE + " " + String.fromCharCode(0x0C2D, 0x0C3E, 0x0C37, 0x0C32, 0x0C41));
    has("a Telugu word can be the most used", txt("density"), TE + " " + String.fromCharCode(0x00D7) + "2");
    finish();
"""
# ---- END: the test for word-counter ----

# ---- START: the test for area-converter ----
T["area-converter"] = r"""
    near("1 acre in square feet", txt("out"), 43560);
    near("1 acre in square metres", txt("sqm"), 4046.86, 0.01);
    eq("sixteen units in the table", rows().length, 16);
    eq("no bigha warning for ordinary units", txt("warn"), "");

    set("to", "guntha");
    near("40 guntha make an acre", txt("out"), 40, 0.01);

    set("to", "cent");
    near("100 cents make an acre", txt("out"), 100, 0.01);

    set("from", "guntha"); set("to", "sqft");
    near("one guntha is 1089 sq ft", txt("out"), 1089, 0.5);

    set("from", "hectare"); set("to", "sqm");
    near("one hectare is 10000 sq m", txt("out"), 10000);

    set("from", "sqyd"); set("to", "sqft");
    near("one square yard is 9 sq ft", txt("out"), 9);

    set("from", "acre"); set("to", "kanal");
    near("8 kanal make an acre", txt("out"), 8, 0.01);

    set("from", "kanal"); set("to", "marla");
    near("20 marla make a kanal", txt("out"), 20, 0.01);

    set("from", "bigha16"); set("to", "sqyd");
    near("the 1600 sq yd bigha", txt("out"), 1600, 0.5);
    has("bigha carries a warning", txt("warn"), "no single national value");

    set("from", "acre");
    eq("warning clears for a fixed unit", txt("warn"), "");

    set("amount", "");
    near("an empty box does not crash it", txt("out"), 0);
        /* A minus area used to convert like any other number. */
    var DASH = String.fromCharCode(0x2014);
    set("amount", "-5");
    eq("a minus area gives no answer", txt("out"), DASH);
    eq("nor square metres", txt("sqm"), DASH);
    has("and says why", txt("warn"), "An area cannot be below zero.");
    eq("as an error", document.getElementById("warn").className, "msg msg--bad");
    set("amount", "1");
    eq("a real area clears it", txt("warn"), "");
    eq("back to a quiet hint", document.getElementById("warn").className, "hint");
    finish();
"""
# ---- END: the test for area-converter ----

# ---- START: the test for number-to-words ----
T["number-to-words"] = r"""
    eq("indian words", txt("words"),
       "twelve lakh thirty-four thousand five hundred sixty-seven point five zero");
    eq("indian grouping", txt("grouped"), "12,34,567.50");
    eq("digit count", txt("digits"), "7");
    has("cheque line names the rupees", txt("cheque"),
        "Rupees Twelve Lakh Thirty-Four Thousand Five Hundred Sixty-Seven");
    has("cheque line names the paise", txt("cheque"), "and Fifty Paise Only");
    gone("no error for a valid number", "errWrap");

    set("system", "intl");
    eq("international words", txt("words"),
       "one million two hundred thirty-four thousand five hundred sixty-seven point five zero");
    eq("international grouping", txt("grouped"), "1,234,567.50");

    set("system", "indian"); set("amount", "100000");
    eq("one lakh", txt("words"), "one lakh");
    eq("one lakh, grouped the Indian way", txt("grouped"), "1,00,000");

    set("amount", "10000000");
    eq("one crore", txt("words"), "one crore");

    set("amount", "123456789");
    eq("twelve crore and change", txt("words"),
       "twelve crore thirty-four lakh fifty-six thousand seven hundred eighty-nine");

    set("system", "intl");
    eq("the same number internationally", txt("words"),
       "one hundred twenty-three million four hundred fifty-six thousand seven hundred eighty-nine");

    set("system", "indian"); set("amount", "0");
    eq("zero", txt("words"), "zero");
    eq("zero on a cheque", txt("cheque"), "Rupees Zero Only");

    set("amount", "-5");
    eq("a negative number", txt("words"), "minus five");

    set("amount", "1234567");
    eq("no paise means no point", txt("words"),
       "twelve lakh thirty-four thousand five hundred sixty-seven");
    eq("cheque line without paise", txt("cheque"),
       "Rupees Twelve Lakh Thirty-Four Thousand Five Hundred Sixty-Seven Only");

    set("amount", "10000000000000000");
    shown("past fifteen digits is refused", "errWrap");
    has("and says why", txt("err"), "fifteen digits");

    set("amount", "");
    shown("an empty box is refused", "errWrap");
        /* 1.999 used to become "two" without a word, and -0.001 "minus zero". */
    set("amount", "1.999");
    eq("1.999 is read as two", txt("words"), "two");
    has("and the rounding is said", txt("roundNote"), "1.999 is read as 2.00");
    set("amount", "1.990");
    eq("a nought at the end changes nothing, so no note", txt("roundNote"), "");
    set("amount", "-0.001");
    eq("minus a thousandth rounds to zero, not minus zero", txt("words"), "zero");
    set("amount", "-2.999");
    eq("a minus amount keeps its sign", txt("words"), "minus three");
    has("and its note does too", txt("roundNote"), "-2.999 is read as -3.00");
    /* 543210987654321 times 100 is past what JavaScript holds exactly: it
       used to be read as 543210987654320 and 96 paise. */
    set("amount", "543210987654321");
    eq("fifteen digits keep their last digit", txt("grouped"), "54,32,10,98,76,54,321");
    eq("and gain no paise", txt("words").indexOf("point"), -1);
    has("the words end where the number does", txt("words"), "seventy-six lakh fifty-four thousand three hundred twenty-one");
    eq("and draw no rounding note", txt("roundNote"), "");
    set("amount", "3.47");
    eq("an ordinary amount in paise draws no note", txt("roundNote"), "");
    eq("and reads its paise", txt("words"), "three point four seven");
    set("amount", "0.005");
    has("half a paisa rounds up, and says so", txt("roundNote"), "0.005 is read as 0.01");
    set("amount", "abc");
    eq("a refusal clears the note", txt("roundNote"), "");
    set("amount", "1234567.50");
    finish();
"""
# ---- END: the test for number-to-words ----

# ---- START: the test for temperature-converter ----
T["temperature-converter"] = r"""
    near("37 C is 98.6 F", txt("headline"), 98.6);
    near("celsius tile", txt("oc"), 37);
    near("fahrenheit tile", txt("of"), 98.6);
    near("kelvin tile", txt("ok"), 310.15);
    has("the working is shown", txt("formula"), "9 / 5 + 32");

    set("from", "f"); set("value", "98.6");
    near("98.6 F is body temperature", txt("headline"), 37, 0.05);

    set("value", "212");
    near("212 F is boiling", txt("oc"), 100);

    set("from", "c"); set("value", "-40");
    near("minus 40 is the same on both scales", txt("of"), -40);
    near("and celsius agrees", txt("oc"), -40);

    set("from", "k"); set("value", "0");
    near("absolute zero in celsius", txt("oc"), -273.15);
    near("absolute zero in fahrenheit", txt("of"), -459.67);
    has("rankine is reported", txt("note"), "Rankine");

    set("value", "-5");
    has("below absolute zero is called out", txt("note"), "below absolute zero");

    set("from", "c"); set("value", "0");
    near("water freezes at 32 F", txt("of"), 32);
    near("and at 273.15 K", txt("ok"), 273.15);

    set("from", "r"); set("value", "491.67");
    near("491.67 R is freezing", txt("oc"), 0, 0.01);

    /* Worth its own assertion: zero Rankine really is absolute zero, so an
       empty box read as 0 while Rankine is selected is correct arithmetic
       rather than the crash it first looks like. */
    set("value", "0");
    near("zero rankine is absolute zero", txt("oc"), -273.15);

    set("from", "c"); set("value", "");
    near("an empty box does not crash it", txt("oc"), 0);
    finish();
"""
# ---- END: the test for temperature-converter ----

# ---- START: the test for date-difference-calculator ----
T["date-difference-calculator"] = r"""
    set("start", "2026-01-01"); set("end", "2026-12-31");
    eq("364 days between", txt("days"), "364 days");
    has("calendar breakdown", txt("breakdown"), "0 years, 11 months, 30 days");
    eq("exactly 52 weeks", txt("weeks"), "52w 0d");
    near("working days in 2026", txt("working"), 260);
    near("weekend days in 2026", txt("weekend"), 104);
    eq("six other units", rows().length, 6);
    gone("no error for two real dates", "errWrap");

    tick("inclusive", true);
    eq("counting both ends adds one", txt("days"), "365 days");

    tick("inclusive", false);
    set("start", "2026-12-31"); set("end", "2026-01-01");
    eq("the order does not matter", txt("days"), "364 days");
    has("but the note says so", txt("note"), "earlier");

    set("start", "2026-06-15"); set("end", "2026-06-15");
    eq("the same date is zero", txt("days"), "0 days");

    set("start", "2024-02-01"); set("end", "2024-03-01");
    eq("february in a leap year", txt("days"), "29 days");

    /* This line used to assert "0 months, 28 days" under a label saying
       one month - the test had learnt the bug. The page promises a month. */
    set("start", "2026-01-31"); set("end", "2026-02-28");
    has("31 Jan to 28 Feb is one month", txt("breakdown"), "0 years, 1 month, 0 days");
    set("end", "2026-03-01");
    has("and to 1 March a month and a day, never minus two days", txt("breakdown"), "0 years, 1 month, 1 day");
    set("start", "2024-02-29"); set("end", "2025-02-28");
    has("29 February to 28 February next year is a year", txt("breakdown"), "1 year, 0 months, 0 days");
    set("start", "2026-02-28"); set("end", "2026-03-31");
    has("28 February to 31 March", txt("breakdown"), "0 years, 1 month, 3 days");

    /* 600 random pairs against a count written here independently: step a
       month at a time from the earlier date, each step clamped to the end
       of its month, and stop before passing the later one. */
    (function () {
      var DAYMS = 86400000, seed = 7, wrong = [];
      function rnd(n) { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; }
      function day(t) { return new Date(t).toISOString().slice(0, 10); }
      function step(y, m, d, k) {
        var mm = m + k, yy = y + Math.floor(mm / 12);
        mm = ((mm % 12) + 12) % 12;
        var last = [31, (yy % 4 === 0 && yy % 100 !== 0) || yy % 400 === 0 ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mm];
        return Date.UTC(yy, mm, Math.min(d, last));
      }
      for (var k = 0; k < 600; k++) {
        var t1 = Date.UTC(1990, 0, 1) + rnd(15000) * DAYMS;
        var t2 = t1 + rnd(k % 3 ? 400 : 4000) * DAYMS;
        var a = new Date(t1), y = a.getUTCFullYear(), m = a.getUTCMonth(), d = a.getUTCDate();
        var n = 0;
        while (step(y, m, d, n + 1) <= t2) { n++; }
        var left = Math.round((t2 - step(y, m, d, n)) / DAYMS);
        var want = Math.floor(n / 12) + (Math.floor(n / 12) === 1 ? " year, " : " years, ") +
                   (n % 12) + (n % 12 === 1 ? " month, " : " months, ") +
                   left + (left === 1 ? " day" : " days") + " on the calendar";
        set("start", day(t1)); set("end", day(t2));
        if (txt("breakdown") !== want) { wrong.push(day(t1) + " to " + day(t2) + ": " + txt("breakdown") + " / " + want); }
      }
      eq("600 random gaps agree with a month-by-month count", wrong.length, 0);
      if (wrong.length) { ok("first", false, wrong[0]); }
    })();

    set("start", "");
    shown("a missing date is refused", "errWrap");
    finish();
"""
# ---- END: the test for date-difference-calculator ----

# ---- START: the test for add-subtract-days ----
T["add-subtract-days"] = r"""
    set("start", "2026-01-31"); set("amount", "1"); set("unit", "months");
    eq("31 January plus a month clamps", txt("result"), "28 February 2026");
    eq("and lands on a Saturday", txt("weekday"), "Saturday");
    eq("iso form", txt("isoOut"), "2026-02-28");
    has("the clamp is explained", txt("note"), "pulled back");
    eq("eight milestones", rows().length, 8);
    gone("no error for a real date", "errWrap");

    set("start", "2024-01-31");
    eq("a leap year clamps to the 29th", txt("result"), "29 February 2024");

    set("start", "2026-01-01"); set("unit", "days"); set("amount", "30");
    eq("thirty days on", txt("result"), "31 January 2026");
    eq("no clamp note when counting days", txt("note"), "");

    set("direction", "sub"); set("amount", "1");
    eq("one day back crosses the year", txt("result"), "31 December 2025");

    set("direction", "add"); set("unit", "business"); set("amount", "10");
    eq("ten working days on", txt("result"), "15 January 2026");
    eq("landing on a Thursday", txt("weekday"), "Thursday");
    has("holidays are disclaimed", txt("note"), "Public holidays are not deducted");

    set("unit", "weeks"); set("amount", "2");
    eq("two weeks on", txt("result"), "15 January 2026");

    set("unit", "years"); set("amount", "1");
    eq("a year on", txt("result"), "1 January 2027");

    set("unit", "days"); set("amount", "0");
    eq("zero leaves the date alone", txt("result"), "1 January 2026");

    /* A minus amount used to be read as nothing: the starting date came
       back as the answer. It now counts the other way, and says so. */
    set("start", "2026-10-03"); set("unit", "days"); set("direction", "add"); set("amount", "-5");
    eq("adding minus five days goes back five", txt("result"), "28 September 2026");
    has("the sum shows the way it went", txt("formula"), "minus 5 days");
    has("and the note says why", txt("note"), "A minus amount counts the other way, so this goes backwards.");
    set("direction", "sub");
    eq("subtracting minus five goes forwards", txt("result"), "8 October 2026");
    set("direction", "add");

    /* parseInt read 1.5 as 1. */
    set("amount", "1.5");
    shown("a fraction is refused", "errWrap");
    has("asking for a whole number", txt("err"), "whole number of days");
    eq("with no date shown", txt("result"), String.fromCharCode(0x2014));

    /* Ten million years printed "NaN undefined NaN" and threw. */
    set("unit", "years"); set("amount", "99999999999999999999");
    shown("a date past any calendar is refused", "errWrap");
    has("and the page says how far it goes", txt("err"), "years 1 to 9999");
    set("amount", "7973");
    eq("9999 is still reachable", txt("result"), "3 October 9999");
    gone("without an error", "errWrap");
    set("amount", "7974");
    shown("10000 is not", "errWrap");
    set("direction", "sub"); set("amount", "2025");
    eq("year 1 is reachable going back", txt("result"), "3 October 1");
    set("amount", "2026");
    shown("year 0 is not", "errWrap");
    set("direction", "add");

    /* The working-day loop stopped after 200,000 steps and printed wherever
       it had reached. */
    set("unit", "business"); set("amount", "150000");
    shown("too many working days is refused", "errWrap");
    has("with the limit named", txt("err"), "100,000 working days");
    set("amount", "100000");
    gone("the limit itself is fine", "errWrap");
    /* Worked out separately in Python, counting Monday to Friday. */
    eq("and lands where 100,000 working days lead", txt("isoOut"), "2410-01-22");
    finish();
"""
# ---- END: the test for add-subtract-days ----

# ---- START: the test for sip-calculator ----
T["sip-calculator"] = r"""
    near("5000 a month, 12%, 10 years", txt("maturity"), 1161695, 2);
    near("what you put in", txt("invested"), 600000);
    near("what it earned", txt("returns"), 561695, 2);
    eq("ten rows", rows().length, 10);
    near("year 1 invested", cell(0, 1), 60000);
    near("year 10 value", cell(9, 2), 1161695, 2);
    has("the assumption is stated on the page", txt("note"), "not guaranteed");

    set("stepup", "10");
    near("a 10% step-up changes the maturity", txt("maturity"), 1687163, 5);
    near("and you invest more too", txt("invested"), 956245, 5);
    has("step-up mentioned in the summary", txt("formula"), "rising");

    set("stepup", "0"); set("rate", "0");
    near("no return means you get back what you put in", txt("maturity"), 600000);
    near("and earn nothing", txt("returns"), 0);

    set("rate", "12"); set("years", "1");
    eq("one year, one row", rows().length, 1);

    set("monthly", "0");
    near("nothing invested, nothing back", txt("maturity"), 0);

    set("monthly", "");
    near("an empty box does not crash it", txt("maturity"), 0);
        /* 2.5 years quietly became 2, and a minus step-up or return became 0%. */
    var DASH = String.fromCharCode(0x2014);
    set("monthly", "5000"); set("rate", "12"); set("stepup", "0"); set("years", "2.5");
    near("2.5 years is 30 instalments", txt("invested"), 150000);
    /* 5000 a month at 1% a month for 30 months, paid at the start of each:
       5000 x 1.01 x (1.01^30 - 1) / 0.01 = 175,664 (worked in Python). */
    near("growing for all thirty months", txt("maturity"), 175664, 1);
    eq("two whole years and a part year in the table", rows().length, 3);
    eq("the part year says how long it is", cell(2, 0), "Year 3 (6 months)");
    has("and so does the summary", txt("formula"), "for 2 years and 6 months at");

    set("years", "10"); set("stepup", "-10");
    /* Worked in Python: 5000, falling 10% each anniversary, 1% a month. */
    near("a falling instalment is worked out, not ignored", txt("invested"), 390793, 2);
    near("and grows to what Python works out", txt("maturity"), 833449, 2);
    has("and named as falling", txt("formula"), "falling 10.00% each year");
    set("stepup", "0"); set("rate", "-6");
    ok("a minus return loses money", parseFloat(txt("maturity").replace(/[^0-9.]/g, "")) < 600000, txt("maturity"));
    set("rate", "12");

    set("years", "80");
    eq("past fifty years is refused", txt("maturity"), DASH);
    has("with the limit", txt("formula"), "Up to 50 years");
    set("years", "10"); set("monthly", "-5");
    eq("a minus amount is refused", txt("maturity"), DASH);
    has("with the reason", txt("formula"), "cannot be below zero");
    set("monthly", "5000"); set("rate", "-100");
    eq("a return of -100% is refused", txt("maturity"), DASH);
    set("rate", "12");
    near("and back to the example", txt("maturity"), 1161695, 2);
      set("monthly", "5000"); set("rate", "12"); set("years", "10"); set("stepup", "0");
  eq("what you put in is grouped in lakhs", txt("invested"), String.fromCharCode(0x20B9) + "6,00,000");
  ok("and so is what it grows to", /^\d{1,2}(,\d\d)+,\d{3}$/.test(txt("maturity").slice(1)), txt("maturity"));
  finish();
"""
# ---- END: the test for sip-calculator ----

# ---- START: the test for unit-converter ----
T["unit-converter"] = r"""
    near("1 metre is 100 cm", txt("out"), 100);
    eq("nine length units", rows().length, 9);

    set("to", "in");
    near("1 metre in inches", txt("out"), 39.3701, 0.001);

    set("from", "in"); set("to", "cm"); set("amount", "1");
    near("an inch is exactly 2.54 cm", txt("out"), 2.54, 0.0001);

    set("from", "mi"); set("to", "km");
    near("a mile is 1.609344 km", txt("out"), 1.6093, 0.001);

    set("kind", "weight");
    eq("seven weight units", rows().length, 7);
    near("1 kg in pounds", txt("out"), 2.2046, 0.001);
    set("from", "lb"); set("to", "kg");
    near("a pound is exactly 0.45359237 kg", txt("out"), 0.4536, 0.0001);

    set("kind", "volume");
    eq("eleven volume units", rows().length, 11);
    near("1 litre is 1000 ml", txt("out"), 1000);

    set("from", "galus"); set("to", "l");
    near("a US gallon is 3.785 litres", txt("out"), 3.7854, 0.001);
    set("from", "galimp");
    near("an imperial gallon is 4.546 litres", txt("out"), 4.5461, 0.001);

    set("from", "cupus"); set("to", "ml");
    near("a US cup is 236.6 ml", txt("out"), 236.5882, 0.01);
    set("from", "cupm");
    near("a metric cup is a round 250 ml", txt("out"), 250);

    set("kind", "length");
    has("it says which base unit is used", txt("note"), "metre");

    set("amount", "");
    near("an empty box does not crash it", txt("out"), 0);
        set("kind", "length"); set("amount", "0.0000001");
    has("a tiny amount is not shown as 0", txt("formula"), "0.0000001");
    set("amount", "1");
    finish();
"""
# ---- END: the test for unit-converter ----

# ---- START: the test for binary-decimal-hex-converter ----
T["binary-decimal-hex-converter"] = r"""
    eq("255 in binary", txt("binOut"), "11111111");
    eq("255 in octal", txt("octOut"), "377");
    eq("255 in decimal", txt("decOut"), "255");
    eq("255 in hex", txt("hexOut"), "FF");
    eq("grouped into a byte", txt("grouped"), "11111111");
    eq("eight bits", txt("bits"), "8");
    eq("one byte", txt("bytes"), "1");
    eq("fits a uint8", txt("fits"), "uint8");
    gone("no error for a valid number", "errWrap");

    set("value", "1011"); set("base", "2");
    eq("binary 1011 is 11", txt("decOut"), "11");
    eq("and B in hex", txt("hexOut"), "B");
    eq("padded to a whole byte", txt("grouped"), "00001011");

    set("base", "16"); set("value", "DEADBEEF");
    eq("hex DEADBEEF in decimal", txt("decOut"), "3735928559");
    eq("32 bits", txt("bits"), "32");

    set("value", "0xdeadbeef");
    eq("a 0x prefix is accepted", txt("decOut"), "3735928559");

    set("base", "10"); set("value", "9007199254740993");
    eq("past the safe integer limit, exactly", txt("decOut"), "9007199254740993");
    eq("and its hex is exact too", txt("hexOut"), "20000000000001");

    set("value", "0");
    eq("zero", txt("binOut"), "0");
    eq("zero is one bit wide by convention", txt("bits"), "1");

    set("value", "-10");
    eq("a negative number keeps its sign", txt("binOut"), "-1010");
    eq("and is offered a signed type", txt("fits"), "int8");

    set("base", "2"); set("value", "1012");
    shown("a 2 in binary is refused", "errWrap");
    has("and says what is allowed", txt("err"), "0 and 1 only");

    set("value", "");
    shown("an empty box is refused", "errWrap");
    finish();
"""
# ---- END: the test for binary-decimal-hex-converter ----

# ---- START: the test for timestamp-converter ----
T["timestamp-converter"] = r"""
    has("1700000000 is Nov 2023", txt("utcOut"), "14 Nov 2023");
    has("at 22:13:20 UTC", txt("utcOut"), "22:13:20");
    eq("seconds echoed back", txt("secOut"), "1700000000");
    eq("milliseconds", txt("msOut"), "1700000000000");
    has("read as seconds", txt("detected"), "seconds");
    has("ISO 8601", txt("isoOut"), "2023-11-14T22:13:20");
    gone("no error for a valid timestamp", "errWrap");

    set("stamp", "0");
    has("zero is the epoch itself", txt("utcOut"), "1 Jan 1970");

    set("stamp", "1000000000");
    has("a billion seconds is Sep 2001", txt("utcOut"), "9 Sep 2001");

    set("stamp", "1700000000000");
    has("thirteen digits are read as milliseconds", txt("detected"), "milliseconds");
    has("and give the same moment", txt("utcOut"), "14 Nov 2023");

    set("stamp", "notanumber");
    shown("letters are refused", "errWrap");

    set("stamp", "");
    shown("an empty box is refused", "errWrap");

    set("date", "2026-01-01"); set("time", "00:00"); set("zone", "utc");
    eq("2026 new year in UTC", txt("stampOut"), "1767225600");
    has("milliseconds given too", txt("stampNote"), "1767225600000");

    set("date", "1970-01-01");
    eq("the epoch itself is zero", txt("stampOut"), "0");

    set("date", "2038-01-19"); set("time", "03:14:07");
    eq("the 32-bit ceiling", txt("stampOut"), "2147483647");

    set("date", "");
    shown("a missing date is refused", "dateErrWrap");
        /* Microseconds were read as milliseconds (the year 55840); nanoseconds
       and fractions of a second were refused. */
    set("stamp", "1700000000000000");
    has("sixteen digits are microseconds", txt("detected"), "Read as microseconds - it has 16 digits.");
    has("and the same moment", txt("utcOut"), "14 Nov 2023, 22:13:20");
    set("stamp", "1700000000000000000");
    has("nineteen are nanoseconds", txt("detected"), "nanoseconds");
    has("the same moment again", txt("utcOut"), "14 Nov 2023, 22:13:20");
    set("stamp", "1700000000.5");
    has("a fraction of a second is fine", txt("detected"), "seconds - it has 10 digits before the point");
    eq("and keeps its half second", txt("msOut"), "1700000000500");
    set("stamp", "1700000000");
    finish();
"""
# ---- END: the test for timestamp-converter ----

# ---- START: the test for days-until-countdown ----
T["days-until-countdown"] = r"""
    /* Built from the same clock the page reads, so the assertion stays true
       whatever day the suite is run on. */
    var today = new Date();
    function isoPlus(days) {
      var d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + days);
      return d.getFullYear() + "-" +
             String(d.getMonth() + 1).padStart(2, "0") + "-" +
             String(d.getDate()).padStart(2, "0");
    }

    set("target", isoPlus(0));
    eq("today reads as today", txt("days"), "Today");

    set("target", isoPlus(1));
    eq("tomorrow is one day", txt("days"), "1 day to go");

    set("target", isoPlus(30));
    eq("thirty days out", txt("days"), "30 days to go");
    eq("which is 4 weeks and 2 days", txt("weeks"), "4w 2d");
    has("the target is spelled out", txt("targetText"), String(new Date(today.getFullYear(), today.getMonth(), today.getDate() + 30).getFullYear()));

    set("target", isoPlus(-5));
    eq("a past date counts up", txt("days"), "5 days ago");
    has("and the note says it has passed", txt("note"), "has passed");

    set("target", isoPlus(7));
    eq("a week out", txt("days"), "7 days to go");
    eq("one week, no spare days", txt("weeks"), "1w 0d");
    near("five working days in a week", txt("working"), 5);

    has("the live line ticks in seconds", txt("live"), "seconds");

    document.getElementById("newYearBtn").click();
    eq("the new year button jumps to 1 January", document.getElementById("target").value,
       (today.getFullYear() + 1) + "-01-01");

    set("target", "");
    shown("a missing date is refused", "errWrap");
        /* "Today" above and "That moment has passed" below read as a
       contradiction. */
    var now = new Date();
    function two(n) { return (n < 10 ? "0" : "") + n; }
    set("target", now.getFullYear() + "-" + two(now.getMonth() + 1) + "-" + two(now.getDate()));
    set("attime", "00:00");
    waitFor("today at midnight has passed",
      function () { return txt("note").indexOf("That is today") === 0; },
      function () {
        has("and the note says so plainly", txt("note"), "00:00 has already gone by");
        finish();
      }, 3000);
"""
# ---- END: the test for days-until-countdown ----

# ---- START: the test for roman-numeral-converter ----
T["roman-numeral-converter"] = r"""
    set("input", "1994");
    eq("1994 is MCMXCIV", txt("asRoman"), "MCMXCIV");
    eq("and reads back as 1,994", txt("asNumber"), "1,994");
    has("with the sum written out", txt("breakdown"), "M (1000) + CM (900) + XC (90) + IV (4)");

    set("input", "4");
    eq("four is IV, never IIII", txt("asRoman"), "IV");
    set("input", "9");
    eq("nine is IX", txt("asRoman"), "IX");
    set("input", "40");
    eq("forty is XL", txt("asRoman"), "XL");
    set("input", "444");
    eq("444 needs three subtractive pairs", txt("asRoman"), "CDXLIV");
    set("input", "3999");
    eq("the largest is MMMCMXCIX", txt("asRoman"), "MMMCMXCIX");

    set("input", "4000");
    shown("4000 is refused", "errWrap");
    has("and says where the letters run out", txt("err"), "3999 is as far");
    set("input", "0");
    shown("zero is refused", "errWrap");
    has("because the system has no symbol for it", txt("err"), "no symbol for zero");

    set("input", "MCMXCIV");
    eq("MCMXCIV reads as 1,994", txt("asNumber"), "1,994");
    gone("and is accepted without complaint", "errWrap");
    set("input", "mcmxciv");
    eq("lower case is accepted too", txt("asNumber"), "1,994");

    /* The two famous non-standard spellings. Both are understood, and both are
       corrected - refusing them outright would leave somebody holding a clock
       face with no idea what it says. */
    set("input", "IIII");
    eq("IIII is understood as 4", txt("asNumber"), "4");
    has("and corrected to IV", txt("err"), "Written properly it is IV");
    set("input", "IC");
    eq("IC is understood as 99", txt("asNumber"), "99");
    has("and corrected to XCIX", txt("err"), "XCIX");
    set("input", "XCIX");
    gone("XCIX itself is the standard spelling", "errWrap");

    set("input", "3.5");
    has("a fraction gets its own message", txt("err"), "no way to write a fraction");
    set("input", "ABC");
    has("junk letters are named as junk", txt("err"), "only the letters");

    /* ---- START: the round trip ----
       Every number in range, converted and read back, against a reference
       written here rather than borrowed from the page. Two tables that agree
       prove nothing if one was copied from the other. */
    var SYM = [[1000,"M"],[900,"CM"],[500,"D"],[400,"CD"],[100,"C"],[90,"XC"],
               [50,"L"],[40,"XL"],[10,"X"],[9,"IX"],[5,"V"],[4,"IV"],[1,"I"]];
    function ref(n) {
      var s = "";
      for (var i = 0; i < SYM.length; i++) {
        while (n >= SYM[i][0]) { s += SYM[i][1]; n -= SYM[i][0]; }
      }
      return s;
    }
    var wrong = 0, firstWrong = "";
    for (var y = 1; y <= 3999; y++) {
      set("input", String(y));
      var got = txt("asRoman");
      if (got !== ref(y)) {
        wrong++;
        if (!firstWrong) { firstWrong = y + " gave " + got + ", wanted " + ref(y); }
      }
      set("input", got);
      if (txt("asNumber").replace(/,/g, "") !== String(y)) {
        wrong++;
        if (!firstWrong) { firstWrong = got + " read back as " + txt("asNumber"); }
      }
    }
    ok("all 3999 numbers convert and read back correctly", wrong === 0,
       wrong + " wrong, first: " + firstWrong);
    /* ---- END: the round trip ---- */

    click("yearBtn");
    eq("the year button fills in this year", val("input"),
       String(new Date().getFullYear()));
    finish();
"""
# ---- END: the test for roman-numeral-converter ----

# ---- START: the test for data-storage-converter ----
T["data-storage-converter"] = r"""
    set("from", "tb");
    set("value", "1");
    eq("1 TB is a trillion bytes", txt("asBytes"), "1,000,000,000,000");
    eq("which is 1,000 GB on the box", txt("asGb"), "1,000 GB");
    eq("and 931.32 GiB to an operating system", txt("asGib"), "931.32 GiB");
    has("the note explains the 1024 division", txt("note"), "divides by 1024");

    set("from", "gb");
    set("value", "500");
    eq("500 GB reads as 465.66 GiB", txt("asGib"), "465.66 GiB");

    set("from", "kib");
    set("value", "1");
    eq("a kibibyte is 1,024 bytes", txt("asBytes"), "1,024");
    set("from", "kb");
    eq("a kilobyte is 1,000 bytes", txt("asBytes"), "1,000");
    set("from", "gib");
    eq("a gibibyte is 1,073,741,824 bytes", txt("asBytes"), "1,073,741,824");

    set("from", "bit");
    set("value", "8");
    eq("eight bits make one byte", txt("asBytes"), "1");

    set("from", "mbit");
    set("value", "100");
    eq("100 Mbps carries 12.5 MB a second", txt("headline"), "12.5 MB");
    has("and the note gives the reason", txt("note"), "8 bits in a byte");

    /* The headline has to pick a unit a person would use. A fixed GiB makes a
       kilobyte read as 0.0000009313 GiB, which is true and useless. */
    set("from", "kb");
    set("value", "1");
    eq("a small size gets a small unit, not GiB", txt("headline"), "1,000 B");
    set("from", "tb");
    set("value", "2");
    eq("and a large one gets TiB", txt("headline"), "1.819 TiB");

    set("from", "tb");
    set("value", "1");
    eq("the table lists all thirteen units", rows().length, 13);
    eq("1 TB is exactly 976,562,500 KiB", cell(3, 1), "976,562,500");
    eq("and exactly 8 trillion bits", cell(0, 1), "8,000,000,000,000");

    /* Past 2^53 a JavaScript number cannot hold every integer. Printing all
       those digits anyway would look exact and be wrong. */
    set("from", "pb");
    set("value", "1000");
    has("a byte count past 2^53 is marked approximate", txt("asBytes"),
        String.fromCharCode(0x2248));
    has("and the note says it is rounded", txt("note"), "rounded");

    set("from", "gb");
    set("value", "");
    shown("an empty box is refused", "errWrap");
    set("value", "-5");
    shown("a negative is flagged", "errWrap");
    eq("but converted as the size that was obviously meant", txt("asGb"), "5 GB");
        /* One bit was "≈ 1.2500e-1" bytes, "too large for exact arithmetic". */
    set("from", "bit"); set("value", "1");
    eq("one bit is an eighth of a byte", txt("asBytes"), "0.125");
    has("and the note says why", txt("note"), "not a whole number of bytes");
    eq("without calling it too large", txt("note").indexOf("too large"), -1);
    set("from", "tb");
    finish();
"""
# ---- END: the test for data-storage-converter ----

# ---- START: the test for speed-converter ----
T["speed-converter"] = r"""
    set("from", "kmh");
    set("value", "100");
    eq("100 km/h is 62.14 mph", txt("asMph"), "62.14 mph");
    eq("and 27.78 m/s", txt("asMs"), "27.78 m/s");
    has("with the five-eighths shortcut alongside the exact figure", txt("note"), "62.5");

    set("value", "12");
    eq("12 km/h is a five minute kilometre", txt("paceKm"), "5:00");
    eq("which is an 8:03 mile", txt("paceMile"), "8:03");
    /* A marathon at this pace is three and a half HOURS. Printed as minutes it
       would read 210:59, which is the same number and no use to a runner. */
    has("and a marathon time written in hours", txt("paceNote"), "3:30:59");

    set("value", "20");
    eq("20 km/h is a three minute kilometre", txt("paceKm"), "3:00");
    has("and a 2:06:35 marathon", txt("paceNote"), "2:06:35");

    set("value", "0");
    eq("a speed of zero has no pace", txt("paceKm"), String.fromCharCode(0x2014));
    has("and the page says why rather than dividing", txt("paceNote"), "never covers one");

    set("value", "-20");
    eq("a negative speed still converts",
       txt("asMph"), String.fromCharCode(0x2212) + "12.43 mph");
    has("but has no pace", txt("paceNote"), "needs a forward speed");

    set("from", "knot");
    set("value", "1");
    eq("one knot is 1.85 km/h", txt("asKmh"), "1.85 km/h");
    has("and the note gives the exact definition", txt("note"), "1852 metres");

    set("from", "mach");
    set("value", "1");
    eq("Mach 1 at sea level is 1,225.04 km/h", txt("asKmh"), "1,225.04 km/h");
    eq("which is 761.21 mph", txt("asMph"), "761.21 mph");
    has("with the caveat that it changes with temperature", txt("note"), "sea level");

    set("from", "ms");
    set("value", "1");
    eq("1 m/s is 3.6 km/h", txt("asKmh"), "3.6 km/h");
    eq("and 2.24 mph", txt("asMph"), "2.24 mph");

    set("from", "fts");
    set("value", "100");
    eq("100 ft/s is 109.73 km/h", txt("asKmh"), "109.73 km/h");

    eq("the table lists six units", rows().length, 6);

    set("value", "");
    shown("an empty box is refused", "errWrap");
    finish();
"""
# ---- END: the test for speed-converter ----

# ---- START: the test for leap-year-checker ----
T["leap-year-checker"] = r"""
    set("year", "2024");
    has("2024 is a leap year", txt("headline"), "2024 is a leap year");
    eq("February has 29 days", txt("febDays"), "29");
    eq("and the year runs to 366", txt("yearDays"), "366");

    set("year", "2026");
    has("2026 is not", txt("headline"), "is not a leap year");
    eq("February has 28 days", txt("febDays"), "28");
    eq("the year runs to 365", txt("yearDays"), "365");
    eq("and the next leap year is 2028", txt("nextLeap"), "2028");

    /* The pair the whole rule exists for. */
    set("year", "1900");
    has("1900 was not a leap year", txt("headline"), "is not a leap year");
    has("because it divides by 100 and not by 400", txt("reading"),
        "divides by 100 but not by 400");
    set("year", "2000");
    has("2000 was", txt("headline"), "is a leap year");
    has("because it divides by 400", txt("reading"), "divides by 400");
    set("year", "2100");
    has("2100 will not be", txt("headline"), "is not a leap year");
    eq("and the next one after it is 2104", txt("nextLeap"), "2104");

    set("year", "2026");
    click("nextBtn");
    eq("the next button jumps to 2028", val("year"), "2028");
    click("prevBtn");
    eq("and the previous button comes back to 2024", val("year"), "2024");
    set("year", "2096");
    click("nextBtn");
    eq("stepping forward from 2096 skips over 2100", val("year"), "2104");

    set("year", "abc");
    shown("junk is refused", "errWrap");
    eq("and the tiles are cleared with it", txt("febDays"),
       String.fromCharCode(0x2014));
    set("year", "2024");
    set("year", "99999");
    shown("a year outside the range is refused", "errWrap");
    eq("and does not leave the last answer sitting there", txt("febDays"),
       String.fromCharCode(0x2014));

    eq("the century table has eight rows",
       document.querySelectorAll("#centuries tr").length, 8);

    /* ---- START: six hundred years against the rule ----
       The reference is written out here rather than read off the page, so the
       two can actually disagree. */
    function refLeap(y) { return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0; }
    var wrong = 0, firstWrong = "";
    for (var y = 1800; y <= 2400; y++) {
      set("year", String(y));
      var says = txt("headline").indexOf("is not") === -1;
      if (says !== refLeap(y)) {
        wrong++;
        if (!firstWrong) { firstWrong = String(y); }
      }
    }
    ok("every year from 1800 to 2400 matches the rule", wrong === 0,
       wrong + " wrong, first " + firstWrong);
    /* ---- END: six hundred years against the rule ---- */
        /* The Gregorian calendar began in 1582; earlier years were read with no
       word that the rule is carried backwards. */
    set("year", "1500");
    has("1500 says the Gregorian rule is carried backwards", txt("why"), "The Gregorian calendar began in 1582");
    has("and that the Julian calendar made it a leap year", txt("why"), "by that calendar 1500 was a leap year");
    set("year", "1200");
    has("1200 is a leap year under both", txt("why"), "every fourth year.");
    eq("so there is no contrast to draw", txt("why").indexOf("by that calendar"), -1);
    set("year", "1583");
    eq("from 1583 there is no note", txt("why").indexOf("Gregorian calendar began"), -1);
    set("year", "2024");
    finish();
"""
# ---- END: the test for leap-year-checker ----

# ---- START: the test for week-number-calculator ----
T["week-number-calculator"] = r"""
    /* Known-correct ISO week numbers, including every awkward case: a year
       starting mid-week, 31 December landing in the next year's week 1, and
       1 January landing in the previous year's week 53. */
    var CASES = [
      ["2026-01-01", "2026-W01"], ["2027-01-01", "2026-W53"],
      ["2024-12-31", "2025-W01"], ["2025-12-29", "2026-W01"],
      ["2021-01-01", "2020-W53"], ["2016-01-03", "2015-W53"],
      ["2016-01-04", "2016-W01"], ["2000-01-01", "1999-W52"],
      ["2026-09-21", "2026-W39"], ["2005-01-01", "2004-W53"],
      ["2008-12-29", "2009-W01"], ["2010-01-03", "2009-W53"],
      ["2010-01-04", "2010-W01"], ["1999-12-31", "1999-W52"],
      ["2020-12-31", "2020-W53"]
    ];
    for (var i = 0; i < CASES.length; i++) {
      set("date", CASES[i][0]);
      eq(CASES[i][0] + " is " + CASES[i][1], txt("headline"), CASES[i][1]);
    }

    set("date", "2027-01-01");
    has("a week-year that differs from the calendar year is called out",
        txt("note"), "belongs to 2026");

    set("date", "2026-09-21");
    has("the range of the week is shown", txt("range"), "21 September 2026");
    has("with how many weeks the year has", txt("range"), "53 ISO weeks");
    has("and the day of the week is named", txt("reading"), "Monday");

    set("wYear", "2026");
    set("wNum", "1");
    /* A week straddling New Year must carry its years, or "29 Dec - 4 Jan"
       leaves the reader guessing at exactly the week where guessing fails. */
    eq("2026 week 1 starts in December 2025", txt("wRange"),
       "29 Dec 2025 " + String.fromCharCode(0x2013) + " 4 Jan 2026");
    eq("and it lists seven days", document.querySelectorAll("#wDays tr").length, 7);
    eq("beginning on Monday 29 December 2025",
       document.querySelectorAll("#wDays tr")[0].children[1].textContent,
       "29 December 2025");
    eq("and ending on Sunday 4 January 2026",
       document.querySelectorAll("#wDays tr")[6].children[1].textContent,
       "4 January 2026");

    set("wNum", "39");
    eq("a mid-year week needs no years on it", txt("wRange"),
       "21 Sep " + String.fromCharCode(0x2013) + " 27 Sep");

    set("wYear", "2025");
    set("wNum", "53");
    shown("2025 has no week 53", "wErrWrap");
    has("and the page says how many it does have", txt("wErr"), "only 52");
    set("wYear", "2026");
    gone("2026 does have one", "wErrWrap");
    set("wNum", "0");
    shown("week zero is refused", "wErrWrap");

    set("date", "");
    shown("an empty date is refused", "errWrap");
    finish();
"""
# ---- END: the test for week-number-calculator ----

# ---- START: the test for salary-calculator ----
T["salary-calculator"] = r"""
  /* Default package: 12,00,000 CTC, basic 40%, metro, 12% PF, gratuity on,
     200 professional tax, no TDS. Worked by hand:
       basic     4,80,000      hra       2,40,000
       pf          57,600      gratuity     23,088
       special   3,99,312      gross    11,19,312
       net      10,59,312  ->  88,276 a month */
  near("default in-hand a month", txt("inHand"), 88276, 1);
  near("default gross a month", txt("grossMonth"), 93276, 1);
  near("default deducted a month", txt("cutMonth"), 5000, 1);
  near("default in-hand a year", txt("inHandYear"), 1059312, 1);

  eq("ten rows in the breakup", rows().length, 10);
  near("basic is 40% of CTC", cell(0, 1), 480000);
  near("HRA is half of basic in a metro", cell(1, 1), 240000);
  near("special allowance balances the package", cell(2, 1), 399312, 1);
  near("employer PF is 12% of basic", cell(3, 1), 57600);
  near("gratuity is 4.81% of basic", cell(4, 1), 23088, 1);
  near("gross excludes PF and gratuity", cell(5, 1), 1119312, 1);
  near("take-home row matches the headline", cell(9, 1), 1059312, 1);
  near("monthly column divides by twelve", cell(0, 2), 40000);

  /* A deduction is printed with the minus in front of the rupee sign, and
     a zero is plain zero. It used to read "Rs-57,600" and "Rs-0", with the
     rupee sign where Rs is here. */
  var M = String.fromCharCode(0x2212), R = String.fromCharCode(0x20B9);
  eq("your PF is shown as a minus", cell(6, 1), M + R + "57,600");
  eq("and so is each month of it", cell(6, 2), M + R + "4,800");
  eq("no income tax is plain zero, not minus zero", cell(8, 1), R + "0");
  eq("in the monthly column too", cell(8, 2), R + "0");
  eq("a positive row has no sign, and lakhs are grouped as lakhs", cell(0, 1), R + "4,80,000");
  ok("no row anywhere prints a sign after the rupee sign",
     Array.prototype.every.call(rows(), function (r) {
       return r.textContent.indexOf(R + "-") === -1 && r.textContent.indexOf(R + M) === -1;
     }), document.getElementById("rows").textContent);

  /* Non-metro drops HRA to 40% of basic, but HRA is inside CTC either way,
     so take-home must not move - only the special allowance absorbs it. */
  set("city", "nonmetro");
  near("non-metro HRA", cell(1, 1), 192000);
  near("special allowance absorbs the difference", cell(2, 1), 447312, 1);
  near("take-home is unchanged by the HRA split", txt("inHand"), 88276, 1);
  set("city", "metro");

  /* A higher basic means more PF and gratuity, so LESS in hand. This is the
     thing the page exists to show, so it is worth asserting. */
  set("basicPct", 50);
  near("50% basic raises employer PF", cell(3, 1), 72000);
  near("50% basic raises gratuity", cell(4, 1), 28860, 1);
  /* Gross 10,99,140 less PF 72,000 less professional tax 2,400 = 10,24,740,
     or 85,395 a month - nearly 2,900 LESS than at 40% basic, on the same CTC.
     That is the whole point of the page, so it is asserted rather than
     explained. */
  near("50% basic lowers take-home", txt("inHand"), 85395, 2);
  set("basicPct", 40);

  set("pfMode", "none");
  near("no PF means no PF row", cell(3, 1), 0);
  near("no PF raises take-home", txt("inHand"), 97876, 2);

  set("pfMode", "capped");
  near("capped PF is 1,800 a month", cell(3, 1), 21600);

  set("pfMode", "percent");
  tick("hasGratuity", false);
  near("gratuity off zeroes the row", cell(4, 1), 0);
  near("gratuity off raises gross", cell(5, 1), 1142400, 1);

  tick("hasGratuity", true);
  set("tds", 5000);
  near("TDS cuts take-home directly", txt("inHand"), 83276, 1);
  near("and shows in the deductions tile", txt("cutMonth"), 10000, 1);
  set("tds", 0);

  set("ptax", 0);
  near("zero professional tax", txt("cutMonth"), 4800, 1);
  set("ptax", 200);

  /* An impossible basic must say so rather than print a negative row. */
  set("basicPct", 95);
  has("impossible basic is explained", txt("msg"), "too high");
  eq("and the negative balancing row reads as a minus", cell(2, 1).charAt(0) + cell(2, 1).charAt(1), M + R);
  set("basicPct", 40);
  eq("and the warning clears", txt("msg"), "");

  set("ctc", "");
  near("an empty CTC does not crash", txt("inHand"), -200, 1);
    finish();
"""
# ---- END: the test for salary-calculator ----

# ---- START: the test for fuel-cost-calculator ----
T["fuel-cost-calculator"] = r"""
  /* Default journey: 20 km each way, return trip, 18 km/l, 105 a litre.
     40 km / 18 = 2.2222 litres, x 105 = 233.33 for the trip. */
  near("default trip cost", txt("tripCost"), 233, 1);
  near("default litres", txt("litres"), 2.22, 0.01);
  near("default cost per km", txt("perKm"), 5.83, 0.01);
  near("one person pays the whole trip", txt("perPerson"), 233, 1);
  has("the label says it is a return trip", txt("tripLabel"), "return");
  has("and names the real distance", txt("tripLabel"), "40");

  eq("three rows", rows().length, 3);
  near("one trip litres", cell(0, 1), 2.22, 0.01);
  near("one trip cost", cell(0, 2), 233, 1);
  near("a month is 22 trips", cell(1, 2), 5133, 2);
  near("a year is twelve months", cell(2, 2), 61600, 5);

  /* Untick the return trip and everything must halve. */
  tick("returnTrip", false);
  near("one way halves the cost", txt("tripCost"), 117, 1);
  near("one way halves the litres", txt("litres"), 1.11, 0.01);
  near("cost per km is unchanged by direction", txt("perKm"), 5.83, 0.01);
  has("the label drops the word return", txt("tripLabel"), "one trip of 20");
  tick("returnTrip", true);

  set("people", 4);
  near("four people split the trip", txt("perPerson"), 58, 1);
  near("but the trip itself costs the same", txt("tripCost"), 233, 1);
  set("people", 1);

  /* Better mileage, less fuel. 40 / 25 = 1.6 litres, x 105 = 168. */
  set("mileage", 25);
  near("better mileage cuts the cost", txt("tripCost"), 168, 1);
  near("better mileage cuts the litres", txt("litres"), 1.60, 0.01);
  set("mileage", 18);

  set("price", 0);
  near("free fuel costs nothing", txt("tripCost"), 0);
  near("but the litres are still burnt", txt("litres"), 2.22, 0.01);
  set("price", 105);

  set("trips", 0);
  near("no trips means no monthly cost", cell(1, 2), 0);
  near("and no yearly cost", cell(2, 2), 0);
  near("a single trip still costs the same", txt("tripCost"), 233, 1);
  set("trips", 22);

  /* Mileage of zero is a real thing to type on the way to typing 18. It must
     explain itself rather than printing Infinity into every field. */
  set("mileage", 0);
  has("zero mileage is explained", txt("msg"), "above zero");
  near("and nothing becomes Infinity", txt("tripCost"), 0);
  set("mileage", 18);
  eq("the warning clears", txt("msg"), "");

  set("distance", "");
  near("an empty distance does not crash", txt("tripCost"), 0);
    var DASH = String.fromCharCode(0x2014);

    set("distance", "-20");
    eq("a minus distance is refused", txt("tripCost"), DASH);
    has("with the reason", txt("msg"), "cannot be below zero");
    set("distance", "20"); set("people", "0");
    has("nobody paying is refused too", txt("msg"), "At least one person");
    set("people", "1");
    near("and back", txt("tripCost"), 233, 1);
      set("distance", "20000"); set("mileage", "10"); set("price", "100"); set("people", "1"); set("trips", "1");
  ok("rupees are grouped in lakhs", /^\d{1,2}(,\d\d)+,\d{3}$/.test(txt("tripCost").slice(1)), txt("tripCost"));
  finish();
"""
# ---- END: the test for fuel-cost-calculator ----

# ---- START: the test for text-to-morse ----
T["text-to-morse"] = r"""
  /* The page loads with SOS in the box: ... --- ... */
  eq("SOS on load", out(), "... --- ...");
  near("three characters counted", txt("cChars"), 3);
  near("one word", txt("cWords"), 1);
  near("nothing skipped", txt("cSkipped"), 0);

  /* One space between letters, a slash between words. */
  set("input", "HI THERE");
  eq("two words are slash separated", out(), ".... .. / - .... . .-. .");
  near("two words counted", txt("cWords"), 2);

  set("input", "SOS 123");
  eq("digits encode too", out(), "... --- ... / .---- ..--- ...--");

  set("input", "A.");
  eq("punctuation is in the standard", out(), ".- .-.-.-");

  /* Case must not matter - Morse has no lower case. */
  set("input", "hi");
  eq("lower case encodes the same", out(), ".... ..");

  /* Anything with no Morse equivalent is counted, not silently dropped. */
  set("input", "A#B");
  eq("the unsupported character is left out", out(), ".- -...");
  near("and is counted", txt("cSkipped"), 1);
  has("and the reason is on screen", txt("msg"), "no Morse equivalent");

  set("input", "AB");
  eq("the warning clears", txt("msg"), "");

  /* ---- the other direction ---- */
  set("direction", "decode");
  set("input", ".... .. / - .... . .-. .");
  eq("Morse reads back to text", out(), "HI THERE");

  set("input", "... --- ...");
  eq("SOS reads back", out(), "SOS");

  /* A keyboard gives en dashes and the like; those must still decode. */
  set("input", "." + String.fromCharCode(0x2013));
  eq("an en dash counts as a dash", out(), "A");

  set("input", "..... ..... .....");
  eq("digits read back", out(), "555");

  set("input", ".-.-.-.-.-.-");
  near("an unreadable symbol is counted", txt("cSkipped"), 1);

  /* Swap puts the result back in the box and flips the direction, so a
     translation can be checked by turning it straight back. */
  set("direction", "encode");
  set("input", "HELLO");
  eq("encoded", out(), ".... . .-.. .-.. ---");
  click("swapBtn");
  eq("swap flips the direction", val("direction"), "decode");
  eq("swap feeds the result back in", val("input"), ".... . .-.. .-.. ---");
  eq("and it decodes to what we started with", out(), "HELLO");

  click("clearBtn");
  eq("clear empties the output", out(), "");
    finish();
"""
# ---- END: the test for text-to-morse ----

# ---- START: the test for nato-phonetic-converter ----
T["nato-phonetic-converter"] = r"""
  var DASH = " " + String.fromCharCode(0x2014) + " ";

  /* The page loads with PNR 7K4B2 and the letter shown beside each word. */
  has("P becomes Papa", out(), "P" + DASH + "Papa");
  has("N becomes November", out(), "N" + DASH + "November");
  has("R becomes Romeo", out(), "R" + DASH + "Romeo");
  has("K becomes Kilo", out(), "K" + DASH + "Kilo");
  has("B becomes Bravo", out(), "B" + DASH + "Bravo");
  has("7 becomes Seven", out(), "7" + DASH + "Seven");
  has("the word break is marked", out(), "|");
  near("five letters", txt("cLetters"), 5);
  near("three digits", txt("cDigits"), 3);
  near("nothing skipped", txt("cSkipped"), 0);

  /* The ICAO spellings are deliberate and must not be "corrected". */
  set("input", "AJX");
  has("Alfa, not Alpha", out(), "Alfa");
  has("Juliett with two t's", out(), "Juliett");
  has("X-ray keeps its hyphen", out(), "X-ray");

  /* Without the letter shown it is just the words, ready to read aloud. */
  tick("optShowLetter", false);
  set("input", "PNR 7K4B2");
  eq("words only", out(),
     "Papa November Romeo | Seven Kilo Four Bravo Two");

  /* Aviation numbers only change three, four, five and nine. */
  tick("optAviation", true);
  set("input", "3459");
  eq("aviation digits", out(), "Tree Fower Fife Niner");
  tick("optAviation", false);
  eq("plain digits", out(), "Three Four Five Nine");

  /* Case must not matter. */
  set("input", "abc");
  eq("lower case still spells out", out(), "Alfa Bravo Charlie");

  /* One per line, for reading a long reference without losing your place. */
  tick("optLines", true);
  set("input", "AB CD");
  has("a line per character", out(), "Alfa\nBravo");
  has("and a blank line between words", out(), "Bravo\n\nCharlie");
  tick("optLines", false);

  /* Anything with no agreed word is counted, not silently dropped. */
  set("input", "A#B");
  eq("the unsupported character is left out", out(), "Alfa Bravo");
  near("and is counted", txt("cSkipped"), 1);
  has("and the reason is on screen", txt("msg"), "no agreed word");

  set("input", "AB");
  eq("the warning clears", txt("msg"), "");

  /* Leading and trailing spaces must not leave a dangling separator. */
  set("input", "  AB  ");
  eq("surrounding spaces are trimmed", out(), "Alfa Bravo");

  click("clearBtn");
  eq("clear empties the output", out(), "");
  near("and zeroes the counts", txt("cLetters"), 0);
    finish();
"""
# ---- END: the test for nato-phonetic-converter ----

# ---- START: the test for cooking-measurement-converter ----
T["cooking-measurement-converter"] = r"""
  /* Loads as 1 Indian cup (200 ml) of plain flour, density 0.52.
     200 ml x 0.52 = 104 g. */
  near("one Indian cup of flour in grams", txt("mainOut"), 104);
  near("one cup is one cup", txt("oCup"), 1);
  near("200 ml is 13.3 tablespoons", txt("oTbsp"), 13.3, 0.05);
  near("200 ml is 40 teaspoons", txt("oTsp"), 40);

  eq("nine units listed", rows().length, 9);
  near("millilitres row", cell(3, 1), 200);
  near("litres row", cell(4, 1), 0.2, 0.01);
  near("fluid ounces row", cell(5, 1), 6.76, 0.02);
  near("grams row", cell(6, 1), 104);
  near("ounces by weight row", cell(8, 1), 3.67, 0.02);

  /* The cup size is the whole point of the page: the same "1 cup" is a
     different amount in an Indian and an American recipe. */
  set("cupSize", "240");
  near("one US cup of flour is about 125 g", txt("mainOut"), 125, 0.6);
  near("and 240 ml", cell(3, 1), 240);
  set("cupSize", "250");
  near("one metric cup of flour is 130 g", txt("mainOut"), 130);
  set("cupSize", "240");

  /* Same volume, different ingredient, very different weight - the reason the
     ingredient has to be chosen at all. */
  set("ingredient", "sugar");
  near("one US cup of sugar is about 204 g", txt("mainOut"), 204);
  set("ingredient", "water");
  near("one US cup of water is 240 g", txt("mainOut"), 240);
  set("ingredient", "honey");
  near("honey is heavier than water", txt("mainOut"), 341, 1);
  set("ingredient", "flour");

  /* Spoons are metric here: 15 ml and 5 ml. */
  set("unit", "tbsp"); set("amount", 1);
  near("one tablespoon is 15 ml", cell(3, 1), 15);
  near("one tablespoon of flour is 7.8 g", txt("mainOut"), 7.8, 0.05);
  set("unit", "tsp");
  near("one teaspoon is 5 ml", cell(3, 1), 5);

  /* Going the other way: grams in, cups out. */
  set("unit", "g"); set("amount", 240);
  near("240 g of flour is 1.92 US cups", txt("mainOut"), 1.92, 0.02);
  has("and the headline says cups", txt("mainOut"), "cups");
  set("ingredient", "water");
  near("240 g of water is exactly one cup", txt("mainOut"), 1);

  set("unit", "kg"); set("amount", 1);
  near("a kilo of water is 1000 ml", cell(3, 1), 1000);

  /* Scaling must be linear - two cups is twice one cup. */
  set("unit", "cup"); set("ingredient", "flour"); set("amount", 2);
  near("two cups is twice one cup", txt("mainOut"), 249.6, 1);

  set("amount", 0);
  has("zero is refused with a reason", txt("msg"), "above zero");
  set("amount", 1);
  has("and the normal note returns", txt("msg"), "approximate");
    finish();
"""
# ---- END: the test for cooking-measurement-converter ----

# ---- START: the test for shoe-size-converter ----
T["shoe-size-converter"] = r"""
  /* Loads as men's India/UK 8, which is US 9, EU 42, 26.5 cm. */
  has("UK 8 is US 9", txt("mainOut"), "US 9");
  has("and the label names the input", txt("mainLabel"), "India / UK 8");
  eq("UK column", txt("oUk"), "8");
  eq("US column", txt("oUs"), "9");
  eq("EU column", txt("oEu"), "42");
  has("foot length in cm", txt("oCm"), "26.5");
  has("brands vary is the resting message", txt("msg"), "Brands vary");

  eq("five rows around the match", rows().length, 5);

  /* Every system must find the same row, since it is one shoe. */
  set("system", "us"); set("size", 9);
  has("US 9 comes back to UK 8", txt("mainOut"), "India / UK 8");
  eq("EU is still 42", txt("oEu"), "42");

  set("system", "eu"); set("size", 42);
  has("EU 42 comes back to UK 8", txt("mainOut"), "India / UK 8");
  eq("US is still 9", txt("oUs"), "9");

  set("system", "cm"); set("size", 26.5);
  has("26.5 cm comes back to UK 8", txt("mainOut"), "India / UK 8");
  has("and the label says foot length", txt("mainLabel"), "26.5 cm long");

  /* The women's chart is a different chart, not an offset of the same one. */
  set("system", "uk"); set("gender", "women"); set("size", 5);
  has("women's UK 5 is US 7", txt("mainOut"), "US 7");
  eq("women's UK 5 is EU 38", txt("oEu"), "38");
  has("women's UK 5 is 24 cm", txt("oCm"), "24");

  set("gender", "men"); set("size", 5);
  has("men's UK 5 is US 6, not US 7", txt("mainOut"), "US 6");

  /* Half sizes and in-between sizes land on the nearest row, and say so. */
  set("size", 8.5);
  has("UK 8.5 is US 9.5", txt("mainOut"), "US 9.5");
  set("size", 8.2);
  has("an in-between size finds the nearest", txt("mainOut"), "US 9");
  has("and admits it is not exact", txt("msg"), "No exact match");

  /* Off the end of the chart must be said out loud, not silently clamped. */
  set("size", 20);
  has("far too large is flagged", txt("msg"), "outside the chart");
  set("size", 1);
  has("far too small is flagged", txt("msg"), "outside the chart");

  set("size", 8);
  has("and a real size clears it", txt("msg"), "Brands vary");

  set("size", "");
  has("an empty box asks for a size", txt("msg"), "Type a size");
  eq("and the tiles reset", txt("oUk"), String.fromCharCode(0x2014));
    finish();
"""
# ---- END: the test for shoe-size-converter ----

# ---- START: the test for base64-encoder-decoder ----
T["base64-encoder-decoder"] = r"""
  eq("108 ToolBox encodes to MTA4IFRvb2xCb3g=", out(), "MTA4IFRvb2xCb3g=");
  eq("characters in", txt("cIn"), "11");
  eq("characters out", txt("cOut"), "16");
  eq("bytes of data", txt("cBytes"), "11");
  has("and it says out loud that this is not encryption", txt("msg"), "not encryption");

  /* Three bytes in, four characters out - the whole shape of Base64. */
  set("input", "aaa");
  eq("three bytes become four characters", out().length, 4);
  eq("and need no padding", (out().match(/=/g) || []).length, 0);
  set("input", "aaaa");
  eq("four bytes need two groups, so eight characters", out().length, 8);
  eq("and two padding characters mark the short one", (out().match(/=/g) || []).length, 2);

  /* btoa() on its own throws on anything above U+00FF. This must not. */
  set("input", String.fromCodePoint(0x1F600));
  eq("an emoji encodes through UTF-8", out(), "8J+YgA==");
  eq("which is four bytes", txt("cBytes"), "4");

  set("input", String.fromCharCode(0xFF, 0xFE));
  eq("a value whose Base64 contains a slash", out(), "w7/Dvg==");
  tick("optUrlSafe", true);
  eq("URL-safe swaps the slash and drops the padding", out(), "w7_Dvg");
  tick("optUrlSafe", false);

  set("input", String.fromCodePoint(0x1F600));
  tick("optUrlSafe", true);
  eq("and it swaps the plus as well", out(), "8J-YgA");
  tick("optUrlSafe", false);

  /* Wrapping, the way an email header does it. */
  set("input", new Array(200).join("x"));
  eq("199 bytes make 268 Base64 characters", out().length, 268);
  tick("optWrap", true);
  eq("wrapped into four lines", out().split("\n").length, 4);
  eq("the first exactly 76 characters", out().split("\n")[0].length, 76);
  eq("and the last one holding the remainder", out().split("\n")[3].length, 40);
  tick("optWrap", false);

  /* ---- decoding ---- */
  set("mode", "decode");
  eq("the alphabet option has nothing to do while decoding",
     document.getElementById("optUrlSafe").disabled, true);
  eq("nor does the wrapping option",
     document.getElementById("optWrap").disabled, true);

  set("input", "MTA4IFRvb2xCb3g=");
  eq("decodes back to the text", out(), "108 ToolBox");
  has("and says how much came out", txt("msg"), "11 bytes");

  set("input", "MTA4 IFRv\nb2xC b3g=");
  eq("whitespace in pasted Base64 is ignored", out(), "108 ToolBox");

  set("input", "w7_Dvg");
  eq("URL-safe with the padding stripped still decodes",
     out(), String.fromCharCode(0xFF, 0xFE));

  set("input", "@@@@");
  has("characters outside the alphabet are refused", txt("msg"), "does not have");
  eq("and nothing is printed", out(), "");

  set("input", "A");
  has("a length that cannot work is refused", txt("msg"), "length does not work out");

  set("input", "//4=");
  has("bytes that are not text are named as data", txt("msg"), "not text");
  eq("and the size is still reported", txt("cBytes"), "2");

  /* The fastest proof an encoder is right: watch it come back. */
  set("mode", "encode");
  set("input", "Hyderabad");
  var encoded = out();
  click("swapBtn");
  eq("the swap button flips the mode", val("mode"), "decode");
  eq("it carries the result across", val("input"), encoded);
  eq("and the round trip is unchanged", out(), "Hyderabad");

  set("input", "");
  eq("an empty box prints nothing", out(), "");
      /* A data: address used to be refused whole, as "not Base64". */
  set("mode", "decode");
  set("input", "data:text/plain;base64,SGVsbG8=");
  eq("a data: address decodes what is after its comma", out(), "Hello");
  has("and says the front was set aside", txt("msg"), "The data: address in front (text/plain) was set aside.");
  set("input", "data:image/png;base64,iVBORw0KGgo=");
  has("a picture is named as one", txt("msg"), "This is 8 bytes of image/png, not text.");
  set("input", "data:,hello");
  has("a data: address without base64 is explained", txt("msg"), "without ;base64");
  set("input", "SGVsbG8=");
  eq("plain Base64 still decodes", out(), "Hello");
  eq("with nothing about addresses", txt("msg").indexOf("data:"), -1);
  set("mode", "encode"); set("input", "");
  finish();
"""
# ---- END: the test for base64-encoder-decoder ----

# ---- START: the test for url-encoder-decoder ----
T["url-encoder-decoder"] = r"""
  eq("the default value is encoded for a query string", out(),
     "lunch%20box%20%26%20drinks%20%E2%80%94%2050%25%20off");
  eq("escapes counted", txt("cEsc"), "11");
  has("and it says where this is safe to go", txt("msg"), "query string");

  tick("optPlus", true);
  has("form style writes spaces as plus signs", out(), "lunch+box");
  eq("and leaves no %20 behind", (out().match(/%20/g) || []).length, 0);
  tick("optPlus", false);

  /* The one decision this page exists for. */
  set("input", "https://108toolbox.in/search?q=lunch box&page=2");
  eq("as a value, even the :// is escaped", out(),
     "https%3A%2F%2F108toolbox.in%2Fsearch%3Fq%3Dlunch%20box%26page%3D2");
  has("and the page warns what that is for",
      txt("msg"), "looks like a whole URL");

  set("scope", "whole");
  eq("as a whole URL only the space is escaped", out(),
     "https://108toolbox.in/search?q=lunch%20box&page=2");
  has("because the structure is left alone", txt("msg"), "left alone on purpose");
  eq("plus-for-spaces has no meaning in a path",
     document.getElementById("optPlus").disabled, true);
  set("scope", "part");

  /* Percent-encoding escapes UTF-8 bytes, not characters. */
  set("input", String.fromCodePoint(0x20B9) + "500");
  eq("a rupee sign is three escaped bytes", out(), "%E2%82%B9500");
  eq("three escapes", txt("cEsc"), "3");

  /* ---- decoding ---- */
  set("mode", "decode");
  set("input", "a+b%20c");
  eq("a plus stays a plus unless you say otherwise", out(), "a+b c");
  tick("optPlus", true);
  eq("with form style on it is a space", out(), "a b c");
  tick("optPlus", false);

  set("input", "%E2%82%B9500");
  eq("the rupee sign comes back whole", out(), String.fromCodePoint(0x20B9) + "500");

  set("input", "100%");
  has("a lone percent is found and located", txt("msg"), "position 4");
  eq("and nothing is printed", out(), "");

  set("input", "%FF");
  has("escaped bytes that are not text are refused", txt("msg"), "not text");

  set("input", "");
  eq("an empty box prints nothing", out(), "");
    finish();
"""
# ---- END: the test for url-encoder-decoder ----

# ---- START: the test for uuid-generator ----
T["uuid-generator"] = r"""
  var RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  var list = out().split("\n");

  eq("five UUIDs on load", list.length, 5);
  eq("and the tile agrees", txt("cCount"), "5");
  eq("36 characters each", txt("cLen"), "36");
  eq("122 random bits", txt("cBits"), "122");
  ok("every one is a valid version 4 UUID",
     list.every(function (u) { return RE.test(u); }), list.join(" | "));
  has("and it says where they were made", txt("msg"), "in this browser");

  /* Six of the 128 bits are not random, and that is the point. */
  eq("the version digit is always 4", list[0].charAt(14), "4");
  ok("the variant digit is 8, 9, a or b",
     "89ab".indexOf(list[0].charAt(19)) > -1, list[0]);

  set("count", 100);
  var many = out().split("\n");
  eq("a hundred at once", many.length, 100);
  var seen = {};
  many.forEach(function (u) { seen[u] = 1; });
  eq("and all hundred are different", Object.keys(seen).length, 100);
  ok("all hundred are well formed",
     many.every(function (u) { return RE.test(u); }), "one of them is not");

  set("count", 500);
  eq("too many is clamped", val("count"), "100");
  has("and the page says so", txt("msg"), "100 is the most this page makes at once");
  set("count", 0);
  eq("zero is refused, not quietly made into one", out(), "");
  set("count", 1);
  eq("one makes one line", out().split("\n").length, 1);
  has("the message reads for one", txt("msg"), "One fresh UUID");

  tick("optUpper", true);
  ok("uppercase leaves no small letters", !/[a-f]/.test(out()), out());
  tick("optUpper", false);

  tick("optNoDash", true);
  eq("without hyphens it is 32 characters", out().length, 32);
  eq("and the tile follows", txt("cLen"), "32");
  tick("optNoDash", false);

  tick("optBraces", true);
  eq("braces on both ends",
     out().charAt(0) + out().charAt(out().length - 1), "{}");
  eq("which makes 38 characters", txt("cLen"), "38");
  tick("optBraces", false);

  /* Generate has to actually generate. */
  var before = out();
  click("genBtn");
  ok("a second press gives a different UUID", out() !== before, "identical output");
  ok("and it is still well formed", RE.test(out()), out());

  click("clearBtn");
  eq("clear empties the list", out(), "");
  eq("and zeroes the count", txt("cCount"), "0");
      /* A count it cannot make used to be swapped without a word: 0 made one,
     2.5 made two and a million made 100. */
  set("count", "0"); click("genBtn");
  eq("nought is refused, not made into one", out(), "");
  has("and the page says what it takes", txt("msg"), "a whole number from 1 to 100");
  set("count", "2.5"); click("genBtn");
  eq("half a UUID is refused too", out(), "");
  set("count", "1000000");
  eq("a million makes the most it can", out().split("\n").length, 100);
  has("and says that is the most", txt("msg"), "100 is the most this page makes at once");
  eq("the box shows what was made", val("count"), "100");
  set("count", "3"); click("genBtn");
  eq("an ordinary count is just made", out().split("\n").length, 3);
  eq("with nothing about a limit", txt("msg").indexOf("most"), -1);
  finish();
"""
# ---- END: the test for uuid-generator ----

# ---- START: the test for color-code-converter ----
T["color-code-converter"] = r"""
  eq("three formats listed", rows().length, 3);
  has("the hex row", cell(0, 1), "#3b82f6");
  has("the rgb row", cell(1, 1), "rgb(59, 130, 246)");
  has("the hsl row", cell(2, 1), "hsl(217, 91%, 60%)");

  near("contrast against white", txt("cWhite"), 3.68, 0.02);
  near("contrast against black", txt("cBlack"), 5.71, 0.02);
  near("relative luminance as a percentage", txt("cLum"), 23.6, 0.2);
  has("black is the readable choice here", txt("msg"), "Black text is the readable choice");
  has("and white is named as failing", txt("msg"), "White text fails AA");

  /* Every syntax the browser knows has to land on the same colour. */
  set("input", "rgb(59 130 246)");
  has("the space-separated form is understood", cell(0, 1), "#3b82f6");
  set("input", "rgb(59, 130, 246)");
  has("and the comma form", cell(0, 1), "#3b82f6");
  set("input", "hsl(217 91% 60%)");
  has("hsl comes back as the same hsl", cell(2, 1), "hsl(217, 91%, 60%)");

  set("input", "rebeccapurple");
  has("a CSS colour name works", cell(0, 1), "#663399");
  has("and gives its rgb", cell(1, 1), "rgb(102, 51, 153)");
  set("input", "tomato");
  has("so does another", cell(1, 1), "rgb(255, 99, 71)");

  /* The two ends of the scale. */
  set("input", "#ffffff");
  near("white against black is the maximum 21", txt("cBlack"), 21, 0.01);
  near("and against white it is 1", txt("cWhite"), 1, 0.01);
  near("luminance is 100", txt("cLum"), 100, 0.1);
  has("so black text is the choice", txt("msg"), "Black text is the readable choice");

  set("input", "#000000");
  near("black against white is 21", txt("cWhite"), 21, 0.01);
  near("luminance is 0", txt("cLum"), 0, 0.01);
  has("so white text is the choice", txt("msg"), "White text is the readable choice");

  /* A three-digit hex is the six-digit one doubled. */
  set("input", "#f00");
  has("short hex expands", cell(0, 1), "#ff0000");
  has("red is hue 0", cell(2, 1), "hsl(0, 100%, 50%)");
  has("and the swatch is actually painted",
      document.getElementById("swatch").style.background, "rgb(255, 0, 0)");

  /* Alpha only shows up when there is alpha. */
  set("input", "#3b82f680");
  has("eight-digit hex keeps its alpha", cell(0, 1), "#3b82f680");
  has("and rgb becomes rgba", cell(1, 1), "rgba(59, 130, 246, 0.5)");
  has("and hsl becomes hsla", cell(2, 1), "hsla(217, 91%, 60%, 0.5)");

  set("input", "not a colour");
  has("nonsense is refused", txt("msg"), "not a colour this browser recognises");
  has("and the table shows a dash", cell(0, 1), String.fromCharCode(0x2014));
  eq("with the contrast zeroed", txt("cWhite"), "0");

  set("input", "");
  has("an empty box asks for a colour", txt("msg"), "Type a colour above");
        set("input", "ff0000");
    has("a hex colour without its # is read", txt("rows"), "#ff0000");
    set("input", "0f0");
    has("so is a short one", txt("rows"), "#00ff00");
    set("input", "#3b82f6");
    finish();
"""
# ---- END: the test for color-code-converter ----

# ---- START: the test for html-encoder-decoder ----
T["html-encoder-decoder"] = r"""
  eq("the default markup is escaped", out(),
     "&lt;a href=&quot;?a=1&amp;b=2&quot;&gt;Tom &amp; Jerry&#39;s &quot;big&quot; day&lt;/a&gt;");
  eq("eleven entities written", txt("cEsc"), "11");
  has("and it says what the result is for", txt("msg"), "show as text");

  /* The ampersand must be escaped first, or you escape your own output. */
  set("input", "&lt;");
  eq("an entity in the input is escaped once, not twice", out(), "&amp;lt;");

  tick("optQuotes", false);
  set("input", "a \"b\" 'c' <d>");
  eq("with quotes off only the markup characters go",
     out(), "a \"b\" 'c' &lt;d&gt;");
  tick("optQuotes", true);
  eq("with quotes on the quotes go too",
     out(), "a &quot;b&quot; &#39;c&#39; &lt;d&gt;");
  has("and an apostrophe is numeric, never &apos;", out(), "&#39;");

  /* A UTF-8 page does not need these escaped, so it does not do it. */
  set("input", String.fromCodePoint(0x20B9) + "500");
  eq("a rupee sign is left as itself by default",
     out(), String.fromCodePoint(0x20B9) + "500");
  eq("and nothing counts as escaped", txt("cEsc"), "0");
  tick("optAll", true);
  eq("and becomes a number only when asked", out(), "&#8377;500");

  set("input", String.fromCodePoint(0x1F600));
  eq("an emoji becomes one entity, not two broken halves",
     out(), "&#128512;");
  tick("optAll", false);

  /* ---- decoding ---- */
  set("mode", "decode");
  eq("the encode options do nothing here",
     document.getElementById("optQuotes").disabled, true);

  set("input", "&lt;b&gt; &amp; &#39;x&#39; &hellip; &#x1F600;");
  has("named entities decode", out(), "<b>");
  has("decimal ones decode", out(), "'x'");
  has("the ellipsis decodes", out(), String.fromCharCode(0x2026));
  has("and hexadecimal ones too", out(), String.fromCodePoint(0x1F600));
  has("seven of them", txt("msg"), "Decoded 7");

  set("input", "&foo; &amp;");
  eq("an unknown entity is left exactly as it was found", out(), "&foo; &");
  has("and the page admits it", txt("msg"), "left 1");

  /* A plain lookup would find these on the prototype and return a function. */
  set("input", "&constructor; &toString;");
  eq("prototype names are not entities", out(), "&constructor; &toString;");
  has("both left alone", txt("msg"), "left 2");

  set("input", "&#xD800;");
  eq("a lone surrogate is not a character, so it is left alone",
     out(), "&#xD800;");

  set("mode", "encode");
  set("input", "<p>Tom & Jerry</p>");
  click("swapBtn");
  eq("the swap flips to decode", val("mode"), "decode");
  eq("and the round trip comes back unchanged", out(), "<p>Tom & Jerry</p>");

  set("input", "");
  eq("an empty box prints nothing", out(), "");
      /* &#0; used to come out as an invisible NUL character. */
  set("mode", "decode");
  set("input", "a&#0;b");
  eq("&#0; is left alone like any number that names no character", out(), "a&#0;b");
  has("and said", txt("msg"), "left 1 entity alone");
  /* &#150; was an invisible control character; a browser shows a dash. */
  set("input", "&#150; &#128; &#x96; &#147;x&#148;");
  eq("Windows numbers read as a browser reads them", out(),
     String.fromCharCode(0x2013) + " " + String.fromCharCode(0x20AC) + " " +
     String.fromCharCode(0x2013) + " " + String.fromCharCode(0x201C) + "x" + String.fromCharCode(0x201D));
  set("input", "&#129;");
  eq("a number Windows never used stays as it is", out(), String.fromCharCode(0x81));
  /* &AMP; and friends are in HTML's own list, and were left alone. */
  set("input", "&AMP; &LT;b&GT; &COPY; &QUOT; &REG; &TRADE;");
  eq("capital names HTML accepts are decoded", out(),
     "& <b> " + String.fromCharCode(0xA9) + ' " ' + String.fromCharCode(0xAE) + " " + String.fromCharCode(0x2122));
  set("input", "&Amp;");
  eq("a mixed-case name is not one, and stays", out(), "&Amp;");
  set("mode", "encode"); set("input", "");
  finish();
"""
# ---- END: the test for html-encoder-decoder ----

# ---- START: the test for image-resizer ----
T["image-resizer"] = r"""
  var X = " " + String.fromCharCode(0x00D7) + " ";
  var DASH = String.fromCharCode(0x2014);

  makeImage("file", 200, 200, function () {
    waitFor("a 200x200 image is read and redrawn",
      function () { return txt("sBytes") !== DASH && txt("sBytes").length > 1; },
      function () {
        eq("the original size is reported", txt("sOriginal"), "200" + X + "200");
        eq("the width box is filled in", val("width"), "200");
        eq("the height box too", val("height"), "200");

        /* The locked shape is the whole point: one box drives the other. */
        set("width", 100);
        eq("height follows the width", val("height"), "100");
        eq("and the new size says so", txt("sNew"), "100" + X + "100");

        set("height", 40);
        eq("it works the other way too", val("width"), "40");

        /* Unlocked, the numbers are taken exactly as typed. */
        tick("lockRatio", false);
        set("width", 100);
        eq("the height is left alone now", val("height"), "40");
        eq("so the output is stretched", txt("sNew"), "100" + X + "40");
        tick("lockRatio", true);

        /* By percentage, half of 200 is 100. */
        set("mode", "percent");
        set("percent", 50);
        eq("half size", txt("sNew"), "100" + X + "100");
        set("percent", 25);
        eq("a quarter", txt("sNew"), "50" + X + "50");

        set("percent", 0);
        has("zero is refused with a reason", txt("msg"), "above zero");

        set("percent", 200);
        eq("double is allowed", txt("sNew"), "400" + X + "400");
        waitFor("but it says enlarging cannot add detail",
          function () { return txt("msg").indexOf("cannot add detail") > -1; },
          function () {
            click("resetBtn");
            eq("reset clears the original size", txt("sOriginal"), DASH);
            eq("and disables the download",
               document.getElementById("dlBtn").disabled, true);
                        /* A size no browser can draw used to fail quietly: the run
               before it finished later, the page said "Ready" beside the
               huge size, and Download gave the old, small picture. */
            makeImage("file", 200, 200, function () {
              waitFor("a fresh picture is ready",
                function () { return !document.getElementById("dlBtn").disabled; },
                function () {
                  set("mode", "percent");
                  set("percent", 50);
                  set("percent", 20000);
                  eq("40000 pixels a side is refused at once", txt("sNew"), "40000" + X + "40000");
                  has("with the limits named", txt("msg"), "up to 16,384 pixels a side");
                  eq("and nothing to download", document.getElementById("dlBtn").disabled, true);
                  eq("nor a size for a file that was not made", txt("sBytes"), DASH);
                  setTimeout(function () {
                    eq("the 50% run that finished after it is thrown away",
                       document.getElementById("dlBtn").disabled, true);
                    has("and the refusal is still what the page says", txt("msg"), "16,384");
                    set("mode", "px");
                    set("width", "20000");
                    has("a single side past the limit is refused too", txt("msg"), "16,384");
                    set("percent", 50); set("mode", "percent");
                    waitFor("and an ordinary size works again",
                      function () { return !document.getElementById("dlBtn").disabled; },
                      function () {
                        eq("at the size asked for", txt("sNew"), "100" + X + "100");
                        finish();
                      });
                  }, 1500);
                });
            });
          });
      });
  });
"""
# ---- END: the test for image-resizer ----

# ---- START: the test for image-rotator ----
T["image-rotator"] = r"""
  var X = " " + String.fromCharCode(0x00D7) + " ";

  /* A rectangle, not a square: a quarter turn has to swap the sides, and a
     square would hide it if it did not. */
  makeImage("file", 200, 100, function () {
    waitFor("a 200x100 image is read",
      function () { return txt("sDims").indexOf("200") > -1; },
      function () {
        eq("it starts unturned", txt("sAngle"), "0" + String.fromCharCode(0x00B0));
        eq("with no mirror", txt("sFlip"), "None");
        eq("at its own size", txt("sDims"), "200" + X + "100");

        click("rotRight");
        has("a quarter turn right", txt("sAngle"), "90");
        eq("swaps the sides", txt("sDims"), "100" + X + "200");

        click("rotRight");
        has("twice is upside down", txt("sAngle"), "180");
        eq("and the sides are back", txt("sDims"), "200" + X + "100");

        click("rotLeft");
        has("then left once is 90 again", txt("sAngle"), "90");

        /* 0 minus 90 must be 270, not -90. */
        click("resetBtn");
        click("rotLeft");
        has("turning left from zero reads 270", txt("sAngle"), "270");

        click("resetBtn");
        click("flipH");
        eq("a left-right mirror", txt("sFlip"), "Left-right");
        click("flipV");
        eq("both mirrors", txt("sFlip"), "Both");
        click("flipH");
        eq("and back to one", txt("sFlip"), "Top-bottom");

        click("rot180");
        has("mirrors and turns stack", txt("sAngle"), "180");
        eq("the mirror survives the turn", txt("sFlip"), "Top-bottom");

        click("resetBtn");
        eq("reset clears the angle", txt("sAngle"), "0" + String.fromCharCode(0x00B0));
        eq("and the mirrors", txt("sFlip"), "None");
        finish();
      });
  });
"""
# ---- END: the test for image-rotator ----

# ---- START: the test for image-to-base64 ----
T["image-to-base64"] = r"""
  var DASH = String.fromCharCode(0x2014);

  makeImage("file", 60, 60, function () {
    waitFor("the image is encoded",
      function () { return txt("output").length > 50; },
      function () {
        has("it is a PNG data URI", txt("output"), "data:image/png;base64,");
        ok("the file size is reported", txt("sBytes") !== DASH, txt("sBytes"));
        ok("so is the text length", txt("sChars") !== DASH, txt("sChars"));
        has("and the growth as a percentage", txt("sGrowth"), "%");

        var raw = txt("output");

        set("snippet", "css");
        has("CSS wraps it in a background-image", txt("output"), "background-image: url(");
        has("and keeps the data URI", txt("output"), "data:image/png;base64,");

        set("snippet", "html");
        has("HTML wraps it in an img tag", txt("output"), "<img src=");

        set("snippet", "md");
        ok("Markdown starts with the image marker",
           txt("output").indexOf("![](") === 0, txt("output").slice(0, 12));

        set("snippet", "raw");
        eq("and the bare form comes back unchanged", txt("output"), raw);

        /* A 60x60 PNG is small, so the page should say inlining is fine. */
        has("it judges a small image worth inlining", txt("msg"), "good size to inline");

        click("resetBtn");
        eq("reset empties the output", txt("output"), "");
        eq("and the tiles", txt("sBytes"), DASH);
        finish();
      });
  });
"""
# ---- END: the test for image-to-base64 ----

# ---- START: the test for svg-to-png ----
T["svg-to-png"] = r"""
  var X = " " + String.fromCharCode(0x00D7) + " ";

  /* The worked example is 120x120 and the page opens at 2x. */
  waitFor("the example SVG is drawn at 2x",
    function () { return txt("sDims") === "240" + X + "240"; },
    function () {
      has("and it says it stayed local", txt("msg"), "nothing was uploaded");
      ok("the PNG has a weight", txt("sBytes").length > 1, txt("sBytes"));
      ok("a preview appeared",
         document.querySelectorAll("#preview img").length > 0);

      set("scale", "1");
      waitFor("1x is the size written in the SVG",
        function () { return txt("sDims") === "120" + X + "120"; },
        function () {
          set("scale", "4");
          waitFor("4x is four times that",
            function () { return txt("sDims") === "480" + X + "480"; },
            function () {

              /* No width or height, only a viewBox - still has a size. */
              set("input", '<svg xmlns="http://www.w3.org/2000/svg" ' +
                           'viewBox="0 0 50 20"><rect width="50" height="20" ' +
                           'fill="#000"/></svg>');
              waitFor("a viewBox alone gives the size",
                function () { return txt("sDims") === "200" + X + "80"; },
                function () {

                  set("input", "hello, not an svg");
                  has("plain text is refused", txt("msg"), "does not contain an <svg> tag");
                  eq("and the download is disabled",
                     document.getElementById("dlBtn").disabled, true);

                  set("input", '<svg xmlns="http://www.w3.org/2000/svg">' +
                               '<rect width="10" height="10"/></svg>');
                  has("an SVG with no size at all says so", txt("msg"), "no size to work from");

                  click("clearBtn");
                  has("clearing asks for input again", txt("msg"), "Paste some SVG");
                                    /* Sizes in other units, a % size and a missing xmlns. */
                  window.__saved = [];
                  window.downloadBlob = function (blob, name) { window.__saved.push(blob); };
                  set("scale", "1");
                  set("input", '<svg xmlns="http://www.w3.org/2000/svg" width="2cm" height="1in"><rect width="10" height="10"/></svg>');
                  waitFor("2cm by 1in is 76 by 96 pixels, not 2 by 1",
                    function () { return txt("sDims") === "76" + X + "96"; },
                    function () {
                      set("input", '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 4 3"><rect width="4" height="3"/></svg>');
                      waitFor("a % size takes the viewBox's shape, not a square",
                        function () { return txt("sDims") === "4" + X + "3"; },
                        function () {
                          set("input", '<svg xmlns="http://www.w3.org/2000/svg" width="300" viewBox="0 0 4 3"><rect width="4" height="3"/></svg>');
                          waitFor("a width alone takes its height from the viewBox",
                            function () { return txt("sDims") === "300" + X + "225"; },
                            function () {
                              set("scale", "4");
                              set("input", '<svg width="10" height="10"><rect x="5" y="5" width="5" height="5" fill="#ff0000"/></svg>');
                              waitFor("an SVG with no xmlns is drawn, at four times its size",
                                function () { return txt("sDims") === "40" + X + "40"; },
                                function () {
                                  has("and the page says the namespace was added", txt("msg"), "had no xmlns");
                                  click("dlBtn");
                                  createImageBitmap(window.__saved[window.__saved.length - 1]).then(function (bmp) {
                                    var c = document.createElement("canvas"); c.width = bmp.width; c.height = bmp.height;
                                    var x = c.getContext("2d"); x.drawImage(bmp, 0, 0);
                                    var red = x.getImageData(35, 35, 1, 1).data, clear = x.getImageData(10, 10, 1, 1).data;
                                    ok("the drawing scaled with it: red at 35,35", red[0] > 240 && red[3] > 240, Array.prototype.join.call(red, ","));
                                    ok("and nothing at 10,10", clear[3] === 0, Array.prototype.join.call(clear, ","));

                                    set("input", '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>');
                                    eq("a refused SVG leaves no size behind", txt("sDims"), String.fromCharCode(0x2014));
                                    eq("no picture", document.querySelectorAll("#preview img").length, 0);
                                    eq("and nothing to download", document.getElementById("dlBtn").disabled, true);
                                    finish();
                                  });
                                });
                            });
                        });
                    });
                });
            });
        });
    });
"""
# ---- END: the test for svg-to-png ----

# ---- START: the test for image-placeholder-generator ----
T["image-placeholder-generator"] = r"""
  var X = " " + String.fromCharCode(0x00D7) + " ";

  waitFor("the default placeholder is drawn",
    function () { return txt("sBytes").length > 1 &&
                         txt("sBytes") !== String.fromCharCode(0x2014); },
    function () {
      eq("at the default size", txt("sDims"), "800" + X + "600");
      eq("which is four by three", txt("sRatio"), "4:3");
      has("and it says nothing was fetched", txt("msg"), "calls no one");

      /* The ratio is the interesting arithmetic: 1600x900 must read 16:9. */
      set("pw", 1600); set("ph", 900);
      eq("sixteen by nine", txt("sRatio"), "16:9");
      set("pw", 100); set("ph", 100);
      eq("a square is one to one", txt("sRatio"), "1:1");
      set("pw", 1000); set("ph", 300);
      eq("ten by three", txt("sRatio"), "10:3");

      /* An awkward pair has no readable whole-number form, so it gives up
         honestly and shows the decimal instead of 617:438. */
      set("pw", 617); set("ph", 438);
      has("an awkward ratio falls back to a decimal", txt("sRatio"), ":1");

      set("pw", 0);
      has("zero is refused with a reason", txt("msg"), "above zero");
      set("pw", 800); set("ph", 600);

      /* Over the cap it clamps rather than failing in the canvas. */
      set("pw", 9000);
      eq("too large is clamped to 4000", txt("sDims"), "4000" + X + "600");

      click("resetBtn");
      waitFor("reset returns to the default",
        function () { return txt("sDims") === "800" + X + "600"; },
        function () {
          eq("with the default colour back", val("bg"), "#e2e8f0");
          var DASH = String.fromCharCode(0x2014);

    /* A zero size left the last picture; 5000 was made at 4000 without a word. */
    set("pw", "0");
    eq("a zero width leaves no size", txt("sDims"), DASH);
    eq("and no picture", document.querySelectorAll("#preview img").length, 0);
    set("pw", "5000"); set("ph", "600");
    waitFor("5000 wide is made at the limit",
      function () { return txt("msg").indexOf("Made at") > -1; },
      function () {
        has("and the page says so", txt("msg"), "4000 pixels a side is the most");
        eq("at 4000", txt("sDims"), "4000 " + String.fromCharCode(0x00D7) + " 600");
        set("pw", "800");
        waitFor("an ordinary size is just ready",
          function () { return txt("msg").indexOf("Ready") === 0; },
          function () {     /* canvas.toBlob answers later, so a draw overtaken by a newer one used to
       land anyway - a picture the boxes no longer describe, and the one
       Download would save. The test holds each encode and releases them in
       the order that used to go wrong. */
    var realToBlob = HTMLCanvasElement.prototype.toBlob;
    var held = [];
    HTMLCanvasElement.prototype.toBlob = function (cb, type, q) { held.push([this, cb, type, q]); };
    function release(i, then) {
      var h = held[i], done = false;
      realToBlob.call(h[0], function (blob) { h[1](blob); done = true; }, h[2], h[3]);
      waitFor("encode " + i + " answered", function () { return done; }, then, 5000);
    }
    function shown() {
      var im = document.querySelector("#preview img");
      return im && im.complete && im.naturalWidth ? im.naturalWidth + "x" + im.naturalHeight : "";
    }
    var NONE = String.fromCharCode(0x2014);
    set("pw", "300");
    set("pw", "0");
    eq("one encode is held", held.length, 1);
    release(0, function () {
      eq("an overtaken draw puts no picture back", document.querySelectorAll("#preview img").length, 0);
      eq("nor a size in bytes", txt("sBytes"), NONE);
      has("and the zero-size message stays", txt("msg"), "above zero");
      set("pw", "400");
      set("pw", "200");
      eq("two more encodes are held", held.length, 3);
      release(2, function () {
        release(1, function () {
          HTMLCanvasElement.prototype.toBlob = realToBlob;
          waitFor("the picture shown has loaded", function () { return shown() !== ""; }, function () {
            eq("the newest draw is the one shown, not an older one answering late", shown(), "200x600");
            finish();
          });
        });
      });
    }); });
      });
        });
    });
"""
# ---- END: the test for image-placeholder-generator ----

# ---- START: the test for random-number-generator ----
T["random-number-generator"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function lines() {
    return txt("output").split("\n").filter(function (s) { return s !== ""; });
  }

  /* The page draws once on load. */
  eq("one number on load", lines().length, 1);
  ok("and it sits inside the default range",
     Number(lines()[0]) >= 1 && Number(lines()[0]) <= 100, lines()[0]);

  set("lo", 1); set("hi", 6); set("howMany", 200);
  click("drawBtn");
  eq("two hundred draws give two hundred numbers", lines().length, 200);
  var all = lines().map(Number);
  var outside = all.filter(function (v) {
    return v < 1 || v > 6 || v !== Math.round(v);
  });
  eq("every one is a whole number from 1 to 6", outside.length, 0);
  ok("repeats happen when they are allowed", new Set(all).size < all.length);
  eq("and all six faces turned up over 200 draws", new Set(all).size, 6);

  /* No repeats, taking the WHOLE range - the shuffle path. */
  set("lo", 1); set("hi", 5); set("howMany", 5);
  tick("unique", true);
  click("drawBtn");
  eq("five different numbers from a range of five",
     lines().map(Number).sort(function (a, b) { return a - b; }).join(","),
     "1,2,3,4,5");

  /* No repeats, taking a few from a wide range - the rejection path. */
  set("lo", 1); set("hi", 1000000); set("howMany", 50);
  click("drawBtn");
  eq("fifty drawn from a million", lines().length, 50);
  eq("and all fifty are different", new Set(lines()).size, 50);

  set("lo", 1); set("hi", 4); set("howMany", 6);
  click("drawBtn");
  has("six different numbers out of four is refused", txt("msg"), "cannot all");
  /* The fifty from the draw before used to stay on screen under the error,
     looking like the answer, and Copy handed them over. */
  eq("and the last draw is not left on screen", txt("output"), "");
  eq("nor in the tiles", txt("sCount"), String.fromCharCode(0x2014));
  var copied = null, realCopy = window.copyText;
  window.copyText = function (t) { copied = t; };
  click("copyBtn");
  window.copyText = realCopy;
  eq("and Copy has nothing old to hand over", copied, null);

  tick("unique", false);
  set("lo", 100); set("hi", 1); set("howMany", 3);
  click("drawBtn");
  has("a backwards range is swapped, not refused", txt("msg"), "from 1 to 100");

  set("lo", 7); set("hi", 7); set("howMany", 3);
  click("drawBtn");
  eq("a range of one gives that number every time", lines().join(","), "7,7,7");
  eq("the lowest tile agrees", num("sLow"), 7);
  eq("and so does the highest", num("sHigh"), 7);

  set("lo", 1); set("hi", 1000); set("howMany", 40);
  tick("sorted", true);
  click("drawBtn");
  var sorted = lines().map(Number);
  var descents = 0;
  for (var i = 1; i < sorted.length; i++) {
    if (sorted[i] < sorted[i - 1]) { descents += 1; }
  }
  eq("sorted means sorted", descents, 0);
  eq("the count tile matches", num("sCount"), 40);

  set("howMany", 0);
  click("drawBtn");
  has("zero numbers is refused", txt("msg"), "at least one");

  click("resetBtn");
  eq("reset empties the list", txt("output"), "");
  eq("and clears the tiles", txt("sCount"), String.fromCharCode(0x2014));
  finish();
"""
# ---- END: the test for random-number-generator ----

# ---- START: the test for coin-flip ----
T["coin-flip"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  click("flipBtn");
  ok("one flip lands on one side or the other",
     txt("bigResult") === "Heads" || txt("bigResult") === "Tails",
     txt("bigResult"));
  eq("the two counts add up to the one flip",
     num("sHeads") + num("sTails"), 1);
  eq("one flip is a streak of one", num("sRun"), 1);
  ok("the sequence stays hidden for a single flip",
     document.getElementById("output").hidden === true);

  set("howMany", 500);
  click("flipBtn");
  eq("five hundred flips are all accounted for",
     num("sHeads") + num("sTails"), 500);
  ok("neither side ran away with it - 500 flips land near half",
     num("sHeads") > 175 && num("sHeads") < 325, txt("sHeads"));
  ok("the sequence appears once there is more than one flip",
     document.getElementById("output").hidden === false);
  eq("the big result is the heads-to-tails split",
     txt("bigResult"), num("sHeads") + " : " + num("sTails"));

  var seq = txt("output").replace(/[^HT]/g, "");
  eq("the printed sequence holds every flip", seq.length, 500);
  eq("and counts the same heads as the tile",
     seq.split("H").length - 1, num("sHeads"));

  ok("a streak of at least two appears in 500 flips",
     num("sRun") >= 2, txt("sRun"));
  ok("and nowhere near the whole run", num("sRun") < 40, txt("sRun"));
  has("the verdict says what is typical", txt("msg"), "typical");

  set("howMany", 0);
  click("flipBtn");
  has("zero flips is refused", txt("msg"), "at least one");

  set("howMany", 10001);
  click("flipBtn");
  has("past the limit is refused", txt("msg"), "limit");

  click("resetBtn");
  eq("reset clears the big result",
     txt("bigResult"), String.fromCharCode(0x2014));
  eq("and the box is put away", document.getElementById("output").hidden, true);
  finish();
"""
# ---- END: the test for coin-flip ----

# ---- START: the test for dice-roller ----
T["dice-roller"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function dice() {
    return txt("output").split(/\s+/).filter(function (s) { return s !== ""; });
  }
  function sum(list) {
    return list.reduce(function (a, b) { return a + b; }, 0);
  }

  /* The page opens on 2d6 and rolls once. */
  eq("two dice are printed", dice().length, 2);
  eq("the possible range of 2d6", txt("sRange"), "2 to 12");
  eq("seven is the likeliest total", txt("sLikely"), "7");
  ok("the total sits inside the range",
     num("bigTotal") >= 2 && num("bigTotal") <= 12, txt("bigTotal"));
  eq("and the total is the sum of the dice shown",
     sum(dice().map(Number)), num("bigTotal"));

  /* One die is flat - there is no peak, and the page says so. */
  set("count", 1);
  click("rollBtn");
  eq("one die has no likeliest face", txt("sLikely"), "all equal");
  eq("its range is just the faces", txt("sRange"), "1 to 6");
  has("and the message says every number is equally likely",
      txt("msg"), "equally likely");

  /* Three dice bunch harder, and the peak falls between two whole numbers. */
  set("count", 3);
  click("rollBtn");
  eq("3d6 can be 3 to 18", txt("sRange"), "3 to 18");
  eq("with two equally likeliest totals", txt("sLikely"), "10 or 11");

  /* A modifier moves the range and the peak together. */
  set("count", 2); set("modifier", 3);
  click("rollBtn");
  eq("2d6+3 ranges 5 to 15", txt("sRange"), "5 to 15");
  eq("and peaks at ten", txt("sLikely"), "10");
  has("the label reads as an addition", txt("bigLabel"), "2d6 + 3");

  set("modifier", -2);
  click("rollBtn");
  eq("a negative modifier works too", txt("sRange"), "0 to 10");
  has("and reads as a subtraction", txt("bigLabel"), "2d6 - 2");

  /* The quick-pick buttons set the sides and reroll. */
  set("modifier", 0);
  document.querySelector('[data-die="20"]').click();
  eq("the d20 button sets twenty sides", val("sides"), "20");
  eq("so two of them can reach forty", txt("sRange"), "2 to 40");

  /* Drop the lowest: four rolled, three counted, the dropped one still shown. */
  set("count", 4); set("sides", 6);
  click("rollBtn");
  eq("nothing is dropped yet", txt("sKept"), "4");
  tick("dropLowest", true);
  eq("three of the four are counted", txt("sKept"), "3 of 4");
  eq("but four dice are still printed", dice().length, 4);
  var bracketed = dice().filter(function (d) { return d.charAt(0) === "("; });
  var kept = dice().filter(function (d) { return d.charAt(0) !== "("; }).map(Number);
  eq("exactly one die is in brackets", bracketed.length, 1);
  eq("the total counts only the three kept", sum(kept), num("bigTotal"));
  var dropped = Number(bracketed[0].replace(/[()]/g, ""));
  ok("and the dropped die is the lowest of the four",
     dropped <= Math.min.apply(null, kept),
     dropped + " dropped, kept " + kept.join(","));
  eq("the range now covers three dice, not four", txt("sRange"), "3 to 18");

  set("count", 1);
  click("rollBtn");
  has("dropping the lowest of one die is refused",
      txt("msg"), "nothing to count");

  tick("dropLowest", false);
  set("sides", 1);
  click("rollBtn");
  has("a one-sided die is refused", txt("msg"), "at least two sides");

  click("resetBtn");
  eq("reset returns to two dice", val("count"), "2");
  eq("six sides", val("sides"), "6");
  eq("and no modifier", val("modifier"), "0");
  var DASH = String.fromCharCode(0x2014);

    /* A refused roll used to leave the last total, and Copy handed it over. */
    set("count", "2"); set("sides", "6"); set("modifier", "0");
    click("rollBtn");
    ok("a roll has a total", txt("bigTotal") !== DASH, txt("bigTotal"));
    set("count", "0");
    click("rollBtn");
    eq("a refused roll leaves no total", txt("bigTotal"), DASH);
    eq("and no dice", txt("output"), "");
    var copied = null, realCopy = window.copyText;
    window.copyText = function (t) { copied = t; };
    click("copyBtn");
    window.copyText = realCopy;
    eq("and Copy has nothing old to hand over", copied, null);
    finish();
"""
# ---- END: the test for dice-roller ----

# ---- START: the test for random-list-shuffler ----
T["random-list-shuffler"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function lines() {
    return txt("output").split("\n").filter(function (s) { return s !== ""; });
  }

  /* Eight names are in the box when the page opens, shuffled once already. */
  eq("all eight come out", lines().length, 8);
  eq("the lines-in tile agrees", num("sIn"), 8);
  eq("nothing was skipped", num("sSkipped"), 0);
  eq("the same eight names, no more and no fewer",
     lines().slice().sort().join("|"),
     val("input").split("\n").slice().sort().join("|"));

  /* Five shuffles of eight items all landing on one order would be a
     one-in-forty-thousand coincidence, four times over. */
  var orders = {};
  for (var i = 0; i < 5; i++) {
    click("shuffleBtn");
    orders[lines().join("|")] = true;
  }
  ok("shuffling again gives a different order",
     Object.keys(orders).length > 1,
     Object.keys(orders).length + " distinct orders in 5 shuffles");

  /* Keeping the first few turns a shuffle into a fair draw. */
  set("takeFirst", 3);
  click("shuffleBtn");
  eq("three kept", lines().length, 3);
  eq("the items-out tile says three", num("sOut"), 3);
  has("which the page calls a fair draw", txt("msg"), "fair draw");

  set("takeFirst", 0);
  set("input", "a\n\nb\n\nc");
  click("shuffleBtn");
  eq("blank lines are dropped by default", lines().length, 3);
  eq("and counted as skipped", num("sSkipped"), 2);

  tick("dropBlank", false);
  click("shuffleBtn");
  eq("kept when asked for", num("sSkipped"), 0);

  tick("dropBlank", true);
  set("input", "x\ny\nx\nz\ny");
  click("shuffleBtn");
  eq("duplicates stay unless asked about", lines().length, 5);
  tick("dropDupes", true);
  click("shuffleBtn");
  eq("and go when they are", lines().length, 3);
  eq("two of the five were repeats", num("sSkipped"), 2);

  set("input", "only one");
  click("shuffleBtn");
  has("one item has one possible order",
      txt("msg"), "only one possible order");

  set("input", "");
  click("shuffleBtn");
  has("an empty box asks for a list", txt("msg"), "Paste a list");
  eq("and the output is cleared", txt("output"), "");
  finish();
"""
# ---- END: the test for random-list-shuffler ----

# ---- START: the test for random-picker ----
T["random-picker"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function names() {
    return txt("output").split("\n")
      .filter(function (s) { return s !== ""; })
      .map(function (s) { return s.replace(/^[0-9]+\.\s*/, ""); });
  }

  click("pickBtn");
  eq("one winner", names().length, 1);
  eq("the winners tile agrees", num("sWinners"), 1);
  eq("eight entries were counted", num("sEntries"), 8);
  has("each had a one-in-eight chance", txt("sChance"), "12.5");
  ok("the winner is one of the entries",
     val("input").split("\n").indexOf(txt("bigWinner")) > -1, txt("bigWinner"));
  has("an unseeded draw says nobody can repeat it", txt("msg"), "Nobody");

  /* Nobody wins twice: ask for all of them and you get a permutation. */
  set("winners", 8);
  click("pickBtn");
  eq("eight winners from eight entries", names().length, 8);
  eq("every entry appears exactly once",
     names().slice().sort().join("|"),
     val("input").split("\n").slice().sort().join("|"));

  set("winners", 9);
  click("pickBtn");
  has("nine winners out of eight is refused", txt("msg"), "only 8 entries");

  /* The seed is the whole point, and it is deterministic. */
  set("winners", 8);
  set("seed", "diwali-2026");
  click("pickBtn");
  var firstDraw = txt("output");
  click("pickBtn");
  eq("the same list and seed draw the same winners again",
     txt("output"), firstDraw);
  has("and the seed is named so it can be published",
      txt("msg"), "diwali-2026");
  eq("a seeded draw is still a permutation",
     names().slice().sort().join("|"),
     val("input").split("\n").slice().sort().join("|"));

  set("seed", "diwali-2027");
  click("pickBtn");
  ok("one character of seed changes the whole draw",
     txt("output") !== firstDraw, "the two seeds gave identical orders");

  set("seed", "");
  set("winners", 1);
  set("input", "Solo");
  click("pickBtn");
  eq("a single entry always wins", txt("bigWinner"), "Solo");
  has("at a hundred per cent", txt("sChance"), "100");

  set("input", "Ravi\nravi\nRAVI");
  click("pickBtn");
  eq("one name in three capitalisations counts once", num("sEntries"), 1);
  tick("dropDupes", false);
  click("pickBtn");
  eq("unless repeats are allowed to stand", num("sEntries"), 3);

  set("input", "");
  click("pickBtn");
  has("an empty list asks for entries", txt("msg"), "Paste some entries");
  var DASH = String.fromCharCode(0x2014);

    /* A refused draw used to leave the last winner on screen. */
    set("input", "a\nb\nc"); set("winners", "1"); set("seed", "");
    click("pickBtn");
    ok("a draw has a winner", txt("bigWinner") !== DASH, txt("bigWinner"));
    set("winners", "0");
    click("pickBtn");
    eq("a refused draw leaves no winner", txt("bigWinner"), DASH);
    eq("and no list", txt("output"), "");
    set("winners", "1.5");
    click("pickBtn");
    has("half a winner is refused, not rounded", txt("msg"), "whole number of winners");
    finish();
"""
# ---- END: the test for random-picker ----

# ---- START: the test for username-generator ----
T["username-generator"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function ideas() {
    return txt("output").split("\n").filter(function (s) { return s !== ""; });
  }
  function all(re) {
    return ideas().every(function (n) { return re.test(n); });
  }

  /* Opens as twelve lowercase describing-word-plus-thing names. */
  eq("twelve ideas", ideas().length, 12);
  eq("the tile agrees", num("sMade"), 12);
  eq("sixty-four adjectives by sixty-four nouns", num("sPool"), 4096);
  eq("all twelve are different", new Set(ideas()).size, 12);
  ok("lowercase letters only", all(/^[a-z]+$/), txt("output"));
  ok("none is over the length limit", num("sLongest") <= 20, txt("sLongest"));
  has("it is honest about what it cannot check", txt("msg"), "cannot check");

  set("sep", "_");
  ok("an underscore joins the two words", all(/^[a-z]+_[a-z]+$/), txt("output"));

  set("letters", "caps");
  ok("capitalised means both words start upper",
     all(/^[A-Z][a-z]+_[A-Z][a-z]+$/), txt("output"));

  set("letters", "lower");
  set("shape", "nounnoun");
  ok("two nouns are never the same noun twice",
     ideas().every(function (n) {
       var half = n.split("_");
       return half.length === 2 && half[0] !== half[1];
     }), txt("output"));
  eq("sixty-four squared", num("sPool"), 4096);

  set("sep", "");
  set("shape", "adjnoun");
  set("digits", "2");
  ok("two digits on the end, never a leading zero",
     all(/^[a-z]+[1-9][0-9]$/), txt("output"));
  eq("ninety two-digit numbers multiply the pool", num("sPool"), 368640);

  set("digits", "1");
  ok("a single digit may be a zero", all(/^[a-z]+[0-9]$/), txt("output"));
  eq("ten of them, so ten times the pool", num("sPool"), 40960);

  set("digits", "0");
  set("shape", "noun");
  eq("one word only is a pool of sixty-four", num("sPool"), 64);
  ok("and every idea is one word", all(/^[a-z]+$/), txt("output"));

  /* Nothing can fit: the shortest noun is four letters and four digits
     make eight, against a limit of six. */
  set("digits", "4");
  set("maxLen", 6);
  click("genBtn");
  has("an impossible limit says so plainly", txt("msg"), "longer than 6");
  eq("and nothing is offered", txt("output"), "");

  set("maxLen", 20);
  set("howMany", 0);
  click("genBtn");
  has("asking for none is refused", txt("msg"), "at least one");

  click("resetBtn");
  eq("reset returns to the opening shape", val("shape"), "adjnoun");
  eq("with no digits", val("digits"), "0");
  eq("and twelve ideas again", ideas().length, 12);
  var DASH = String.fromCharCode(0x2014);

    /* A refused request used to leave the last list. */
    set("maxLen", "20"); set("howMany", "5");
    click("genBtn");
    ok("five ideas", txt("output").split("\n").length === 5, txt("output"));
    set("howMany", "0");
    click("genBtn");
    eq("a refused request leaves no list", txt("output"), "");
    eq("and no count", txt("sMade"), DASH);
    set("howMany", "5000");
    click("genBtn");
    has("too many is refused with the limit", txt("msg"), "Up to 1,000 ideas");
    set("howMany", "12");
    click("genBtn");
    eq("and twelve works again", txt("output").split("\n").length, 12);
    finish();
"""
# ---- END: the test for username-generator ----

# ---- START: the test for hash-generator ----
T["hash-generator"] = r"""
  /* Published test vectors, computed with Python's hashlib, not with this
     page - a hash tool agreeing with itself proves nothing. */
  var FOX256 = "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592";
  var FOX1   = "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12";
  var ABC256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
  var ABC384 = "cb00753f45a35e8bb5a03d699ac65007272c32ab0eded1631a8b605a43ff5bed8086072ba1e7cc2358baeca134c825a7";
  var ABC512 = "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f";
  var TELUGU = String.fromCharCode(0x0C39, 0x0C32, 0x0C4B);
  var TEL256 = "3a6430b444174ccd2a9818fa3026854a09d9cf23025e22e4efe7b1f8de4d9f5d";

  function once(label, want, then) {
    waitFor(label, function () { return txt("output") === want; }, then);
  }

  /* crypto.subtle is asynchronous, so every step has to wait for its answer. */
  once("SHA-256 of the fox sentence is the published vector", FOX256, function () {
    eq("the algorithm tile", txt("sAlgo"), "SHA-256");
    eq("the length tile", txt("sBits"), "256 bits");
    eq("hashed from text, not a file", txt("sSource"), "text");

    set("algo", "SHA-1");
    once("SHA-1 of the same sentence", FOX1, function () {
      eq("forty hex characters", txt("output").length, 40);
      has("and SHA-1 is called broken", txt("msg"), "broken");

      set("algo", "SHA-256");
      set("input", "abc");
      once("SHA-256 of abc", ABC256, function () {
        set("algo", "SHA-384");
        once("SHA-384 of abc", ABC384, function () {
          set("algo", "SHA-512");
          once("SHA-512 of abc", ABC512, function () {
            eq("128 hex characters", txt("output").length, 128);
            eq("and the tile says so", txt("sBits"), "512 bits");

            set("algo", "SHA-256");
            tick("upper", true);
            once("capitals when asked for", ABC256.toUpperCase(), function () {
              tick("upper", false);

              /* UTF-8 bytes, not character codes. Getting this wrong gives a
                 hash that disagrees with every other tool on earth, and only
                 for people whose language is not English. */
              set("input", TELUGU);
              once("Telugu text hashes as UTF-8 bytes", TEL256, function () {
                set("input", "");
                waitFor("an empty box asks for input",
                  function () { return txt("msg").indexOf("Type something") > -1; },
                  function () {
                    eq("and the output is cleared", txt("output"), "");
                    click("resetBtn");
                    eq("reset returns to SHA-256", val("algo"), "SHA-256");
                    finish();
                  });
              });
            });
          });
        });
      });
    });
  });
"""
# ---- END: the test for hash-generator ----

# ---- START: the test for jwt-decoder ----
T["jwt-decoder"] = r"""
  function b64(obj) {
    var bytes = new TextEncoder().encode(JSON.stringify(obj));
    var bin = "";
    for (var i = 0; i < bytes.length; i++) { bin += String.fromCharCode(bytes[i]); }
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  click("sampleBtn");
  has("the header carries the algorithm", txt("headerOut"), "HS256");
  has("the payload carries the name", txt("payloadOut"), "Priya Sharma");
  eq("three parts", txt("sParts"), "3");
  eq("the algorithm tile", txt("sAlg"), "HS256");
  has("issued-at is printed in words", txt("timesOut"), "Issued at");
  has("and so is the expiry", txt("timesOut"), "Expires");
  ok("the example is not already expired", txt("sState") !== "expired", txt("sState"));
  has("it says the signature was never checked",
      txt("msg"), "signature was not checked");

  set("input", "abc.def");
  has("two parts is not a JWT", txt("msg"), "three parts");
  eq("and the count is shown", txt("sParts"), "2");

  set("input", "aaa.bbb.ccc");
  has("three unreadable parts are reported", txt("msg"), "not readable");

  set("input", b64({ alg: "none", typ: "JWT" }) + "." + b64({ sub: "1" }) + ".");
  eq("alg none reaches the tile", txt("sAlg"), "none");
  has("and is called out as an attack shape", txt("msg"), "alg: none");

  /* The specification says seconds. A great deal of code writes milliseconds. */
  set("input", b64({ alg: "HS256" }) + "." + b64({ exp: 1700000000000 }) + ".sig");
  has("a year-56000 expiry is explained, not printed straight",
      txt("timesOut"), "milliseconds");

  /* atob alone returns one character per byte, which mangles every alphabet
     but English. This is the step most decoders skip. */
  var name = String.fromCharCode(0x0C30, 0x0C35, 0x0C3F);
  set("input", b64({ alg: "HS256" }) + "." + b64({ name: name }) + ".sig");
  has("a Telugu claim decodes correctly", txt("payloadOut"), name);

  set("input", "Bearer " + b64({ alg: "HS256" }) + "." + b64({ sub: "x" }) + ".sig");
  eq("a Bearer prefix is stripped", txt("sAlg"), "HS256");

  /* A token is somebody's key; it must never be treated as markup. */
  set("input", b64({ alg: "HS256" }) + "." + b64({ n: "<iframe onload=zq>" }) + ".sig");
  ok("a tag inside a claim stays text",
     document.querySelectorAll("#payloadOut iframe").length === 0);
  has("and is shown to the visitor as characters",
      txt("payloadOut"), "<iframe");

  click("clearBtn");
  has("clearing asks for a token", txt("msg"), "Paste a token");
  finish();
"""
# ---- END: the test for jwt-decoder ----

# ---- START: the test for regex-tester ----
T["regex-tester"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function marks(sel) { return document.querySelectorAll("#highlight " + sel).length; }

  /* Opens on an email pattern over two lines of sample text. */
  eq("two addresses match", num("sMatches"), 2);
  eq("the pattern has two groups", txt("sGroups"), "2");
  has("the first group caught the name", txt("matchList"), "priya");
  has("the second caught the domain", txt("matchList"), "108toolbox");
  eq("both are highlighted", marks("mark"), 2);

  /* The word boundary is the lesson: without it, .in matches inside
     .invalid and an address that should not match does. */
  set("pattern", "(\\w+)@(\\w+)\\.in");
  eq("without the boundary a third address matches", num("sMatches"), 3);
  set("pattern", "(\\w+)@(\\w+)\\.in\\b");
  eq("with it, back to two", num("sMatches"), 2);

  tick("fG", false);
  eq("without the g flag only the first is found", num("sMatches"), 1);
  tick("fG", true);

  set("pattern", "PRIYA");
  eq("capitals matter by default", num("sMatches"), 0);
  tick("fI", true);
  eq("until the i flag is ticked", num("sMatches"), 1);
  tick("fI", false);

  /* A pattern able to match nothing matches nothing everywhere. */
  set("pattern", "\\d*");
  has("zero-length matches are called out", txt("msg"), "zero length");
  ok("and drawn as empty markers", marks("mark.rx-empty") > 0);

  set("pattern", "(unclosed");
  has("an invalid pattern is reported", txt("msg"), "not a valid pattern");

  /* Warned about, never refused - plenty of such patterns are fine in use. */
  set("pattern", "(a+)+b");
  set("text", "aaaaaaaa");
  has("a repeat inside a repeat is warned about",
      txt("msg"), "repeat inside a repeat");

  /* The highlight is built as markup, so the visitor's text must be escaped
     on the way in. This is rule 11 at the one place on the site that has to
     build HTML out of what somebody typed. */
  set("pattern", "span");
  set("text", "<span onclick=zq>hello</span>");
  eq("the pattern still matches inside markup-looking text", num("sMatches"), 2);
  eq("but nothing became a real element", marks("span"), 0);
  ok("the angle brackets are shown as characters",
     document.getElementById("highlight").textContent.indexOf("<span") === 0,
     document.getElementById("highlight").textContent.slice(0, 40));

  click("resetBtn");
  has("reset asks for a pattern", txt("msg"), "Type a pattern");
  finish();
"""
# ---- END: the test for regex-tester ----

# ---- START: the test for json-to-csv ----
T["json-to-csv"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function lines() { return txt("output").split("\r\n"); }

  eq("three rows from the example", num("sRows"), 3);
  eq("five columns once the order is flattened", num("sCols"), 5);
  eq("the header row names them", lines()[0],
     "name,city,order.id,order.total,note");
  eq("a comma inside a value is quoted, not split",
     lines()[2].slice(0, 10), '"Ravi, Jr"');
  has("the row missing a note is explained, not dropped",
      txt("msg"), "some cells are empty");

  set("input", '[{"a":"say \\"hi\\""}]');
  eq("an inner quotation mark is doubled", lines()[1], '"say ""hi""' + '"');

  set("input", '[{"a":1,"b":2}]');
  set("delim", ";");
  eq("semicolons when asked for", lines()[0], "a;b");
  set("delim", ",");

  set("input", '[{"a":{"b":1}}]');
  eq("nesting becomes a dotted column", lines()[0], "a.b");
  tick("flatten", false);
  eq("unflattened it is one column", lines()[0], "a");
  eq("holding the JSON, quoted properly", lines()[1], '"{""b"":1}"');
  tick("flatten", true);

  set("input", '[{"a":"line\\nbreak"}]');
  ok("a line break inside a value forces quotes",
     lines()[1].charAt(0) === '"', lines()[1]);

  set("input", '{"x":1,"y":2}');
  eq("a lone object becomes one row", num("sRows"), 1);

  set("input", "[]");
  has("an empty array has nothing to convert", txt("msg"), "nothing to put");

  set("input", "{nope}");
  has("invalid JSON is reported", txt("msg"), "not valid JSON");

  click("clearBtn");
  has("clearing asks for JSON", txt("msg"), "Paste some JSON");
    /* JSON.parse rounds every number to about 16 digits, so an account
     number or an ID used to reach the table changed. */
  set("input", '[{"id":12345678901234567890,"amt":1e400,"small":12.5,"n":-9007199254740993}]');
  eq("long numbers reach the table digit for digit", txt("output"),
     "id,amt,small,n\r\n12345678901234567890,1e400,12.5,-9007199254740993");
  has("and the page says so", txt("msg"), "3 numbers are longer than a computer holds exactly (such as 12345678901234567890)");
  set("input", '[{"id":9007199254740991}]');
  eq("the largest exact one is an ordinary number", txt("output"), "id\r\n9007199254740991");
  eq("with nothing to say about it", txt("msg"), "1 row, 1 column.");
  set("input", '{"t":"a 12345678901234567890 in a string","u":"x"}');
  eq("digits inside a string are never touched", txt("output"), "t,u\r\na 12345678901234567890 in a string,x");
  eq("and draw no note", txt("msg"), "1 row, 2 columns.");
  set("input", '{"t":"say \\"12345678901234567890\\" now"}');
  eq("nor are digits after an escaped quotation mark", txt("output"), 't\r\n"say ""12345678901234567890"" now"');
  eq("which draw no note either", txt("msg"), "1 row, 1 column.");

  /* JSON.parse also keeps only the last of two keys with one name. */
  set("input", '[{"a":1,"a":2,"b":3}]');
  eq("a repeated key keeps its last value", txt("output"), "a,b\r\n2,3");
  has("and the page says the first was not used", txt("msg"), 'The key "a" appears twice in one object');
    /* A cell that starts with = + - or @ is a formula to a spreadsheet: data
     from a form could run one the moment the file was opened. */
  set("input", '[{"a":"=1+1","b":"-5","c":"+91 98765 43210","d":"@SUM(A1)","e":5,"f":"-x","g":"plain","h":-7}]');
  eq("formula text gets a ' in front, numbers and plain text do not", lines()[1],
     "'=1+1,-5,'+91 98765 43210,'@SUM(A1),5,'-x,plain,-7");
  has("and the page counts them", txt("msg"), "4 cells start with = + - or @, so a ' was put in front");
  set("input", '[{"=cmd":"1"}]');
  eq("a column name is text from the file too", lines()[0], "'=cmd");
  has("one cell is counted as one", txt("msg"), "One cell starts with = + - or @");
  set("input", '[{"a":"=HYPERLINK(\\"x\\",\\"y\\")"}]');
  eq("a guarded formula is still quoted properly", lines()[1], '"\'=HYPERLINK(""x"",""y"")"');
  set("input", '[{"a":"\\t=1"}]');
  eq("a tab in front does not hide one", lines()[1], "'\t=1");
  tick("guard", false);
  set("input", '[{"a":"=1+1","b":"@x"}]');
  eq("unticked, every value is kept exactly", lines()[1], "=1+1,@x");
  eq("with nothing to say about it", txt("msg"), "1 row, 2 columns.");
  tick("guard", true);
  eq("ticking it again guards them", lines()[1], "'=1+1,'@x");
  finish();
"""
# ---- END: the test for json-to-csv ----

# ---- START: the test for csv-to-json ----
T["csv-to-json"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function parsed() { return JSON.parse(txt("output")); }

  eq("three rows from the example", num("sRows"), 3);
  eq("four columns", num("sCols"), 4);
  eq("the separator was worked out", txt("sSep"), "comma");

  var rows = parsed();
  eq("a comma inside quotes stays one field", rows[0].name, "Sharma, Priya");
  eq("a doubled quotation mark becomes one", rows[2].name, 'Anjali "Anu"');
  eq("the phone keeps its leading zero", rows[0].phone, "09876543210");
  has("and it says everything was kept as text", txt("msg"), "kept as text");

  tick("typed", true);
  rows = parsed();
  eq("a plain number becomes a number", rows[0].pin, 500081);
  eq("but a leading zero is never touched", rows[0].phone, "09876543210");
  has("and the page explains why", txt("msg"), "leading zero");
  tick("typed", false);

  set("input", 'a,b\n"one\ntwo",3');
  rows = parsed();
  eq("a line break inside quotes does not end the row", rows.length, 1);
  eq("it belongs to the value", rows[0].a, "one\ntwo");

  set("input", "city,city\nPune,Nashik");
  rows = parsed();
  eq("a repeated heading is numbered", rows[0].city2, "Nashik");
  eq("so the first column survives", rows[0].city, "Pune");

  set("input", "1,2\n3,4");
  tick("headers", false);
  rows = parsed();
  eq("with no header row, rows are arrays", rows[0].length, 2);
  eq("and no row is eaten by the headings", rows.length, 2);
  tick("headers", true);

  set("input", "a;b\n1;2");
  eq("semicolons are detected", txt("sSep"), "semicolon");

  set("input", "a,b,c\n1,2");
  has("a short line is reported rather than reshaped",
      txt("msg"), "not have 3 fields");

  click("clearBtn");
  has("clearing asks for CSV", txt("msg"), "Paste some CSV");
      tick("typed", false);
    set("input", "a\n1");
    eq("one row is a row", txt("msg").indexOf("1 row,"), 0);
    finish();
"""
# ---- END: the test for csv-to-json ----

# ---- START: the test for cron-expression-parser ----
T["cron-expression-parser"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function runLines() {
    return txt("nextRuns").split("\n").filter(function (s) { return s !== ""; });
  }

  /* Opens on half past five, Monday to Friday. */
  has("half past five", txt("words"), "At 05:30");
  has("Monday to Friday, named", txt("words"), "Monday");
  has("and Friday", txt("words"), "Friday");
  eq("five fields", txt("sFields"), "5");
  eq("once a day", txt("sPerDay"), "1 time");
  eq("five dates are listed", runLines().length, 5);

  var weekendRuns = runLines().filter(function (line) {
    return line.indexOf("Sat") === 0 || line.indexOf("Sun") === 0;
  });
  eq("and none of them is a weekend", weekendRuns.length, 0);

  /* The rule everybody trips over, on its own button. */
  document.querySelector('[data-cron="0 0 13 * 5"]').click();
  has("both day fields restricted is called out", txt("msg"), "EITHER");
  has("and the words say so too", txt("words"), "EITHER matches");
  ok("so the dates are not all Fridays",
     runLines().filter(function (l) { return l.indexOf("Fri") === 0; }).length < 5,
     txt("nextRuns"));

  set("cron", "*/15 * * * *");
  has("a step reads as every fifteen minutes", txt("words"), "Every 15 minutes");
  eq("which is 96 times a day", txt("sPerDay"), "96 times");

  set("cron", "@daily");
  has("a shortcut is expanded", txt("words"), "At 00:00");
  has("and runs every day", txt("words"), "every day");

  set("cron", "0 9 * * MON-FRI");
  has("weekday names work", txt("words"), "Monday");
  has("and read to Friday", txt("words"), "Friday");

  set("cron", "0 0 1 JAN *");
  has("month names work too", txt("words"), "January");

  set("cron", "0 0 * * 7");
  has("seven is Sunday, as well as zero", txt("words"), "Sunday");

  set("cron", "bad");
  has("too few fields is refused", txt("msg"), "five fields");
  eq("and the count is shown", txt("sFields"), "1");

  set("cron", "99 * * * *");
  has("an impossible minute names the field", txt("msg"), "minute field");

  click("resetBtn");
  has("reset returns to the example", txt("words"), "At 05:30");
  finish();
"""
# ---- END: the test for cron-expression-parser ----

# ---- START: the test for html-minifier ----
T["html-minifier"] = r"""
  function out() { return txt("output"); }

  /* The sample has a comment, loose spacing, two inline elements side by
     side, and a pre block. */
  eq("the comment is gone", out().indexOf("the header"), -1);
  eq("runs of spaces are collapsed", out().indexOf("<div   class"), -1);
  ok("the div survives with one space", out().indexOf('<div class="card"') > -1, out());

  /* The word space between two inline elements is the whole point. */
  has("the space between b and i is kept", out(), "</b> <i>");

  /* pre is content, not formatting. */
  has("the pre block is untouched", out(), "<pre>  keep");
  ok("including its line break",
     out().indexOf("<pre>  keep\n  this  </pre>") > -1, out());

  ok("something was actually saved",
     parseFloat(txt("sSaved")) > 0, txt("sSaved"));

  /* Off by default, and it changes the page - which is why it says so. */
  tick("optBetween", true);
  has("with the third box on, the word space goes", out(), "</b><i>");
  has("and the page warns about it", txt("msg"), "word space");
  tick("optBetween", false);
  has("and comes back when it is off", out(), "</b> <i>");

  tick("optComments", false);
  has("comments stay when asked", out(), "the header");
  tick("optComments", true);

  /* The gzipped pair is the honest number. */
  waitFor("the gzipped sizes arrive",
    function () { return txt("sGzBefore") !== String.fromCharCode(0x2014); },
    function () {
      ok("gzipped before is a size or an honest n/a",
         txt("sGzBefore").length > 1, txt("sGzBefore"));
      ok("and so is gzipped after",
         txt("sGzAfter").length > 1, txt("sGzAfter"));

      click("clearBtn");
      has("clearing asks for HTML", txt("msg"), "Paste some HTML");
      finish();
    });
"""
# ---- END: the test for html-minifier ----

# ---- START: the test for css-minifier ----
T["css-minifier"] = r"""
  function out() { return txt("output"); }

  /* calc needs its spaces: calc(100%-2rem) is not a subtraction and the
     browser throws the whole declaration away without a word. */
  has("calc keeps its spaces", out(), "calc(100% - 2rem)");

  /* ".card a :hover" and ".card a:hover" match different elements. */
  has("the descendant space in a selector is kept", out(), "a :hover");

  /* Inside a block the colon is only a separator. */
  has("but the space after a colon in a block goes", out(), "color:#abc");
  has("and a repeated-pair colour is shortened", out(), "#abc");
  eq("the trailing semicolon before a brace is dropped", out().indexOf(";}"), -1);
  has("combinators are tightened", out(), ".card>.title");
  has("and so are selector commas", out(), ".card+.title");

  eq("the ordinary comment is gone", out().indexOf("the card"), -1);

  tick("optHex", false);
  has("colours stay long when asked", out(), "#aabbcc");
  tick("optHex", true);

  tick("optComments", false);
  has("comments stay when asked", out(), "the card");
  tick("optComments", true);

  /* Quoted text is content; url() may hold spaces and brackets. */
  set("input", '.a{content:"  two  ";background:url(a b.png)}');
  has("quoted spaces survive", out(), '"  two  "');
  has("and so does a url with a space", out(), "url(a b.png)");

  /* A licence comment is the one comment that must never be stripped. */
  set("input", "/*! MIT licence */ .a { color : red ; }");
  has("a bang comment is kept", out(), "/*! MIT licence */");
  has("while the rest is still minified", out(), ".a{color:red}");

  waitFor("the gzipped sizes arrive",
    function () { return txt("sGzBefore") !== String.fromCharCode(0x2014); },
    function () {
      ok("gzipped before is reported", txt("sGzBefore").length > 1, txt("sGzBefore"));
      click("clearBtn");
      has("clearing asks for CSS", txt("msg"), "Paste some CSS");
      finish();
    });
"""
# ---- END: the test for css-minifier ----

# ---- START: the test for sql-formatter ----
T["sql-formatter"] = r"""
  function out() { return txt("output"); }
  function lines() { return out().split("\n"); }
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  eq("SELECT is on a line of its own", lines()[0], "SELECT");
  ok("the columns come one per line", lines()[1].indexOf("  o.id,") === 0, lines()[1]);
  ok("FROM starts its own line",
     out().indexOf("\nFROM orders o") > -1, out());
  ok("the join does too",
     out().indexOf("\nINNER JOIN customers c ON") > -1, out());
  ok("AND is broken onto its own indented line",
     /\n\s+AND c\.city/.test(out()), out());
  ok("ORDER BY is one clause, not two",
     out().indexOf("\nORDER BY o.total DESC") > -1, out());

  has("a quoted value is carried through untouched", out(), "'Hyderabad'");
  has("table aliases are left alone", out(), "orders o");
  ok("several lines came out", num("sLines") >= 8, txt("sLines"));
  ok("tokens were counted", num("sTokens") > 20, txt("sTokens"));

  /* A keyword inside a string is a value, not a keyword. */
  set("input", "select a from t where b = 'select' and c = 'FROM'");
  has("a keyword inside quotes stays as written", out(), "'select'");
  has("in either case", out(), "'FROM'");
  has("while the real keyword is capitalised", out(), "SELECT");

  /* Comments keep their contents exactly. */
  set("input", "select a -- keep this Select\nfrom t");
  has("a line comment is carried through", out(), "-- keep this Select");

  set("input", "select a /* block Select */ from t");
  has("so is a block comment", out(), "/* block Select */");

  /* Capitals are a convention, so they can be turned off. */
  set("input", "select a from t");
  has("capitals by default", out(), "SELECT");
  tick("optUpper", false);
  has("and lower case when asked", out(), "select");
  eq("with no capitals left", out().indexOf("SELECT"), -1);
  tick("optUpper", true);

  /* Indent width. */
  set("input", "select a, b from t");
  eq("two spaces by default", lines()[1], "  a,");
  set("indent", "4");
  eq("four when chosen", lines()[1], "    a,");
  set("indent", "2");

  click("clearBtn");
  has("clearing asks for SQL", txt("msg"), "Paste some SQL");
    /* The page says nothing is added, and it was adding spaces INSIDE
     pieces of SQL: >= became "> =", <> "< >", $1 "$ 1", t.* "t. *",
     1.5e3 "1.5 e3" and COUNT(*) "COUNT (*)" - which MySQL refuses. None
     of that SQL would run. Each case below is the whole output. */
  set("input", "select a from t where a>=1 and b<>2 and c!=3 and d<=4 and e||f = 'x' and g::int = 5 and h->>'k' = 'v'");
  eq("operators of two and three characters stay whole", out(),
     "SELECT\n  a\nFROM t\nWHERE a >= 1\n  AND b <> 2\n  AND c != 3\n  AND d <= 4\n  AND e || f = 'x'\n  AND g::INT = 5\n  AND h ->> 'k' = 'v'");
  set("input", "select * from t where id = $1 and name = :name and x = @var and y = @@sess and z = ? and t.* is not null");
  eq("parameters, variables and t.* stay whole", out(),
     "SELECT\n  *\nFROM t\nWHERE id = $1\n  AND name = :name\n  AND x = @var\n  AND y = @@sess\n  AND z = ?\n  AND t.* IS NOT NULL");
  set("input", "select upper(name), coalesce(a,b), count(distinct x), now(), count (*) from t");
  eq("a bracket typed against a function stays against it, one typed apart stays apart", out(),
     "SELECT\n  upper(name),\n  COALESCE(a, b),\n  COUNT(DISTINCT x),\n  now(),\n  COUNT (*)\nFROM t");
  set("input", "select -1, a-1, a - -2, (-3), 1.5e3, .5, 2E-4 from t where b in (-1,+2)");
  eq("numbers stay whole, signs included", out(),
     "SELECT\n  -1,\n  a - 1,\n  a - -2,\n  (-3),\n  1.5e3,\n  .5,\n  2E-4\nFROM t\nWHERE b IN (-1, +2)");
  set("input", "select N'name', E'tab', x'FF', $$ body $1 $$, $fn$ x $fn$ from \"s\".\"t\" where a = 'it''s'");
  eq("prefixed and dollar-quoted strings and quoted names stay whole", out(),
     "SELECT\n  N'name',\n  E'tab',\n  x'FF',\n  $$ body $1 $$,\n  $fn$ x $fn$\nFROM \"s\".\"t\"\nWHERE a = 'it''s'");
  set("input", "select x::numeric(10,2) from t where a in(1,2)");
  eq("a cast with a size, and IN written against its bracket", out(),
     "SELECT\n  x::NUMERIC(10, 2)\nFROM t\nWHERE a IN(1, 2)");

  /* Formatting the output again changes nothing. */
  set("input", "select a,b , c from t join u on t.id=u.id where a>=-1 and b<>'x' or c::int=$2 order by 1");
  var once = out();
  set("input", once);
  eq("formatting twice is the same as once", out(), once);
  finish();
"""
# ---- END: the test for sql-formatter ----

# ---- START: the test for readability-score ----
T["readability-score"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  /* The sample opens with one very long sentence and three short ones, so
     the warning about long sentences should be the one showing. */
  has("the longest sentence is named", txt("msg"), "longest sentence");
  eq("averaging 9.8 words a sentence", txt("sPerSentence"), "9.8");
  ok("the worst-sentence list is filled", txt("worst").length > 40, txt("worst"));
  has("with a word count against each", txt("worst"), "words -");

  /* Arithmetic that can be checked by hand: 11 words, 4 sentences, and
     every word one syllable. */
  set("input", "The cat sat. The dog ran. The bird flew. Rain fell.");
  eq("eleven words", num("sWords"), 11);
  eq("four sentences", num("sSentences"), 4);
  eq("2.8 words a sentence", txt("sPerSentence"), "2.8");
  eq("one syllable each", txt("sPerWord"), "1.00");
  eq("which is off the top of the scale", txt("bigScore"), "100.0");
  has("and reads as very easy", txt("bigBand"), "Very easy");

  /* Under ten words the score would be noise, so it is not given. */
  set("input", "Too short.");
  has("a very short text is refused a score", txt("msg"), "at least ten words");
  eq("and the tiles are blank", txt("bigScore"), String.fromCharCode(0x2014));

  set("input", "");
  has("an empty box says the same", txt("msg"), "at least ten words");

  click("clearBtn");
  eq("clearing empties the box", val("input"), "");
    /* Every full stop used to end a sentence: titles, initials, p.m. and
     even the point in 3.50. Short "sentences" pushed the score up. */
  function sentences(text) { set("input", text); return txt("sSentences"); }
  eq("titles and p.m. do not end a sentence",
     sentences("Dr. Smith went home at 5 p.m. today with his dog. He slept well. Mr. Rao said e.g. this is fine."), "3");
  eq("a decimal point does not either",
     sentences("It costs 3.50 rupees today in the big market. That is cheap for everyone here."), "2");
  eq("nor do initials",
     sentences("J. K. Rowling wrote many books for young readers. They sold very well indeed."), "2");
  eq("a short form followed by a capital letter does end one",
     sentences("We lived for ten long years in the U.S. Then we moved back home again."), "2");
  eq("and etc. before a capital",
     sentences("Bring pens, paper, books etc. And bring your lunch box with you today."), "2");
  eq("question and exclamation marks always end one",
     sentences("Wait for me here please! Did you really see it there? Yes I did see it."), "3");
  eq("a quotation mark after the full stop belongs to that sentence",
     sentences("He looked at us and said \"Stop right now.\" Then he walked away slowly."), "2");
  eq("and a run of dots ends one",
     sentences("I waited and waited for the bus... It never came to our stop at all."), "2");

  /* That example was measured as nine "sentences" of two words each. */
  set("input", "Dr. Smith went home at 5 p.m. today. He slept. Mr. Rao said e.g. this is fine.");
  near("so its sentences average six words, not two", txt("sPerSentence"), 6, 0.05);

  /* Telugu text has words, none of them A to Z. */
  var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
  set("input", (TE + " " + TE + " " + TE + ". ").repeat(6));
  has("text in another script is told the score is for English", txt("msg"), "worked out for English");
  set("input", "Too short.");
  has("while short English is still asked for ten words", txt("msg"), "at least ten words");
  finish();
"""
# ---- END: the test for readability-score ----

# ---- START: the test for text-diff-checker ----
T["text-diff-checker"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }
  function marks(sel) { return document.querySelectorAll("#output " + sel).length; }

  /* The sample changes one line and adds one. */
  eq("two lines added", num("sAdded"), 2);
  eq("one line removed", num("sRemoved"), 1);
  eq("three unchanged", num("sSame"), 3);
  eq("the additions are marked", marks(".df-add"), 2);
  eq("and so are the removals", marks(".df-del"), 1);

  /* An edited line is one removal and one addition - there is no third kind. */
  set("left", "one\ntwo\nthree");
  set("right", "one\nTWO\nthree");
  eq("an edit shows as an addition", num("sAdded"), 1);
  eq("and a removal", num("sRemoved"), 1);

  tick("optCase", true);
  eq("ignoring capitals makes it vanish", num("sAdded"), 0);
  has("and the page says the raw text still differs", txt("msg"), "raw text is not identical");
  tick("optCase", false);

  /* The invisible difference this page CAN see. */
  set("left", "alpha\nbeta");
  set("right", "alpha \nbeta");
  eq("a trailing space is a real difference", num("sAdded"), 1);
  tick("optTrail", true);
  eq("until it is ignored", num("sAdded"), 0);
  tick("optTrail", false);

  tick("optShow", true);
  has("showing invisibles marks the spaces", txt("output"), String.fromCharCode(0x00B7));
  tick("optShow", false);

  set("left", "same");
  set("right", "same");
  has("identical text is called identical", txt("msg"), "identical");

  /* Somebody's document must never become markup. */
  set("left", "<iframe onload=zq>");
  set("right", "plain");
  eq("a tag in the text does not become an element", marks("iframe"), 0);
  has("it is shown as characters", txt("output"), "<iframe");

  set("left", "a\nb\nc");
  set("right", "");
  eq("emptying one side removes every line", num("sRemoved"), 3);

  click("swapBtn");
  eq("swapping turns removals into additions", num("sAdded"), 3);

  click("clearBtn");
  has("clearing asks for both versions", txt("msg"), "Paste a version");
  finish();
"""
# ---- END: the test for text-diff-checker ----

# ---- START: the test for text-to-speech ----
T["text-to-speech"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  /* Voices come from the machine, so a test machine may have none. Both
     paths have to be right, and the no-voice path is the one that has to
     explain itself rather than looking broken. */
  var count = num("sVoices");

  if (count === 0) {
    has("with no voices it says where voices come from",
        txt("msg"), "come from your device");
    ok("and the speak button is disabled",
       document.getElementById("speakBtn").disabled === true);
    has("the list explains instead of being blank",
        txt("voiceList"), "come from your device");
  } else {
    var lines = txt("voiceList").split("\n").filter(function (s) { return s !== ""; });
    eq("every voice is listed", lines.length, count);
    ok("and each is marked local or network",
       lines.every(function (l) { return /^\[(local|network)\] /.test(l); }), lines[0]);
    ok("the tile says where the chosen one runs",
       txt("sWhere") === "on this device" || txt("sWhere") === "uses the internet",
       txt("sWhere"));
  }

  /* These work with or without a voice. */
  eq("the character count matches the text",
     num("sLength"), val("input").length);
  set("input", "Hello there");
  eq("and follows it", num("sLength"), 11);

  set("rate", "1.6");
  eq("the speed label follows the slider", txt("rateOut"), "1.6");
  set("pitch", "0.4");
  eq("and so does the pitch label", txt("pitchOut"), "0.4");

  ok("pause and stop start out unavailable",
     document.getElementById("pauseBtn").disabled === true &&
     document.getElementById("stopBtn").disabled === true);
      /* A voice reads only its own language. Telugu given to an English
       voice used to draw "This voice runs entirely on your device". */
    var TE = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
    var all = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
    if (all.length) {
      var teVoices = all.filter(function (v) { return /^te([-_]|$)/i.test(v.lang); });
      set("input", TE + " " + TE);
      if (!teVoices.length) {
        has("Telugu with no Telugu voice says so", txt("msg"), "no voice on this device reads it");
      } else {
        has("Telugu with a Telugu voice not chosen says choose one", txt("msg"), "Choose a Telugu voice");
      }
      set("input", "Hello there");
      eq("English text draws no language note", txt("msg").indexOf("This text is in"), -1);
    } else {
      ok("this browser lists no voices, so the language note cannot be tested here", true);
    }
    finish();
"""
# ---- END: the test for text-to-speech ----

# ---- START: the test for working-days-calculator ----
T["working-days-calculator"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  /* Monday 5 January 2026 to Friday 9 January 2026. */
  set("from", "2026-01-05");
  set("to", "2026-01-09");
  eq("Monday to Friday is five working days", num("bigWorking"), 5);
  eq("and five days in all - both ends count", num("sTotal"), 5);
  eq("with no weekend in between", num("sWeekend"), 0);
  eq("one working week", txt("sWeeks"), "1.0");
  eq("forty hours at eight a day", num("sHours"), 40);

  /* Out to the Sunday. */
  set("to", "2026-01-11");
  eq("seven calendar days", num("sTotal"), 7);
  eq("still five working days", num("bigWorking"), 5);
  eq("two of them a weekend", num("sWeekend"), 2);

  /* A holiday on a working day costs a working day. */
  set("holidays", "2026-01-07");
  eq("the holiday is taken off", num("bigWorking"), 4);
  eq("and counted as used", num("sHolidays"), 1);

  /* A holiday on a Sunday was already not a working day. */
  set("holidays", "2026-01-07\n2026-01-11");
  eq("a holiday on a weekend changes nothing", num("bigWorking"), 4);
  eq("and is not counted twice", num("sHolidays"), 1);

  set("holidays", "not a date");
  has("an unreadable holiday line is reported", txt("msg"), "could not be read");
  set("holidays", "");

  /* A Friday-Saturday weekend, which is normal in much of the world. */
  tick("d0", false);
  tick("d5", true);
  eq("a Friday-Saturday weekend, as much of the world has", num("sWeekend"), 2);
  has("the message names both days", txt("msg"), "Friday and Saturday");

  tick("d5", false); tick("d6", false);
  eq("with no weekend every day is a working day", num("bigWorking"), 7);
  has("and the page says so", txt("msg"), "No weekend is ticked");

  set("from", "2026-02-01");
  set("to", "2026-01-01");
  has("a backwards range is refused", txt("msg"), "Swap them");

  click("resetBtn");
  eq("reset puts the weekend back", num("sWeekend") >= 0, true);
  var DASH = String.fromCharCode(0x2014);

    /* "The second date is before the first" sat beside an old count. */
    set("from", "2026-10-31"); set("to", "2026-10-01");
    eq("a reversed range leaves no count", txt("bigWorking"), DASH);
    has("and says why", txt("msg"), "before the first");
    set("from", "2026-10-01"); set("to", "2026-10-31");
    ok("and the right way round counts again", txt("bigWorking") !== DASH, txt("bigWorking"));
    finish();
"""
# ---- END: the test for working-days-calculator ----

# ---- START: the test for stopwatch-timer ----
T["stopwatch-timer"] = r"""
  eq("it opens stopped at zero", txt("display"), "00:00.00");
  eq("and says so", txt("sState"), "ready");
  ok("lap is not available yet",
     document.getElementById("lapBtn").disabled === true);

  click("startBtn");
  eq("starting says running", txt("sState"), "running");
  eq("and the button becomes stop", txt("startBtn"), "Stop");
  has("the message explains why a background tab is safe",
      txt("msg"), "lose you any time");

  waitFor("the clock moves",
    function () { return txt("display") !== "00:00.00"; },
    function () {
      click("lapBtn");
      eq("a lap is recorded", txt("sLaps"), "1");
      ok("with a split and a total", txt("laps").indexOf("total") > -1, txt("laps"));
      click("lapBtn");
      eq("and another", txt("sLaps"), "2");
      ok("the fastest is marked",
         txt("laps").indexOf("fastest") > -1, txt("laps"));

      click("startBtn");
      eq("stopping says stopped", txt("sState"), "stopped");
      var frozen = txt("display");
      var since = Date.now();

      waitFor("time passes while it is stopped",
        function () { return Date.now() - since > 300; },
        function () {
          eq("and the display has not moved", txt("display"), frozen);

          click("resetBtn");
          eq("reset returns to zero", txt("display"), "00:00.00");
          eq("clears the laps", txt("sLaps"), "0");
          eq("and says ready", txt("sState"), "ready");

          /* Countdown, one second, so the test does not sit waiting. */
          set("mode", "countdown");
          eq("the label changes", txt("displayLabel"), "Countdown");
          set("mins", 0);
          set("secs", 1);
          click("startBtn");
          waitFor("a one-second countdown finishes",
            function () { return txt("sState") === "finished"; },
            function () {
              eq("and lands exactly on zero", txt("display"), "00:00.00");
              has("saying so", txt("msg"), "Time is up");

              click("resetBtn");
              set("mins", 0); set("secs", 0);
              click("startBtn");
              has("a zero countdown is refused", txt("msg"), "Set a time");
              finish();
            }, 8000);
        }, 4000);
    }, 4000);
"""
# ---- END: the test for stopwatch-timer ----

# ---- START: the test for time-zone-converter ----
T["time-zone-converter"] = r"""
  /* A fixed date, so daylight saving is a fact rather than a variable. */
  set("zone", "Asia/Kolkata");
  set("date", "2026-01-15");
  set("time", "09:00");
  eq("9am in India is 03:30 UTC", txt("sUtc"), "03:30");
  eq("and India is always five and a half hours ahead", txt("sOffset"), "UTC+05:30");
  has("the list names the zone, not a city", txt("output"), "Asia/Kolkata");
  has("and marks where the visitor is", txt("output"), "where you are");

  /* Chrome on an Indian PC reports the old name Asia/Calcutta. Taken as it
     came, India was listed twice, once under each name. */
  var raw = Intl.DateTimeFormat().resolvedOptions().timeZone;
  var offered = Array.prototype.map.call(
    document.getElementById("zone").options, function (o) { return o.value; });
  eq("no zone is offered twice", new Set(offered).size, offered.length);
  var listed = txt("output").split("\n").map(function (r) {
    return r.replace(/  <- where you are$/, "").split(" ").pop();
  }).filter(function (z) { return z !== ""; });
  eq("no zone is listed twice", new Set(listed).size, listed.length);
  ok("and no old name reaches the page",
     ["Asia/Calcutta", "Asia/Katmandu", "Asia/Saigon"].every(function (z) {
       return offered.indexOf(z) === -1 && listed.indexOf(z) === -1;
     }), raw);
  if (raw === "Asia/Calcutta") {
    eq("this browser's Asia/Calcutta is shown as Asia/Kolkata", txt("sHere"), "Asia/Kolkata");
    has("and the mark sits on it", txt("output"), "Asia/Kolkata  <- where you are");
    eq("India is offered once", offered.filter(function (z) { return z === "Asia/Kolkata"; }).length, 1);
  }
  has("India keeps one offset all year, and the page says so",
      txt("msg"), "same offset all year");

  set("date", "2026-07-15");
  eq("still 03:30 in July", txt("sUtc"), "03:30");

  /* Britain moves its clocks; India does not. This is the whole point. */
  set("zone", "Europe/London");
  set("date", "2026-01-15");
  eq("9am in London in January is 09:00 UTC", txt("sUtc"), "09:00");
  eq("offset zero", txt("sOffset"), "UTC+00:00");
  has("and the page warns the date matters", txt("msg"), "changes its clocks");

  set("date", "2026-07-15");
  eq("9am in London in July is 08:00 UTC", txt("sUtc"), "08:00");
  eq("an hour ahead in summer", txt("sOffset"), "UTC+01:00");

  set("zone", "America/New_York");
  set("date", "2026-01-15");
  eq("9am in New York in January is 14:00 UTC", txt("sUtc"), "14:00");
  set("date", "2026-07-15");
  eq("and 13:00 in July", txt("sUtc"), "13:00");

  set("zone", "UTC");
  set("date", "2026-03-10");
  set("time", "00:00");
  eq("midnight UTC does not roll the date", txt("sUtc"), "00:00");

  var rows = txt("output").split("\n").filter(function (s) { return s !== ""; });
  ok("every major zone is listed", rows.length >= 15, rows.length + " rows");
  ok("each row carries an offset",
     rows.every(function (r) { return r.indexOf("UTC") > -1; }), rows[0]);

  click("nowBtn");
  ok("using now fills in today", val("date").length === 10, val("date"));
  finish();
"""
# ---- END: the test for time-zone-converter ----

# ---- START: the test for unit-price-comparison ----
T["unit-price-comparison"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  /* 500g at 45, 1kg at 82, 2.5kg at 235 - so 9.00, 8.20 and 9.40 per 100g.
     The middle pack wins and the biggest is the worst buy, which is the
     whole point the page is making. */
  eq("the middle pack is best value", txt("bigBest"), "Pack 2");
  eq("three packs compared", txt("sPacks"), "3");
  eq("compared per 100 grams", txt("sUnit"), "100 g");
  has("at 8.20", txt("bigLabel"), "8.20");
  ok("a saving of about 13 per cent",
     num("sSaving") > 12 && num("sSaving") < 14, txt("sSaving"));
  has("the winner is marked in the list", txt("output"), "BEST  Pack 2");
  /* 1 kg won with a 500 g pack beside it, and the page used to say "the
     SMALLER pack wins". What is true is that the biggest did not. */
  has("the page says the biggest pack lost", txt("msg"), "BIGGEST pack is not the best buy");
  ok("and not that the smaller one won", txt("msg").indexOf("SMALL") === -1, txt("msg"));

  /* The smallest of three winning: 500 g at 30 (6.00 per 100 g) beats 8.20
     and 9.40. Against 1 kg alone the gap is still wide enough to name a
     winner rather than call it a tie. */
  set("p1", "30");
  eq("the smallest pack can win", txt("bigBest"), "Pack 1");
  has("and is called the smallest of three", txt("msg"), "SMALLEST pack wins");
  /* Of two packs it is the smaller. */
  set("p3", ""); set("q3", "");
  has("of two it is the smaller", txt("msg"), "SMALLER pack wins");
  /* The biggest winning is the ordinary case, and says so. */
  set("p1", "60");
  eq("the bigger pack can win", txt("bigBest"), "Pack 2");
  has("and the page says by how much", txt("msg"), "Best value by");
  /* The dearest pack need not be the biggest. 500 g at 60 is the worst buy
     here and 2.5 kg at 225 sits between, so 1 kg wins while the biggest
     loses - which the page must say, rather than praise a big pack. */
  set("p3", "225"); set("q3", "2.5");
  eq("1 kg wins between a dear small pack and a big one", txt("bigBest"), "Pack 2");
  has("and the biggest is said to have lost", txt("msg"), "BIGGEST pack is not the best buy");
  set("p1", "45"); set("p3", "235");
  eq("back to the example", txt("bigBest"), "Pack 2");

  /* Kilos and grams are the same measure, so they compare happily. */
  set("u1", "kg"); set("q1", "0.5");
  eq("half a kilo is the same as 500 grams", txt("bigBest"), "Pack 2");
  set("u1", "g"); set("q1", "500");

  /* Weight against volume is not a comparison anyone can make. */
  set("u2", "ml");
  has("mixed measures are refused", txt("msg"), "not measured in the same way");
  eq("and no winner is claimed", txt("bigBest"), String.fromCharCode(0x2014));
  set("u2", "kg");

  /* Pieces compare only with pieces. */
  set("u1", "piece"); set("q1", "10"); set("p1", "50");
  set("u2", "piece"); set("q2", "24"); set("p2", "96");
  set("p3", ""); set("q3", "");
  eq("24 for 96 beats 10 for 50", txt("bigBest"), "Pack 2");
  eq("compared per piece", txt("sUnit"), "piece");

  /* A near tie is not worth crossing the aisle for. */
  set("p2", "50"); set("q2", "10");
  has("a tie is called a tie", txt("msg"), "not worth choosing");

  set("p2", ""); set("q2", "");
  has("one pack alone cannot be compared", txt("msg"), "at least two packs");

  click("resetBtn");
  has("reset empties everything", txt("msg"), "at least two packs");
  finish();
"""
# ---- END: the test for unit-price-comparison ----

# ---- START: the test for scientific-calculator ----
T["scientific-calculator"] = r"""
  /* sin(30) in degrees is 0.5, plus sqrt(16) times 2 which is 8. */
  eq("the opening sum", txt("answer"), "8.5");
  eq("angles start in degrees", txt("sMode"), "deg");

  set("angle", "rad");
  eq("the mode tile follows", txt("sMode"), "rad");
  ok("and the answer changes completely", txt("answer") !== "8.5", txt("answer"));
  has("with a warning about which mode is on", txt("msg"), "radians");
  set("angle", "deg");
  eq("back to 8.5", txt("answer"), "8.5");

  set("expr", "2+3*4");
  eq("multiplication before addition", txt("answer"), "14");
  set("expr", "(2+3)*4");
  eq("brackets win", txt("answer"), "20");
  set("expr", "2^3^2");
  eq("powers go right to left", txt("answer"), "512");
  set("expr", "2(3+4)");
  eq("writing the multiply sign is optional", txt("answer"), "14");
  set("expr", "-5+3");
  eq("a minus at the front is a sign", txt("answer"), "-2");
  set("expr", "5!");
  eq("factorial", txt("answer"), "120");
  set("expr", "7%3");
  eq("the percent key is a remainder", txt("answer"), "1");

  /* The key is labelled mod, so the word is typed too. It used to be read
     as 10mod3 with the spaces gone, and "mod3" was refused as a function. */
  set("expr", "10 mod 3");
  eq("mod typed as a word is the remainder", txt("answer"), "1");
  set("expr", "10mod3");
  eq("with no spaces as well", txt("answer"), "1");
  set("expr", "2 + 10 mod 4");
  eq("mod ranks with * and /, above +", txt("answer"), "4");
  set("expr", "-7 mod 3");
  eq("a negative keeps its sign, as the page says", txt("answer"), "-1");
  set("expr", "10 MOD (2+2)");
  eq("in capitals and before a bracket", txt("answer"), "2");
  set("expr", "17 mod pi");
  eq("and before a constant", txt("answer"), String(Number((17 % Math.PI).toPrecision(12))));
  click("clearBtn");
  document.querySelector('[data-ins="%"]').click();
  eq("the mod key still writes %", val("expr"), "%");
  set("expr", "modulo(3)");
  has("a longer word is not mod", txt("msg"), "not a function");

  /* No answer at all is a different thing from an answer too big. */
  set("expr", "sqrt(-1)");
  eq("the square root of -1 is not a number", txt("answer"), "not a number");
  has("and the page says there is no real answer", txt("msg"), "no real-number answer");
  ok("rather than blaming a division by zero", txt("msg").indexOf("past what") === -1, txt("msg"));
  set("expr", "0/0");
  has("0 / 0 has no answer either", txt("msg"), "no real-number answer");
  set("expr", "1/0");
  has("1 / 0 is still called a division by zero", txt("msg"), "division by zero");
  set("expr", "ln(0)");
  has("and ln(0) runs off the end of the format", txt("msg"), "past what");
  set("expr", "log(100)");
  eq("log is base ten", txt("answer"), "2");
  set("expr", "ln(e)");
  eq("ln is natural", txt("answer"), "1");

  /* Floating point, reported rather than hidden. */
  set("expr", "0.1+0.2");
  eq("the display is rounded to something readable", txt("answer"), "0.3");
  eq("and the tile admits it", txt("sExact"), "rounded");
  has("with the real value spelled out", txt("msg"), "0.30000000000000004");
  tick("optFull", true);
  has("and shown in full when asked", txt("answer"), "0.30000000000000004");
  tick("optFull", false);

  set("expr", "2+2");
  eq("an exact answer says exact", txt("sExact"), "exact");

  /* It refuses rather than guessing, and nothing is run as code. */
  set("expr", "2+");
  has("a dangling operator is refused", txt("msg"), "missing a number");
  set("expr", "(2+3");
  has("an unclosed bracket is named", txt("msg"), "never closes");
  set("expr", "foo(2)");
  has("an unknown function is named", txt("msg"), "not a function");
  set("expr", "constructor");
  has("a built-in property is not a function here", txt("msg"), "not a function");
  set("expr", "alert(1)");
  has("and neither is anything else from the page", txt("msg"), "not a function");

  set("expr", "");
  has("an empty box asks for a sum", txt("msg"), "Type a sum");

  /* The keypad appends to the expression. */
  document.querySelector('[data-ins="pi"]').click();
  has("the pi key inserts pi", val("expr"), "pi");
  ok("and it is worked out", txt("answer").indexOf("3.14159") === 0, txt("answer"));

  click("clearBtn");
  eq("C empties the box", val("expr"), "");
  finish();
"""
# ---- END: the test for scientific-calculator ----

# ---- START: the test for calorie-calculator ----
T["calorie-calculator"] = r"""
  function num(id) { return Number(txt(id).replace(/[^0-9.-]/g, "")); }

  /* Mifflin-St Jeor, worked by hand: 10*70 + 6.25*170 - 5*30 + 5 = 1617.5,
     and 1617.5 * 1.375 = 2224. */
  eq("resting burn", num("sBmr"), 1618);
  eq("and the daily total", num("bigTdee"), 2224);
  has("a range is given, not just a number", txt("sRange"), "-");
  has("the activity part is shown as an addition", txt("sActivity"), "+");

  /* The two variants differ by a flat 166. */
  set("sex", "female");
  eq("the female variant is 166 lower", num("sBmr"), 1452);
  set("sex", "male");

  /* The multiplier is the part that moves the answer most. */
  set("activity", "1.2");
  eq("desk work", num("bigTdee"), 1941);
  set("activity", "1.9");
  eq("a physical job", num("bigTdee"), 3073);
  set("activity", "1.375");

  var rows = txt("ladder").split("\n").filter(function (s) { return s !== ""; });
  eq("every activity level is listed", rows.length, 5);
  eq("with the chosen one marked",
     rows.filter(function (r) { return r.indexOf(">") === 0; }).length, 1);
  has("and the message explains why the ladder matters",
      txt("msg"), "row you pick matters more");

  /* Pounds and feet land within a couple of calories of the metric answer. */
  set("units", "imperial");
  ok("imperial agrees with metric", Math.abs(num("sBmr") - 1618) < 5, txt("sBmr"));
  set("units", "metric");

  /* Outside the range the formula was built on, it says so. */
  set("age", "10");
  has("a child is out of range", txt("msg"), "outside the range");
  set("age", "30");

  set("kg", "");
  has("a missing weight is asked for", txt("msg"), "Fill in weight");

  click("resetBtn");
  eq("reset returns to the example", num("sBmr"), 1618);
  var DASH = String.fromCharCode(0x2014);

    set("units", "metric"); set("kg", "70"); set("cm", "170"); set("age", "30");
    ok("an answer", txt("bigTdee") !== DASH, txt("bigTdee"));
    set("age", "150");
    eq("out of range leaves no answer beside the warning", txt("bigTdee"), DASH);
    set("age", "0");
    has("a typed zero is not asked to be filled in", txt("msg"), "above zero");
    set("age", "30");
    finish();
"""
# ---- END: the test for calorie-calculator ----

# ---- START: the test for image-color-picker ----
T["image-color-picker"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  function clickAt(x, y) {
    var c = document.getElementById("stage");
    var r = c.getBoundingClientRect();
    c.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      clientX: r.left + (x + 0.5) * r.width / c.width,
      clientY: r.top + (y + 0.5) * r.height / c.height
    }));
  }

  clickAt(1, 1);
  has("clicking before choosing a picture says so", txt("msg"), "Choose an image first");

  makeImage("file", 200, 100, function () {
    waitFor("the picture is drawn",
      function () { return txt("msg").indexOf("Click anywhere") > -1; },
      function () {
        eq("the canvas is the picture's real size", document.getElementById("stage").width, 200);

        /* Pixel (20,12) sits in the block that starts at (16,8), which the
           test image paints rgb(16, 8, 128). Worked by hand from its own rule. */
        clickAt(20, 12);
        eq("the exact pixel's RGB", txt("sRgb"), "16, 8, 128");
        eq("its HEX", txt("sHex"), "#100880");
        eq("its HSL, worked out by hand", txt("sHsl"), "244, 88%, 27%");
        has("the history keeps it", txt("picked"), "#100880");

        clickAt(100, 40);
        eq("another pixel is a different colour", txt("sHex"), "#602880");
        eq("and history grows", txt("picked").split("\n").length, 2);
        eq("newest at the top", txt("picked").indexOf("#602880"), 0);

        /* A 3x3 patch across a block edge averages: x=7 is one column of
           r=0 and x=8,9 are two columns of r=8, so (0*3 + 8*6) / 9 = 5.33. */
        set("area", "3");
        clickAt(8, 12);
        eq("a patch across an edge averages the neighbours", txt("sRgb"), "5, 8, 128");
        set("area", "1");
        clickAt(8, 12);
        eq("while one pixel is exact", txt("sRgb"), "8, 8, 128");

        var chips = document.querySelectorAll("#palette .palette-chip");
        ok("a palette was found", chips.length >= 1 && chips.length <= 6, chips.length + " chips");
        chips[0].click();
        ok("clicking a palette colour picks it", /^#[0-9a-f]{6}$/.test(txt("sHex")), txt("sHex"));

        /* A fully transparent pixel has no colour, and must say so rather
           than passing off the zeros stored there as black. */
        var clear = document.createElement("canvas");
        clear.width = 20; clear.height = 20;
        clear.toBlob(function (blob) {
          feed("file", new File([blob], "clear.png", { type: "image/png" }));
          waitFor("the transparent picture loads",
            function () { return document.getElementById("stage").width === 20; },
            function () {
              clickAt(5, 5);
              has("a transparent pixel is called transparent", txt("msg"), "fully transparent");

              click("resetBtn");
              eq("reset clears the colour", txt("sHex"), DASH);
              eq("and the history", txt("picked"), "");
              finish();
            });
        }, "image/png");
      });
  });
"""
# ---- END: the test for image-color-picker ----

# ---- START: the test for image-cropper ----
T["image-cropper"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  function fire(type, x, y) {
    var c = document.getElementById("stage");
    var r = c.getBoundingClientRect();
    c.dispatchEvent(new PointerEvent(type, {
      bubbles: true, cancelable: true, pointerId: 1,
      clientX: r.left + x * r.width / c.width,
      clientY: r.top + y * r.height / c.height
    }));
  }
  function drag(x1, y1, x2, y2) { fire("pointerdown", x1, y1); fire("pointermove", x2, y2); fire("pointerup", x2, y2); }
  function box() {
    return [Number(val("cx")), Number(val("cy")), Number(val("cw")), Number(val("ch"))].join(",");
  }

  makeImage("file", 200, 100, function () {
    waitFor("the crop is ready",
      function () { return txt("sBytes") !== DASH; },
      function () {
        eq("opens on a centred box covering 80%", txt("sSize"), "160 " + TIMES + " 80");
        eq("positioned in the middle", txt("sPos"), "20, 10");
        eq("the original's size is shown", txt("sOriginal"), "200 " + TIMES + " 100");
        eq("64 per cent kept", txt("sArea"), "64%");
        eq("the four boxes agree", box(), "20,10,160,80");
        ok("the download is available", document.getElementById("dlBtn").disabled === false);

        /* Small and out of the way, so the drags below start on empty picture. */
        set("cw", 40); set("ch", 20); set("cx", 0); set("cy", 0);
        eq("typed pixels set the box", box(), "0,0,40,20");

        drag(100, 60, 150, 90);
        eq("dragging on empty picture draws a new box", box(), "100,60,50,30");

        drag(120, 75, 130, 80);
        eq("dragging inside moves it, keeping its size", box(), "110,65,50,30");

        drag(160, 95, 180, 90);
        eq("dragging a corner resizes it, the opposite corner staying put", box(), "110,65,70,25");

        drag(180, 90, 250, 150);
        eq("dragging past the edge stops at the edge", box(), "110,65,90,35");

        /* Shape locked. */
        set("aspect", "1:1");
        eq("choosing a shape fits the box to it", box(), "110,10,90,90");
        drag(20, 20, 60, 30);
        eq("a locked box follows the pointer but keeps its shape", box(), "20,20,40,40");
        set("cw", 60);
        eq("typing a width moves the height with it", box(), "20,20,60,60");
        set("ch", 30);
        eq("and the other way round", box(), "20,20,30,30");
        set("aspect", "16:9");
        eq("16:9 of that width", box(), "20,20,30,17");
        set("aspect", "free");

        /* A box cannot be bigger than the picture. */
        set("cx", 0); set("cy", 0); set("cw", 999); set("ch", 999);
        eq("oversize is clamped to the picture", box(), "0,0,200,100");
        eq("all of it kept", txt("sArea"), "100%");

        /* The crop is cut from the ORIGINAL. The block at (16,8) is painted
           rgb(16,8,128); cropping exactly it must give back exactly that. */
        var before = document.querySelector("#preview img") ? document.querySelector("#preview img").src : "";
        set("cw", 8); set("ch", 8); set("cx", 16); set("cy", 8);
        waitFor("the crop is re-cut",
          function () {
            var i = document.querySelector("#preview img");
            return i && i.src !== before && i.complete && i.naturalWidth === 8;
          },
          function () {
            var i = document.querySelector("#preview img");
            var c = document.createElement("canvas");
            c.width = 8; c.height = 8;
            var ctx = c.getContext("2d");
            ctx.drawImage(i, 0, 0);
            var px = ctx.getImageData(4, 4, 1, 1).data;
            eq("the crop is really that part of the original",
               px[0] + "," + px[1] + "," + px[2], "16,8,128");
            eq("and is 8 pixels each way", i.naturalWidth + "x" + i.naturalHeight, "8x8");

            click("dlBtn");
            eq("the file is named for its size", lastSaved().name, "test-crop-8x8.png");

            set("fmt", "image/jpeg");
            waitFor("a JPG is produced",
              function () { click("dlBtn"); return lastSaved().name === "test-crop-8x8.jpg"; },
              function () {
                eq("named for the real type", lastSaved().name, "test-crop-8x8.jpg");
                eq("and it is a JPEG", lastSaved().blob.type, "image/jpeg");

                feed("file", new File(["not a picture"], "note.txt", { type: "text/plain" }));
                has("a non-image is refused", txt("msg"), "not an image");

                click("resetBtn");
                eq("reset clears the size", txt("sSize"), DASH);
                eq("disables the download", document.getElementById("dlBtn").disabled, true);
                eq("and frees the shape", val("aspect"), "free");
                  /* A PNG with see-through parts saved as JPG used to come out on a BLACK
     square: the canvas hands transparent pixels to the JPEG encoder as
     black. The tool now puts white behind the picture for JPG. */
  (function () {
    window.__saved = window.__saved || [];
    window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
    click("resetBtn");
    set("fmt", "image/jpeg");
    var c = document.createElement("canvas"); c.width = 120; c.height = 80;
    var x = c.getContext("2d"); x.fillStyle = "#ff0000"; x.fillRect(40, 20, 40, 40);
    c.toBlob(function (png) {
      var dt = new DataTransfer();
      dt.items.add(new File([png], "logo.png", { type: "image/png" }));
      var input = document.getElementById("file");
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      waitFor("a transparent PNG is taken in", function () {
        return !document.getElementById("dlBtn").disabled;
      }, function () {
        var count = window.__saved.length;
        click("dlBtn");
        waitFor("and saved as a JPG", function () { return window.__saved.length > count; }, function () {
          var saved = window.__saved[window.__saved.length - 1];
          eq("cropper: the file is a JPEG", saved.blob.type, "image/jpeg");
          createImageBitmap(saved.blob).then(function (bmp) {
            var k = document.createElement("canvas"); k.width = bmp.width; k.height = bmp.height;
            var kx = k.getContext("2d"); kx.drawImage(bmp, 0, 0);
            var d = kx.getImageData(2, 2, 1, 1).data;
            ok("cropper: a see-through corner comes out white, not black",
               d[0] > 245 && d[1] > 245 && d[2] > 245, Array.prototype.join.call(d, ","));
            var m = kx.getImageData(bmp.width >> 1, bmp.height >> 1, 1, 1).data;
            ok("cropper: and the picture itself is unchanged",
               m[0] > 220 && m[1] < 40 && m[2] < 40, Array.prototype.join.call(m, ","));
            finish();
          });
        }, 20000);
      }, 20000);
    }, "image/png");
  })();
              }, 4000);
          }, 4000);
      });
  });
"""
# ---- END: the test for image-cropper ----

# ---- START: the test for favicon-generator ----
T["favicon-generator"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  /* Walks a zip's central directory. Independent of the writer: this is the
     reader's half of the format, so an offset wrong in the same way on both
     sides cannot hide. */
  function readZip(bytes) {
    var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var eocd = bytes.length - 22;
    if (eocd < 0 || dv.getUint32(eocd, true) !== 0x06054b50) { return null; }
    var count = dv.getUint16(eocd + 10, true);
    var p = dv.getUint32(eocd + 16, true);
    var out = [];
    for (var i = 0; i < count; i++) {
      if (dv.getUint32(p, true) !== 0x02014b50) { return null; }
      var size = dv.getUint32(p + 24, true);
      var nameLen = dv.getUint16(p + 28, true);
      var extraLen = dv.getUint16(p + 30, true);
      var commentLen = dv.getUint16(p + 32, true);
      var local = dv.getUint32(p + 42, true);
      var name = new TextDecoder().decode(bytes.slice(p + 46, p + 46 + nameLen));
      var start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
      out.push({ name: name, data: bytes.slice(start, start + size) });
      p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
  }

  /* A PNG announces its own size in bytes 16 to 23, big-endian. */
  function pngSize(data) {
    var sig = [137, 80, 78, 71, 13, 10, 26, 10];
    for (var i = 0; i < 8; i++) { if (data[i] !== sig[i]) { return null; } }
    var dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
    return { w: dv.getUint32(16), h: dv.getUint32(20) };
  }

  /* The colour of one pixel of a PNG, by decoding it for real. */
  function pixelOf(data, x, y, then) {
    var img = new Image();
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      var ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      var p = ctx.getImageData(x, y, 1, 1).data;
      then([p[0], p[1], p[2], p[3]]);
    };
    img.src = URL.createObjectURL(new Blob([data], { type: "image/png" }));
  }

  function grabZip(then) {
    var seen = window.__saved.length;
    click("zipBtn");
    waitFor("the zip is handed over",
      function () { return window.__saved.length > seen; },
      function () { bytesOf(lastSaved().blob, function (bytes) { then(readZip(bytes)); }); });
  }
  function entry(files, name) {
    return files.filter(function (f) { return f.name === name; })[0];
  }

  makeImage("file", 200, 100, function () {
    waitFor("the icons are drawn",
      function () { return document.getElementById("zipBtn").disabled === false; },
      function () {
        eq("five icon cards are shown", document.querySelectorAll("#grid .stat").length, 5);
        eq("seven files in the zip", txt("sFiles"), "7");
        eq("the source size is shown", txt("sSource"), "200 " + TIMES + " 100");
        has("the snippet links the Apple icon", txt("snippet"), 'rel="apple-touch-icon"');
        has("and the .ico", txt("snippet"), 'href="/favicon.ico"');
        has("a small source is called small", txt("msg"), "smaller than 512");

        grabZip(function (files) {
          ok("the zip opens", files !== null);
          eq("named as a set", lastSaved().name, "favicons.zip");
          eq("every file is there, in order", files.map(function (f) { return f.name; }).join(" "),
             "favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png " +
             "android-chrome-192x192.png android-chrome-512x512.png head-snippet.html");

          [["favicon-16x16.png", 16], ["favicon-32x32.png", 32], ["apple-touch-icon.png", 180],
           ["android-chrome-192x192.png", 192], ["android-chrome-512x512.png", 512]].forEach(function (pair) {
            var size = pngSize(entry(files, pair[0]).data);
            ok(pair[0] + " is a real PNG of " + pair[1] + " pixels",
               size && size.w === pair[1] && size.h === pair[1],
               JSON.stringify(size));
          });

          /* The .ico: a 6-byte header, then 16 bytes per image, each pointing
             at a complete PNG. Read back the way a browser reads it. */
          var ico = entry(files, "favicon.ico").data;
          var dv = new DataView(ico.buffer, ico.byteOffset, ico.byteLength);
          eq("ico: reserved zero, type 1 (icon)", dv.getUint16(0, true) + "," + dv.getUint16(2, true), "0,1");
          eq("ico: three images", dv.getUint16(4, true), 3);
          [16, 32, 48].forEach(function (px, i) {
            var at = 6 + 16 * i;
            var off = dv.getUint32(at + 12, true), len = dv.getUint32(at + 8, true);
            var inner = pngSize(ico.slice(off, off + len));
            ok("ico image " + (i + 1) + " is declared " + px + " and is " + px,
               ico[at] === px && ico[at + 1] === px && inner && inner.w === px && inner.h === px,
               JSON.stringify(inner));
          });
          var lastEntry = 6 + 16 * 2;
          eq("ico: the last image ends exactly at the end of the file",
             dv.getUint32(lastEntry + 12, true) + dv.getUint32(lastEntry + 8, true), ico.length);

          has("the snippet file is in the zip",
              new TextDecoder().decode(entry(files, "head-snippet.html").data), "apple-touch-icon");

          /* 200x100 kept whole in a 32 pixel square leaves the top and bottom
             empty: transparent at the top, opaque in the middle. */
          pixelOf(entry(files, "favicon-32x32.png").data, 16, 2, function (top) {
            eq("kept whole: the top edge is transparent", top[3], 0);
            pixelOf(entry(files, "favicon-32x32.png").data, 16, 16, function (mid) {
              eq("and the middle is picture", mid[3], 255);

              var firstSrc = document.querySelector("#grid img").src;
              set("fit", "cover");
              waitFor("the icons are redrawn",
                function () { return document.querySelector("#grid img").src !== firstSrc; },
                function () {
                  grabZip(function (cover) {
                    pixelOf(entry(cover, "favicon-32x32.png").data, 16, 2, function (edge) {
                      eq("cropped to fill: the top edge is picture", edge[3], 255);

                      var secondSrc = document.querySelector("#grid img").src;
                      set("fit", "contain");
                      tick("useBg", true);
                      set("bg", "#ff0000");
                      waitFor("redrawn with a background",
                        function () { return document.querySelector("#grid img").src !== secondSrc; },
                        function () {
                          grabZip(function (filled) {
                            pixelOf(entry(filled, "favicon-32x32.png").data, 16, 2, function (bg) {
                              eq("a chosen background fills the empty space", bg.join(","), "255,0,0,255");

                              click("icoBtn");
                              eq("the .ico alone is offered too", lastSaved().name, "favicon.ico");

                              click("resetBtn");
                              eq("reset empties the grid", document.querySelectorAll("#grid .stat").length, 0);
                              eq("and disables the downloads", document.getElementById("zipBtn").disabled, true);
                              finish();
                            });
                          });
                        }, 8000);
                    });
                  });
                }, 8000);
            });
          });
        });
      });
  });
"""
# ---- END: the test for favicon-generator ----

# ---- START: the test for photo-watermark ----
T["photo-watermark"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  /* What changed on a canvas between two moments: how many pixels, and the
     bounding box of them. */
  function grab(id) {
    var c = document.getElementById(id);
    return c.getContext("2d").getImageData(0, 0, c.width, c.height);
  }
  function changed(before, after) {
    var w = before.width, box = { n: 0, x0: 1e9, y0: 1e9, x1: -1, y1: -1, energy: 0, black: 0, red: 0 };
    for (var i = 0; i < before.data.length; i += 4) {
      var dr = Math.abs(before.data[i] - after.data[i]);
      var dg = Math.abs(before.data[i + 1] - after.data[i + 1]);
      var db = Math.abs(before.data[i + 2] - after.data[i + 2]);
      if (dr + dg + db > 0) {
        var p = i / 4, x = p % w, y = Math.floor(p / w);
        box.n += 1;
        box.energy += dr + dg + db;
        if (x < box.x0) { box.x0 = x; } if (x > box.x1) { box.x1 = x; }
        if (y < box.y0) { box.y0 = y; } if (y > box.y1) { box.y1 = y; }
        if (after.data[i] === 0 && after.data[i + 1] === 0 && after.data[i + 2] === 0) { box.black += 1; }
        if (after.data[i] === 255 && after.data[i + 1] === 0 && after.data[i + 2] === 0) { box.red += 1; }
      }
    }
    return box;
  }

  makeImage("file", 400, 200, function () {
    waitFor("the picture is drawn",
      function () { return txt("sSize") !== DASH; },
      function () {
        eq("the picture size", txt("sSize"), "400 " + TIMES + " 200");
        eq("one mark", txt("sMarks"), "1");
        eq("twenty pixel letters at 5% of 400", txt("sFont"), "20 px");
        ok("download is available", document.getElementById("dlBtn").disabled === false);

        set("text", "");
        var base = grab("stage");
        eq("the empty mark leaves the original: first pixel", base.data[0] + "," + base.data[1] + "," + base.data[2], "0,0,128");
        eq("and none are drawn", txt("sMarks"), "0");
        has("and it says so", txt("msg"), "empty");

        set("text", "ABCD"); set("opacity", 100);
        set("size", 10);
        eq("the size label follows the slider", txt("sizeOut"), "10");
        eq("and doubles the letters", txt("sFont"), "40 px");
        set("size", 5);

        set("pos", "nw");
        var m = changed(base, grab("stage"));
        ok("top left: something was drawn", m.n > 50, m.n + " pixels");
        ok("and all of it in the top-left quarter", m.x1 < 200 && m.y1 < 100,
           "box " + m.x0 + "," + m.y0 + " to " + m.x1 + "," + m.y1);

        set("pos", "se");
        m = changed(base, grab("stage"));
        ok("bottom right: all of it in the bottom-right quarter", m.x0 >= 200 && m.y0 >= 100,
           "box " + m.x0 + "," + m.y0 + " to " + m.x1 + "," + m.y1);
        ok("and not touching the edge", m.x1 < 399 && m.y1 < 199, "box ends " + m.x1 + "," + m.y1);

        set("pos", "c");
        m = changed(base, grab("stage"));
        ok("centre: away from every edge",
           m.x0 > 100 && m.x1 < 300 && m.y0 > 60 && m.y1 < 140,
           "box " + m.x0 + "," + m.y0 + " to " + m.x1 + "," + m.y1);

        /* A mark turned a quarter-circle in a corner is tall, not wide, and
           has to be placed by that or it hangs off the picture. */
        set("pos", "se"); set("angle", 90);
        m = changed(base, grab("stage"));
        ok("turned 90 degrees it is taller than wide", (m.y1 - m.y0) > (m.x1 - m.x0),
           "box " + (m.x1 - m.x0) + " wide, " + (m.y1 - m.y0) + " tall");
        ok("and still inside the picture", m.x1 < 399 && m.y1 < 199 && m.x0 > 0 && m.y0 > 0,
           "box " + m.x0 + "," + m.y0 + " to " + m.x1 + "," + m.y1);
        set("angle", 0);

        var strong = changed(base, grab("stage")).energy;
        set("opacity", 10);
        var faint = changed(base, grab("stage")).energy;
        ok("lower opacity changes far less", faint * 3 < strong, "strong " + strong + ", faint " + faint);
        set("opacity", 100);

        set("mode", "tile");
        ok("tiled: many marks", Number(txt("sMarks")) > 5, txt("sMarks"));
        eq("and the position menu is put away", document.getElementById("posField").hidden, true);
        m = changed(base, grab("stage"));
        ok("spread over the whole picture", m.x0 < 60 && m.x1 > 340 && m.y0 < 60 && m.y1 > 140,
           "box " + m.x0 + "," + m.y0 + " to " + m.x1 + "," + m.y1);
        has("and it says tiling is harder to remove", txt("msg"), "harder to crop");
        set("mode", "single");
        eq("single brings the menu back", document.getElementById("posField").hidden, false);

        set("text", "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKL"); set("size", 40);
        has("text wider than the picture is flagged", txt("msg"), "wider than the picture");
        set("text", "ABCD"); set("size", 5);

        click("dlBtn");
        waitFor("a PNG is produced",
          function () { return window.__saved.length > 0; },
          function () {
            eq("named for the photo", lastSaved().name, "test-watermarked.png");
            eq("a PNG", lastSaved().blob.type, "image/png");

            set("fmt", "image/jpeg");
            click("dlBtn");
            waitFor("a JPG is produced",
              function () { return window.__saved.length > 1; },
              function () {
                eq("and a JPG when asked", lastSaved().name, "test-watermarked.jpg");
                eq("really a JPEG", lastSaved().blob.type, "image/jpeg");

                click("resetBtn");
                eq("reset puts the copyright sign back",
                   val("text"), String.fromCharCode(0x00A9) + " Your Name");
                eq("disables the download", document.getElementById("dlBtn").disabled, true);
                  /* A PNG with see-through parts saved as JPG used to come out on a BLACK
     square: the canvas hands transparent pixels to the JPEG encoder as
     black. The tool now puts white behind the picture for JPG. */
  (function () {
    window.__saved = window.__saved || [];
    window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
    click("resetBtn");
    set("fmt", "image/jpeg");
    var c = document.createElement("canvas"); c.width = 120; c.height = 80;
    var x = c.getContext("2d"); x.fillStyle = "#ff0000"; x.fillRect(40, 20, 40, 40);
    c.toBlob(function (png) {
      var dt = new DataTransfer();
      dt.items.add(new File([png], "logo.png", { type: "image/png" }));
      var input = document.getElementById("file");
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      waitFor("a transparent PNG is taken in", function () {
        return !document.getElementById("dlBtn").disabled;
      }, function () {
        var count = window.__saved.length;
        click("dlBtn");
        waitFor("and saved as a JPG", function () { return window.__saved.length > count; }, function () {
          var saved = window.__saved[window.__saved.length - 1];
          eq("watermark: the file is a JPEG", saved.blob.type, "image/jpeg");
          createImageBitmap(saved.blob).then(function (bmp) {
            var k = document.createElement("canvas"); k.width = bmp.width; k.height = bmp.height;
            var kx = k.getContext("2d"); kx.drawImage(bmp, 0, 0);
            var d = kx.getImageData(2, 2, 1, 1).data;
            ok("watermark: a see-through corner comes out white, not black",
               d[0] > 245 && d[1] > 245 && d[2] > 245, Array.prototype.join.call(d, ","));
            var m = kx.getImageData(bmp.width >> 1, bmp.height >> 1, 1, 1).data;
            ok("watermark: and the picture itself is unchanged",
               m[0] > 220 && m[1] < 40 && m[2] < 40, Array.prototype.join.call(m, ","));
            finish();
          });
        }, 20000);
      }, 20000);
    }, "image/png");
  })();
              });
          });
      });
  });
"""
# ---- END: the test for photo-watermark ----

# ---- START: the test for meme-generator ----
T["meme-generator"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  /* What changed on a canvas between two moments: how many pixels, and the
     bounding box of them. */
  function grab(id) {
    var c = document.getElementById(id);
    return c.getContext("2d").getImageData(0, 0, c.width, c.height);
  }
  function changed(before, after) {
    var w = before.width, box = { n: 0, x0: 1e9, y0: 1e9, x1: -1, y1: -1, energy: 0, black: 0, red: 0 };
    for (var i = 0; i < before.data.length; i += 4) {
      var dr = Math.abs(before.data[i] - after.data[i]);
      var dg = Math.abs(before.data[i + 1] - after.data[i + 1]);
      var db = Math.abs(before.data[i + 2] - after.data[i + 2]);
      if (dr + dg + db > 0) {
        var p = i / 4, x = p % w, y = Math.floor(p / w);
        box.n += 1;
        box.energy += dr + dg + db;
        if (x < box.x0) { box.x0 = x; } if (x > box.x1) { box.x1 = x; }
        if (y < box.y0) { box.y0 = y; } if (y > box.y1) { box.y1 = y; }
        if (after.data[i] === 0 && after.data[i + 1] === 0 && after.data[i + 2] === 0) { box.black += 1; }
        if (after.data[i] === 255 && after.data[i + 1] === 0 && after.data[i + 2] === 0) { box.red += 1; }
      }
    }
    return box;
  }

  makeImage("file", 400, 300, function () {
    waitFor("the picture is drawn",
      function () { return txt("sSize") !== DASH; },
      function () {
        eq("the picture size", txt("sSize"), "400 " + TIMES + " 300");
        ok("both captions were drawn", Number(txt("sTop")) >= 1 && Number(txt("sBottom")) >= 1,
           txt("sTop") + " / " + txt("sBottom"));
        ok("neither takes more than three lines", Number(txt("sTop")) <= 3 && Number(txt("sBottom")) <= 3);

        set("top", ""); set("bottom", "");
        var base = grab("stage");
        eq("with no text the picture is untouched: first pixel",
           base.data[0] + "," + base.data[1] + "," + base.data[2], "0,0,128");
        has("and it says so", txt("msg"), "empty");

        set("top", "HELLO");
        var m = changed(base, grab("stage"));
        ok("top text lands in the top half", m.n > 50 && m.y1 < 150, "box ends at y " + m.y1);
        ok("centred on the picture", m.x0 > 100 && m.x1 < 300, "box " + m.x0 + " to " + m.x1);
        ok("and there is outline: pure black pixels exist", m.black > 10, m.black + " black pixels");
        set("top", "");

        set("bottom", "HELLO");
        m = changed(base, grab("stage"));
        ok("bottom text lands in the bottom half", m.n > 50 && m.y0 > 150, "box starts at y " + m.y0);
        ok("and near the bottom edge", m.y1 > 260, "box ends at y " + m.y1);
        set("bottom", "");

        /* A long caption wraps, and never runs off either side. */
        var caption = "";
        for (var i = 0; i < 30; i++) { caption += "WORD "; }
        set("top", caption);
        var lines = Number(txt("sTop"));
        ok("a long caption wraps onto two or three lines", lines >= 2 && lines <= 3, lines + " lines");
        m = changed(base, grab("stage"));
        ok("and stays inside the picture", m.x0 > 0 && m.x1 < 399, "box " + m.x0 + " to " + m.x1);

        /* One word too wide to wrap is shrunk instead. */
        var word = "";
        for (var j = 0; j < 30; j++) { word += "W"; }
        set("top", word);
        eq("an unbreakable word stays on one line", txt("sTop"), "1");
        m = changed(base, grab("stage"));
        ok("and is shrunk to fit", m.x0 > 0 && m.x1 < 399, "box " + m.x0 + " to " + m.x1);

        set("top", "hello world");
        var withCaps = changed(base, grab("stage"));
        tick("caps", false);
        var without = changed(base, grab("stage"));
        ok("capitals change what is drawn", (withCaps.x1 - withCaps.x0) !== (without.x1 - without.x0),
           (withCaps.x1 - withCaps.x0) + " vs " + (without.x1 - without.x0));
        tick("caps", true);

        set("fill", "#ff0000");
        m = changed(base, grab("stage"));
        ok("the letter colour is used", m.red > 20, m.red + " red pixels");

        click("dlBtn");
        waitFor("a PNG is produced",
          function () { return window.__saved.length > 0; },
          function () {
            eq("named for the photo", lastSaved().name, "test-meme.png");
            click("resetBtn");
            eq("reset puts the classic top line back", val("top"), "WHEN THE CODE WORKS");
            eq("and disables the download", document.getElementById("dlBtn").disabled, true);
              /* A PNG with see-through parts saved as JPG used to come out on a BLACK
     square: the canvas hands transparent pixels to the JPEG encoder as
     black. The tool now puts white behind the picture for JPG. */
  (function () {
    window.__saved = window.__saved || [];
    window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
    click("resetBtn");
    set("fmt", "image/jpeg");
    var c = document.createElement("canvas"); c.width = 120; c.height = 80;
    var x = c.getContext("2d"); x.fillStyle = "#ff0000"; x.fillRect(40, 20, 40, 40);
    c.toBlob(function (png) {
      var dt = new DataTransfer();
      dt.items.add(new File([png], "logo.png", { type: "image/png" }));
      var input = document.getElementById("file");
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      waitFor("a transparent PNG is taken in", function () {
        return !document.getElementById("dlBtn").disabled;
      }, function () {
        var count = window.__saved.length;
        click("dlBtn");
        waitFor("and saved as a JPG", function () { return window.__saved.length > count; }, function () {
          var saved = window.__saved[window.__saved.length - 1];
          eq("meme: the file is a JPEG", saved.blob.type, "image/jpeg");
          createImageBitmap(saved.blob).then(function (bmp) {
            var k = document.createElement("canvas"); k.width = bmp.width; k.height = bmp.height;
            var kx = k.getContext("2d"); kx.drawImage(bmp, 0, 0);
            var d = kx.getImageData(2, 2, 1, 1).data;
            ok("meme: a see-through corner comes out white, not black",
               d[0] > 245 && d[1] > 245 && d[2] > 245, Array.prototype.join.call(d, ","));
            var m = kx.getImageData(bmp.width >> 1, bmp.height >> 1, 1, 1).data;
            ok("meme: and the picture itself is unchanged",
               m[0] > 220 && m[1] < 40 && m[2] < 40, Array.prototype.join.call(m, ","));
            finish();
          });
        }, 20000);
      }, 20000);
    }, "image/png");
  })();
          });
      });
  });
"""
# ---- END: the test for meme-generator ----

# ---- START: the test for image-splitter ----
T["image-splitter"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  /* Walks a zip's central directory. Independent of the writer: this is the
     reader's half of the format, so an offset wrong in the same way on both
     sides cannot hide. */
  function readZip(bytes) {
    var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var eocd = bytes.length - 22;
    if (eocd < 0 || dv.getUint32(eocd, true) !== 0x06054b50) { return null; }
    var count = dv.getUint16(eocd + 10, true);
    var p = dv.getUint32(eocd + 16, true);
    var out = [];
    for (var i = 0; i < count; i++) {
      if (dv.getUint32(p, true) !== 0x02014b50) { return null; }
      var size = dv.getUint32(p + 24, true);
      var nameLen = dv.getUint16(p + 28, true);
      var extraLen = dv.getUint16(p + 30, true);
      var commentLen = dv.getUint16(p + 32, true);
      var local = dv.getUint32(p + 42, true);
      var name = new TextDecoder().decode(bytes.slice(p + 46, p + 46 + nameLen));
      var start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
      out.push({ name: name, data: bytes.slice(start, start + size) });
      p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
  }

  /* A PNG announces its own size in bytes 16 to 23, big-endian. */
  function pngSize(data) {
    var sig = [137, 80, 78, 71, 13, 10, 26, 10];
    for (var i = 0; i < 8; i++) { if (data[i] !== sig[i]) { return null; } }
    var dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
    return { w: dv.getUint32(16), h: dv.getUint32(20) };
  }

  /* The colour of one pixel of a PNG, by decoding it for real. */
  function pixelOf(data, x, y, then) {
    var img = new Image();
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      var ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      var p = ctx.getImageData(x, y, 1, 1).data;
      then([p[0], p[1], p[2], p[3]]);
    };
    img.src = URL.createObjectURL(new Blob([data], { type: "image/png" }));
  }

  function grabZip(then) {
    var seen = window.__saved.length;
    click("zipBtn");
    waitFor("the zip is handed over",
      function () { return window.__saved.length > seen; },
      function () { bytesOf(lastSaved().blob, function (bytes) { then(readZip(bytes)); }); });
  }

  makeImage("file", 200, 100, function () {
    waitFor("the plan is drawn",
      function () { return txt("sTiles") !== DASH; },
      function () {
        /* 200 across three: the cuts fall at 0, 67, 133, 200, so 67, 66, 67. */
        eq("three across by default", txt("sTiles"), "3");
        eq("the first tile is 67 wide", txt("sEach"), "67 " + TIMES + " 100");
        has("an uneven split says nothing was trimmed", txt("msg"), "single pixel");
        eq("each tile has its own button", document.querySelectorAll("#tileList button").length, 3);

        set("cols", 2);
        eq("two tiles", txt("sTiles"), "2");
        eq("an even split", txt("sEach"), "100 " + TIMES + " 100");
        has("said plainly", txt("msg"), "every pixel of the original");

        set("rows", 2);
        eq("four tiles", txt("sTiles"), "4");
        eq("each 100 by 50", txt("sEach"), "100 " + TIMES + " 50");

        document.querySelector('[data-grid="3x3"]').click();
        eq("the quick layout sets columns", val("cols"), "3");
        eq("and rows", val("rows"), "3");
        eq("nine tiles", txt("sTiles"), "9");

        set("cols", 99);
        eq("more than ten is clamped", val("cols"), "10");

        /* Two across, and the content of the tiles checked pixel by pixel. */
        set("cols", 2); set("rows", 1);
        grabZip(function (files) {
          eq("two files in the zip", files.length, 2);
          eq("named by position", files.map(function (f) { return f.name; }).join(" "),
             "test-r1c1.png test-r1c2.png");
          var a = pngSize(files[0].data), b = pngSize(files[1].data);
          eq("together they are exactly as wide as the picture", a.w + b.w, 200);
          eq("and as tall", a.h, 100);

          /* The right tile begins at x=100, in the block that starts at x=96. */
          pixelOf(files[1].data, 0, 0, function (px) {
            eq("the right tile starts where the original does: rgb(96,0,128)",
               px[0] + "," + px[1] + "," + px[2], "96,0,128");

            /* Three by three, to prove coverage and posting order. */
            set("cols", 3); set("rows", 3);
            set("naming", "order");
            grabZip(function (nine) {
              eq("nine files", nine.length, 9);
              eq("posting order: the first tile in reading order is posted LAST",
                 nine[0].name, "test-09.png");
              eq("and the final one is posted first", nine[8].name, "test-01.png");

              var width = 0, height = 0;
              [0, 1, 2].forEach(function (c) { width += pngSize(nine[c].data).w; });
              [0, 3, 6].forEach(function (r) { height += pngSize(nine[r].data).h; });
              eq("a row of tiles covers the full width", width, 200);
              eq("a column covers the full height", height, 100);

              set("naming", "position");
              set("fmt", "image/jpeg");
              document.querySelectorAll("#tileList button")[0].click();
              waitFor("a single tile is saved",
                function () { return window.__saved.length > 2; },
                function () {
                  eq("as a JPG when asked", lastSaved().name, "test-r1c1.jpg");

                  click("resetBtn");
                  eq("reset clears the tiles", txt("sTiles"), DASH);
                  eq("and disables the zip", document.getElementById("zipBtn").disabled, true);
                  finish();
                });
            });
          });
        });
      });
  });
"""
# ---- END: the test for image-splitter ----

# ---- START: the test for image-metadata-viewer ----
T["image-metadata-viewer"] = r"""
  var TIMES = String.fromCharCode(0x00D7);
  var DASH = String.fromCharCode(0x2014);

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  function lastSaved() { return window.__saved[window.__saved.length - 1]; }

  /* Hand a File to a file input exactly as choosing one would. */
  function feed(id, file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById(id);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bytesOf(blob, then) {
    blob.arrayBuffer().then(function (buffer) { then(new Uint8Array(buffer)); });
  }

  /* Walks a zip's central directory. Independent of the writer: this is the
     reader's half of the format, so an offset wrong in the same way on both
     sides cannot hide. */
  function readZip(bytes) {
    var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var eocd = bytes.length - 22;
    if (eocd < 0 || dv.getUint32(eocd, true) !== 0x06054b50) { return null; }
    var count = dv.getUint16(eocd + 10, true);
    var p = dv.getUint32(eocd + 16, true);
    var out = [];
    for (var i = 0; i < count; i++) {
      if (dv.getUint32(p, true) !== 0x02014b50) { return null; }
      var size = dv.getUint32(p + 24, true);
      var nameLen = dv.getUint16(p + 28, true);
      var extraLen = dv.getUint16(p + 30, true);
      var commentLen = dv.getUint16(p + 32, true);
      var local = dv.getUint32(p + 42, true);
      var name = new TextDecoder().decode(bytes.slice(p + 46, p + 46 + nameLen));
      var start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
      out.push({ name: name, data: bytes.slice(start, start + size) });
      p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
  }

  /* A PNG announces its own size in bytes 16 to 23, big-endian. */
  function pngSize(data) {
    var sig = [137, 80, 78, 71, 13, 10, 26, 10];
    for (var i = 0; i < 8; i++) { if (data[i] !== sig[i]) { return null; } }
    var dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
    return { w: dv.getUint32(16), h: dv.getUint32(20) };
  }

  /* The colour of one pixel of a PNG, by decoding it for real. */
  function pixelOf(data, x, y, then) {
    var img = new Image();
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      var ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      var p = ctx.getImageData(x, y, 1, 1).data;
      then([p[0], p[1], p[2], p[3]]);
    };
    img.src = URL.createObjectURL(new Blob([data], { type: "image/png" }));
  }
/* Builds EXIF blocks by hand, for testing the reader against.

   Shared by the node probe and by the browser test (the test body is
   assembled from this file), so both are exercising the reader with the same
   bytes. Written independently of the reader on purpose: it shares no code
   with it, so a mistake in one cannot be repeated in the other.

   An entry is { tag, type, data } where data is a list of raw bytes already in
   the file's byte order, or { tag, ptr: "exif" | "gps" } for the two pointers
   into sub-directories.

   TIFF layout: 8-byte header, IFD0, Exif IFD, GPS IFD, then a data area that
   holds every value too big for the four bytes inside an entry. */
function exifBuilder(be) {
  function u16(v) { return be ? [(v >> 8) & 255, v & 255] : [v & 255, (v >> 8) & 255]; }
  function u32(v) {
    var b = [(v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255];
    return be ? b : b.reverse();
  }
  function ascii(s) {
    var out = [];
    for (var i = 0; i < s.length; i++) { out.push(s.charCodeAt(i)); }
    out.push(0);
    return out;
  }
  function rational(n, d) { return u32(n).concat(u32(d)); }

  var api = {
    ascii: function (tag, s) { var d = ascii(s); return { tag: tag, type: 2, count: d.length, data: d }; },
    short: function (tag, v) { return { tag: tag, type: 3, count: 1, data: u16(v) }; },
    byte: function (tag, v) { return { tag: tag, type: 1, count: 1, data: [v] }; },
    rational: function (tag, n, d) { return { tag: tag, type: 5, count: 1, data: rational(n, d) }; },
    rationals: function (tag, list) {
      var d = [];
      list.forEach(function (p) { d = d.concat(rational(p[0], p[1])); });
      return { tag: tag, type: 5, count: list.length, data: d };
    },
    undefinedBytes: function (tag, bytes) { return { tag: tag, type: 7, count: bytes.length, data: bytes }; },
    ptr: function (tag, which) { return { tag: tag, ptr: which }; }
  };

  api.build = function (spec) {
    var sets = [
      { key: "ifd0", list: (spec.ifd0 || []).slice() },
      { key: "exif", list: (spec.exif || []).slice() },
      { key: "gps",  list: (spec.gps  || []).slice() }
    ];
    /* An empty directory that nothing points at is simply left out. */
    sets = sets.filter(function (s) { return s.key === "ifd0" || s.list.length; });

    var size = function (s) { return 2 + 12 * s.list.length + 4; };
    var at = 8;
    var offsets = {};
    sets.forEach(function (s) { offsets[s.key] = at; at += size(s); });
    var dataStart = at;

    var blob = [];
    var out = (be ? [0x4D, 0x4D] : [0x49, 0x49]).concat(u16(42), u32(8));

    sets.forEach(function (s) {
      out = out.concat(u16(s.list.length));
      s.list.forEach(function (e) {
        var entry = u16(e.tag);
        if (e.ptr) {
          entry = entry.concat(u16(4), u32(1), u32(offsets[e.ptr] || 0));
        } else if (e.data.length <= 4) {
          var inline = e.data.slice();
          while (inline.length < 4) { inline.push(0); }
          entry = entry.concat(u16(e.type), u32(e.count), inline);
        } else {
          entry = entry.concat(u16(e.type), u32(e.count), u32(dataStart + blob.length));
          blob = blob.concat(e.data);
          if (blob.length % 2) { blob.push(0); }
        }
        out = out.concat(entry);
      });
      out = out.concat(u32(0));
    });
    return new Uint8Array(out.concat(blob));
  };
  return api;
}

/* The APP1 segment that carries a TIFF block inside a JPEG. */
function jpegWithExif(tiff, extraSegments) {
  var body = [69, 120, 105, 102, 0, 0].concat(Array.prototype.slice.call(tiff));
  var len = body.length + 2;
  var out = [0xFF, 0xD8, 0xFF, 0xE1, (len >> 8) & 255, len & 255].concat(body);
  (extraSegments || []).forEach(function (seg) { out = out.concat(seg); });
  return new Uint8Array(out.concat([0xFF, 0xD9]));
}

/* The same fields every time, so every test asks about the same photograph. */
function samplePhoto(be, overrides) {
  var b = exifBuilder(be);
  var o = overrides || {};
  var gps = o.noGps ? [] : [
    b.ascii(1, o.latRef || "N"),
    b.rationals(2, o.lat || [[17, 1], [23, 1], [6, 1]]),
    b.ascii(3, o.lonRef || "E"),
    b.rationals(4, o.lon || [[78, 1], [29, 1], [12, 1]]),
    b.byte(5, 0),
    b.rational(6, 542, 1)
  ];
  return b.build({
    ifd0: [
      b.ascii(0x010F, o.make || "Canon"),
      b.ascii(0x0110, "EOS 5D"),
      b.short(0x0112, 6),
      b.ascii(0x0131, "Editor 1.0"),
      b.ascii(0x0132, "2026:09:13 09:00:00"),
      b.ptr(0x8769, "exif")
    ].concat(gps.length ? [b.ptr(0x8825, "gps")] : []),
    exif: [
      b.rational(0x829A, 1, 250),
      b.rational(0x829D, 28, 10),
      b.short(0x8827, 400),
      b.ascii(0x9003, "2026:09:12 14:30:22"),
      b.rational(0x920A, 50, 1),
      b.short(0xA405, 75),
      b.ascii(0xA434, "EF 50mm f/1.8"),
      b.ascii(0xA431, "0123456789")
    ],
    gps: gps
  });
}

  function rowsText() {
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll("#rows tr"), function (tr) {
      var tds = tr.querySelectorAll("td");
      if (tds.length === 2) { out.push(tds[0].textContent + ": " + tds[1].textContent); }
    });
    return out;
  }
  function has1(prefix) {
    return rowsText().some(function (r) { return r.indexOf(prefix) === 0; });
  }

  /* A real JPEG from a canvas, with an EXIF block inserted after the marker
     that opens the file - which is where a camera would put it. */
  function insertExif(jpeg, tiff) {
    var body = [69, 120, 105, 102, 0, 0].concat(Array.prototype.slice.call(tiff));
    var len = body.length + 2;
    var seg = [0xFF, 0xE1, (len >> 8) & 255, len & 255].concat(body);
    var out = new Uint8Array(jpeg.length + seg.length);
    out.set(jpeg.slice(0, 2), 0);
    out.set(seg, 2);
    out.set(jpeg.slice(2), 2 + seg.length);
    return out;
  }

  function makeJpeg(overrides, then) {
    var c = document.createElement("canvas");
    c.width = 64; c.height = 48;
    var ctx = c.getContext("2d");
    ctx.fillStyle = "#336699"; ctx.fillRect(0, 0, 64, 48);
    ctx.fillStyle = "#ffcc00"; ctx.fillRect(10, 10, 30, 20);
    c.toBlob(function (blob) {
      bytesOf(blob, function (jpeg) {
        then(new File([insertExif(jpeg, samplePhoto(false, overrides))], "photo.jpg", { type: "image/jpeg" }));
      });
    }, "image/jpeg", 0.9);
  }

  makeJpeg({}, function (photo) {
    feed("file", photo);
    waitFor("the details are read",
      function () { return rowsText().length > 8; },
      function () {
        eq("the format", txt("sFormat"), "JPEG");
        ok("the file size is shown", txt("sSize") !== DASH, txt("sSize"));

        ok("make", has1("Make: Canon"), rowsText().join(" | "));
        ok("model", has1("Model: EOS 5D"));
        ok("shutter, worked out from 1/250", has1("Shutter: 1/250 s"));
        ok("aperture", has1("Aperture: f/2.8"));
        ok("ISO", has1("ISO: 400"));
        ok("focal length with its equivalent", has1("Focal length: 50 mm (75 mm equivalent)"));
        ok("the serial number, which can identify a camera", has1("Serial number: 0123456789"));
        ok("the date is tidied", has1("Taken: 2026-09-12 14:30:22"));
        ok("the orientation in words", has1("Orientation: Stored sideways"));
        ok("the latitude, hand-worked from 17 23 6", has1("Latitude: 17.385000"));
        ok("and the longitude", has1("Longitude: 78.486667"));

        eq("the location alert is shown", document.getElementById("alert").hidden, false);
        has("and names the coordinates", txt("alert"), "17.385000, 78.486667");
        has("and warns", txt("alert"), "Anyone you send this file to");
        eq("there is deliberately no map link", document.querySelectorAll("#alert a, #rows a").length, 0);
        has("the message says to clean it", txt("msg"), "clean copy");

        waitFor("the picture is decoded",
          function () { return txt("sDims") !== DASH; },
          function () {
            eq("its pixel size is as SHOWN - the browser applies the orientation tag", txt("sDims"), "48 " + TIMES + " 64");
            ok("and the table says what is STORED", has1("Size as stored: 64 " + TIMES + " 48"), rowsText().join(" | "));
            eq("the clean-copy button is available", document.getElementById("cleanBtn").disabled, false);

            click("cleanBtn");
            waitFor("the clean copy is produced",
              function () { return window.__saved.length > 0; },
              function () {
                eq("named for the photo", lastSaved().name, "photo-clean.jpg");
                eq("still a JPEG", lastSaved().blob.type, "image/jpeg");
                bytesOf(lastSaved().blob, function (clean) {
                  eq("it begins as a JPEG must", clean[0] + "," + clean[1], "255,216");
                  var ascii = "";
                  for (var i = 0; i < clean.length; i++) { ascii += String.fromCharCode(clean[i]); }
                  eq("it holds no EXIF block", ascii.indexOf("Exif"), -1);
                  eq("nor the camera's name", ascii.indexOf("Canon"), -1);
                  eq("nor the serial number", ascii.indexOf("0123456789"), -1);

                  /* Read it back with the same tool: what it writes, it must
                     read as clean. */
                  feed("file", new File([clean], "photo-clean.jpg", { type: "image/jpeg" }));
                  waitFor("the clean copy is read",
                    function () { return !has1("Make: Canon"); },
                    function () {
                      ok("no camera details remain", !has1("Make:") && !has1("Model:") && !has1("Serial number"));
                      ok("and no location", !has1("Latitude") && !has1("Longitude"));
                      eq("the location alert goes away", document.getElementById("alert").hidden, true);

                      /* Hostile text in a camera field stays text. */
                      makeJpeg({ make: "<iframe onload=zq>" }, function (evil) {
                        feed("file", evil);
                        waitFor("the hostile file is read",
                          function () { return has1("Make: <iframe"); },
                          function () {
                            eq("a tag in a camera name never becomes an element",
                               document.querySelectorAll("#rows iframe").length, 0);
                            ok("it is shown as characters", has1("Make: <iframe onload=zq>"));

                            feed("file", new File(["hello"], "note.txt", { type: "text/plain" }));
                            waitFor("a text file is examined",
                              function () { return txt("msg").indexOf("cannot be read here") > -1; },
                              function () {
                                eq("its type is reported instead", txt("sFormat"), "text/plain");

                                makeImage("file", 40, 30, function () {
                                  waitFor("a PNG is read",
                                    function () { return txt("sFormat") === "PNG"; },
                                    function () {
                                      has("no EXIF is explained rather than shown as an error", txt("msg"), "No EXIF block");

                                      click("resetBtn");
                                      eq("reset empties the table", document.querySelectorAll("#rows tr").length, 0);
                                      eq("and disables the clean copy", document.getElementById("cleanBtn").disabled, true);
                                      finish();
                                    });
                                });
                              });
                          });
                      });
                    });
                });
              });
          });
      });
  });
"""
# ---- END: the test for image-metadata-viewer ----

# ---- START: the test for barcode-generator ----
T["barcode-generator"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TIMES = String.fromCharCode(0x00D7);
  var stage = document.getElementById("stage");
  var pngBtn = document.getElementById("pngBtn");
  var svgBtn = document.getElementById("svgBtn");

  /* Record downloads instead of performing them. */
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ---- decoders written for this test; they share no table with the page ---- */
  var C128_BITS = ["11011001100", "11001101100", "11001100110", "10010011000", "10010001100", "10001001100", "10011001000", "10011000100", "10001100100", "11001001000", "11001000100", "11000100100", "10110011100", "10011011100", "10011001110", "10111001100", "10011101100", "10011100110", "11001110010", "11001011100", "11001001110", "11011100100", "11001110100", "11101101110", "11101001100", "11100101100", "11100100110", "11101100100", "11100110100", "11100110010", "11011011000", "11011000110", "11000110110", "10100011000", "10001011000", "10001000110", "10110001000", "10001101000", "10001100010", "11010001000", "11000101000", "11000100010", "10110111000", "10110001110", "10001101110", "10111011000", "10111000110", "10001110110", "11101110110", "11010001110", "11000101110", "11011101000", "11011100010", "11011101110", "11101011000", "11101000110", "11100010110", "11101101000", "11101100010", "11100011010", "11101111010", "11001000010", "11110001010", "10100110000", "10100001100", "10010110000", "10010000110", "10000101100", "10000100110", "10110010000", "10110000100", "10011010000", "10011000010", "10000110100", "10000110010", "11000010010", "11001010000", "11110111010", "11000010100", "10001111010", "10100111100", "10010111100", "10010011110", "10111100100", "10011110100", "10011110010", "11110100100", "11110010100", "11110010010", "11011011110", "11011110110", "11110110110", "10101111000", "10100011110", "10001011110", "10111101000", "10111100010", "11110101000", "11110100010", "10111011110", "10111101110", "11101011110", "11110101110", "11010000100", "11010010000", "11010011100"];
  var C128_STOP = "1100011101011";
  var L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"], G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"], R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"], PAR = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];
  var C39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
    "$": "nwnwnwnnn", "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn"
  };
  var ITF = ["nnwwn", "wnnnw", "nwnnw", "wwnnn", "nnwnw", "wnwnn", "nwwnn", "nnnww", "wnnwn", "nwnwn"];

  function runsOf(bits) {
    var out = [], i = 0;
    while (i < bits.length) {
      var j = i;
      while (j < bits.length && bits.charAt(j) === bits.charAt(i)) { j++; }
      out.push({ dark: bits.charAt(i) === "1", n: j - i });
      i = j;
    }
    return out;
  }
  function narrowWide(bits) {
    var elems = "";
    var runs = runsOf(bits);
    for (var i = 0; i < runs.length; i++) {
      if (runs[i].n !== 1 && runs[i].n !== 3) { return { error: "an element " + runs[i].n + " modules wide" }; }
      elems += runs[i].n === 1 ? "n" : "w";
    }
    return { elems: elems };
  }

  function decode128(bits) {
    if (bits.slice(-13) !== C128_STOP) { return { error: "no stop pattern" }; }
    var body = bits.slice(0, -13);
    if (body.length % 11) { return { error: "body is not whole symbols" }; }
    var values = [];
    for (var i = 0; i < body.length; i += 11) {
      var v = C128_BITS.indexOf(body.substr(i, 11));
      if (v < 0) { return { error: "unknown symbol number " + (i / 11) }; }
      values.push(v);
    }
    var start = values[0], check = values[values.length - 1], data = values.slice(1, -1);
    if (start < 103 || start > 105) { return { error: "bad start symbol " + start }; }
    var sum = start;
    data.forEach(function (x, k) { sum += (k + 1) * x; });
    if (sum % 103 !== check) { return { error: "checksum is " + check + ", should be " + (sum % 103) }; }
    var set = "ABC".charAt(start - 103), text = "";
    for (var k = 0; k < data.length; k++) {
      var x = data[k];
      if (x >= 103) { return { error: "a start symbol in the middle" }; }
      if (x === 99 && set !== "C") { set = "C"; continue; }
      if (x === 100 && set !== "B") { set = "B"; continue; }
      if (x === 101 && set !== "A") { set = "A"; continue; }
      if (set === "C") {
        if (x > 99) { return { error: "value " + x + " in set C" }; }
        text += (x < 10 ? "0" : "") + x;
      } else if (x > 95) {
        return { error: "function symbol " + x + " in set " + set };
      } else if (set === "B") {
        text += String.fromCharCode(x + 32);
      } else {
        text += String.fromCharCode(x < 64 ? x + 32 : x - 64);
      }
    }
    return { text: text };
  }

  function decode39(bits) {
    var nw = narrowWide(bits);
    if (nw.error) { return nw; }
    var e = nw.elems, text = "", i = 0;
    while (i < e.length) {
      var chunk = e.substr(i, 9), found = null;
      Object.keys(C39).forEach(function (ch) { if (C39[ch] === chunk) { found = ch; } });
      if (found === null) { return { error: "unknown character " + chunk }; }
      text += found;
      i += 9;
      if (i < e.length) {
        if (e.charAt(i) !== "n") { return { error: "gap is not narrow" }; }
        i += 1;
      }
    }
    if (text.length < 2 || text.charAt(0) !== "*" || text.charAt(text.length - 1) !== "*") {
      return { error: "no start and stop marks" };
    }
    return { text: text.slice(1, -1) };
  }

  function decodeItf(bits) {
    var nw = narrowWide(bits);
    if (nw.error) { return nw; }
    var e = nw.elems;
    if (e.slice(0, 4) !== "nnnn") { return { error: "no start" }; }
    if (e.slice(-3) !== "wnn") { return { error: "no stop" }; }
    var body = e.slice(4, -3), text = "";
    if (body.length % 10) { return { error: "body is not whole pairs" }; }
    for (var i = 0; i < body.length; i += 10) {
      var bars = "", spaces = "";
      for (var k = 0; k < 10; k += 2) { bars += body.charAt(i + k); spaces += body.charAt(i + k + 1); }
      var a = ITF.indexOf(bars), b = ITF.indexOf(spaces);
      if (a < 0 || b < 0) { return { error: "unknown digit" }; }
      text += String(a) + String(b);
    }
    return { text: text };
  }

  function eanOk(digits) {
    var s = 0, n = digits.length - 1;
    for (var i = 0; i < n; i++) { s += Number(digits.charAt(i)) * (((n - 1 - i) % 2 === 0) ? 3 : 1); }
    return (10 - s % 10) % 10 === Number(digits.charAt(n));
  }
  function decodeEan(bits) {
    var digits = "", i;
    if (bits.length === 67) {
      if (bits.slice(0, 3) !== "101" || bits.slice(31, 36) !== "01010" || bits.slice(64) !== "101") {
        return { error: "guards" };
      }
      for (i = 0; i < 4; i++) {
        var l8 = L.indexOf(bits.substr(3 + 7 * i, 7));
        if (l8 < 0) { return { error: "left digit " + i }; }
        digits += l8;
      }
      for (i = 0; i < 4; i++) {
        var r8 = R.indexOf(bits.substr(36 + 7 * i, 7));
        if (r8 < 0) { return { error: "right digit " + i }; }
        digits += r8;
      }
    } else if (bits.length === 95) {
      if (bits.slice(0, 3) !== "101" || bits.slice(45, 50) !== "01010" || bits.slice(92) !== "101") {
        return { error: "guards" };
      }
      var pattern = "", left = "";
      for (i = 0; i < 6; i++) {
        var cell = bits.substr(3 + 7 * i, 7);
        if (L.indexOf(cell) > -1) { pattern += "L"; left += L.indexOf(cell); }
        else if (G.indexOf(cell) > -1) { pattern += "G"; left += G.indexOf(cell); }
        else { return { error: "left digit " + i }; }
      }
      var first = PAR.indexOf(pattern);
      if (first < 0) { return { error: "parity pattern " + pattern }; }
      var right = "";
      for (i = 0; i < 6; i++) {
        var r13 = R.indexOf(bits.substr(50 + 7 * i, 7));
        if (r13 < 0) { return { error: "right digit " + i }; }
        right += r13;
      }
      digits = String(first) + left + right;
    } else {
      return { error: "a symbol " + bits.length + " modules wide" };
    }
    return eanOk(digits) ? { text: digits } : { error: "the check digit is wrong in " + digits };
  }

  /* ---- reading the canvas the way a scanner does ---- */
  function scan(scale, quietLeft, quietRight) {
    var w = stage.width, h = stage.height;
    var data = stage.getContext("2d").getImageData(0, 0, w, h).data;
    function dark(x, y) { return data[(y * w + x) * 4] < 128; }
    var row = -1;
    for (var y = 0; y < h && row < 0; y++) {
      for (var x = 0; x < w; x++) { if (dark(x, y)) { row = y + 3; break; } }
    }
    var modules = w / scale, all = "", clean = true;
    for (var m = 0; m < modules; m++) {
      var first = dark(m * scale, row);
      all += first ? "1" : "0";
      for (var k = 1; k < scale; k++) { if (dark(m * scale + k, row) !== first) { clean = false; } }
    }
    return { all: all, bits: all.slice(quietLeft, modules - quietRight), modules: modules, clean: clean };
  }
  /* How far a bar reaches down from the top of the drawing, in pixels. */
  function barLength(x) {
    var w = stage.width, h = stage.height;
    var data = stage.getContext("2d").getImageData(0, 0, w, h).data;
    var y = 0;
    while (y < h && data[(y * w + x) * 4] >= 128) { y++; }
    var n = 0;
    while (y + n < h && data[((y + n) * w + x) * 4] < 128) { n++; }
    return n;
  }

  function pick(fmt, text) { set("format", fmt); set("text", text); }
  function drawn() { return stage.style.display !== "none"; }
  function trip(label, fmt, text, want, decoder, ql, qr) {
    pick(fmt, text);
    if (!drawn()) { ok(label + " is drawn", false, txt("msg")); return; }
    var got = decoder(scan(3, ql, qr).bits);
    eq(label, got.error ? "DECODE FAILED: " + got.error : got.text, want);
  }
  function ean13(label, text, want) { trip(label, "ean13", text, want, decodeEan, 11, 7); }
  function code128(label, text) { trip(label, "code128", text, text, decode128, 10, 10); }

  /* ================= the page as it opens ================= */
  ok("a barcode is on the screen when the page opens", drawn());
  eq("the type is Code 128", val("format"), "code128");
  eq("saying 108TOOLBOX", val("text"), "108TOOLBOX");
  var first = scan(3, 10, 10);
  eq("and a scanner reads it back", decode128(first.bits).text, "108TOOLBOX");
  ok("with every bar a whole number of modules", first.clean);
  ok("and a clear margin on both sides", first.all.slice(0, 10) === "0000000000" && first.all.slice(-10) === "0000000000");
  eq("the page counts the modules the pixels show", txt("sMod"), String(first.bits.length));
  eq("the image is that many modules plus the margins, at 3 px each", stage.width, (first.bits.length + 20) * 3);
  eq("and the size tile says so", txt("sSize"), stage.width + " " + TIMES + " " + stage.height);
  has("and it says it was drawn here", txt("msg"), "Drawn in your browser");
  ok("both download buttons are ready", !pngBtn.disabled && !svgBtn.disabled);
  ok("a check symbol is shown", /^[0-9]+$/.test(txt("sCheck")), txt("sCheck"));

  /* ================= Code 128 ================= */
  eq("digits only use set C: start, four pairs, check, stop is 79 modules", (pick("code128", "12345678"), txt("sMod")), "79");
  eq("and read back", decode128(scan(3, 10, 10).bits).text, "12345678");
  eq("letters use set B: start, five letters, check, stop is 90 modules", (pick("code128", "Hello"), txt("sMod")), "90");
  eq("a run of digits inside letters switches sets and back again", (pick("code128", "ab12345678cd"), txt("sMod")), "145");
  eq("and read back", decode128(scan(3, 10, 10).bits).text, "ab12345678cd");
  eq("a control character forces set A", (pick("code128", "A\tB"), txt("sMod")), "68");
  eq("and read back", decode128(scan(3, 10, 10).bits).text, "A\tB");
  code128("lowercase and a control character together", "a\tb");
  code128("one character", "a");
  code128("a single digit", "7");
  code128("two digits", "42");
  code128("three digits, an odd run", "123");
  code128("a space", " ");
  code128("a run of zeros", "0000000000");
  code128("the tilde and DEL", "~" + String.fromCharCode(127) + "~");
  code128("punctuation that XML and HTML care about", "<&>\"'");
  code128("an identifier with slashes and hashes", "ID-2026-09-29/SHIP#4471");
  var printable = "", low = "", c;
  for (c = 32; c < 96; c++) { printable += String.fromCharCode(c); }
  code128("every character from space to underscore", printable);
  printable = "";
  for (c = 96; c < 127; c++) { printable += String.fromCharCode(c); }
  code128("every character from the backtick to the tilde", printable);
  for (c = 0; c < 32; c++) { if (c !== 10 && c !== 13) { low += String.fromCharCode(c); } }
  code128("every control character a text box can hold", "x" + low);
  code128("digits and letters alternating", "1a2b3c4d5e6f");
  code128("digit runs of every parity between letters", "1a12a123a1234a12345a123456a1234567");

  /* ================= EAN-13 ================= */
  pick("ean13", "590123412345");
  eq("the check digit of 590123412345 is 7 - a published example", txt("sCheck"), "7");
  has("and the message says it was worked out", txt("msg"), "Check digit 7");
  eq("the symbol is 95 modules wide", txt("sMod"), "95");
  eq("its bars match a symbol built from literal tables", scan(3, 11, 7).bits, "10100010110100111011001100100110111101001110101010110011011011001000010101110010011101000100101");
  eq("and decode to the whole number", decodeEan(scan(3, 11, 7).bits).text, "5901234123457");
  ean13("a published EAN-13 whose check digit is 1", "400638133393", "4006381333931");
  ean13("an ISBN-13, typed with hyphens", "978-3-16-148410-0", "9783161484100");
  ean13("digits pasted with spaces", "590 123 412345 7", "5901234123457");
  ean13("all zeros", "000000000000", "0000000000000");
  ean13("all nines", "999999999999", "9999999999994");
  var d, payload, cd, s, i;
  function checkOf(p) {
    var t = 0;
    for (var k = 0; k < p.length; k++) { t += Number(p.charAt(k)) * (((p.length - 1 - k) % 2 === 0) ? 3 : 1); }
    return String((10 - t % 10) % 10);
  }
  for (d = 0; d < 10; d++) {
    payload = String(d) + "12345678901";
    ean13("a number starting with " + d + " (its own parity pattern)", payload, payload + checkOf(payload));
  }

  pick("ean13", "5901234123457");
  has("typing the check digit too is accepted", txt("msg"), "check digit is right");
  pick("ean13", "5901234123458");
  ok("a wrong check digit draws nothing", !drawn());
  has("and names the right one", txt("msg"), "should be 7");
  ok("and switches both downloads off", pngBtn.disabled && svgBtn.disabled);
  eq("and blanks the tiles", txt("sMod") + txt("sCheck") + txt("sSize"), DASH + DASH + DASH);
  pick("ean13", "59012341234x");
  has("a letter is named", txt("msg"), "Digits only");
  pick("ean13", "5901234");
  has("too few digits says how many are needed", txt("msg"), "needs 12 digits");
  ok("and draws nothing", !drawn());
  pick("ean13", "5901234123457");
  ok("fixing it brings the barcode back", drawn());
  ok("and the downloads", !pngBtn.disabled && !svgBtn.disabled);

  /* Guard bars are taller than the rest, and only when there are digits to hang beside. */
  var normalBar = barLength((11 + 3 + 3) * 3 + 1);   /* a bar in the first digit */
  var guardBar = barLength(11 * 3 + 1);              /* the start guard */
  var middleBar = barLength((11 + 46) * 3 + 1);      /* inside the centre guard */
  eq("an ordinary bar is as tall as asked", normalBar, 100);
  ok("the start guard hangs lower", guardBar > normalBar + 5, guardBar + " vs " + normalBar);
  ok("and so does the centre guard", middleBar > normalBar + 5, middleBar + " vs " + normalBar);
  tick("showText", false);
  eq("with the text off the guard is an ordinary bar again", barLength(11 * 3 + 1), 100);
  eq("and the bars did not change", scan(3, 11, 7).bits, "10100010110100111011001100100110111101001110101010110011011011001000010101110010011101000100101");
  tick("showText", true);

  /* ================= UPC-A ================= */
  pick("upca", "03600029145");
  eq("the check digit of 03600029145 is 2 - a published example", txt("sCheck"), "2");
  eq("the symbol is 95 modules wide", txt("sMod"), "95");
  eq("its bars match a symbol built from literal tables", scan(3, 9, 9).bits, "10100011010111101010111100011010001101000110101010110110011101001100110101110010011101101100101");
  eq("and read as an EAN-13 with a 0 in front", decodeEan(scan(3, 9, 9).bits).text, "0036000291452");
  pick("upca", "04210000526");
  eq("a second published UPC-A ends in 4", txt("sCheck"), "4");
  eq("and is read back", decodeEan(scan(3, 9, 9).bits).text, "0042100005264");
  pick("upca", "042100005264");
  has("typing all twelve is checked", txt("msg"), "check digit is right");
  pick("upca", "042100005265");
  has("a wrong twelfth digit is refused", txt("msg"), "should be 4");
  pick("upca", "03600029145");
  ok("the first digit's bars hang too, since it is printed outside", barLength((9 + 6) * 3 + 1) > 105);
  eq("the ordinary bars do not", barLength((9 + 12) * 3 + 1), 100);

  /* ================= EAN-8 ================= */
  pick("ean8", "9638507");
  eq("the check digit of 9638507 is 4 - a published example", txt("sCheck"), "4");
  eq("the symbol is 67 modules wide", txt("sMod"), "67");
  eq("its bars match a symbol built from literal tables", scan(3, 7, 7).bits, "1010001011010111101111010110111010101001110111001010001001011100101");
  eq("and are read back", decodeEan(scan(3, 7, 7).bits).text, "96385074");
  pick("ean8", "7351353");
  eq("another published EAN-8 ends in 7", txt("sCheck"), "7");
  eq("and is read back", decodeEan(scan(3, 7, 7).bits).text, "73513537");
  pick("ean8", "96385075");
  has("a wrong eighth digit is refused", txt("msg"), "should be 4");

  /* ================= Code 39 ================= */
  function c39(label, text, want) { trip(label, "code39", text, want === undefined ? text : want, decode39, 10, 10); }
  c39("the default word", "108TOOLBOX");
  c39("one character", "A");
  c39("the symbols", "$/+%");
  c39("the other symbols", "-. ");
  c39("every digit", "0123456789");
  c39("every capital", "ABCDEFGHIJKLMNOPQRSTUVWXYZ");
  c39("every character at once", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%");
  c39("lowercase is turned into capitals", "abc", "ABC");
  has("and the message says so", txt("msg"), "lowercase turned into capitals");
  pick("code39", "ABC");
  eq("three characters plus two stars, sixteen modules each, less the last gap: 79", txt("sMod"), "79");
  eq("Code 39 has no check character to show", txt("sCheck"), DASH);
  pick("code39", "A_B");
  has("an underscore is refused by name", txt("msg"), "cannot hold");
  pick("code39", "A*B");
  has("a star is refused because it is the start mark", txt("msg"), "start and stop");

  /* ================= ITF ================= */
  function itf(label, text, want) { trip(label, "itf", text, want === undefined ? text : want, decodeItf, 10, 10); }
  itf("the default", "1234567890");
  itf("one pair", "12");
  itf("zeros", "0000");
  itf("nines", "9999");
  itf("all ten digits again, differently", "9876543210");
  itf("a fourteen-digit carton number", "31415926535897");
  pick("itf", "1234567890");
  eq("ten digits: 4 + 9 x 10 + 5 = 99 modules", txt("sMod"), "99");
  itf("an odd count gets a 0 in front", "123", "0123");
  has("and the message says so", txt("msg"), "put in front");
  var padded = scan(3, 10, 10).bits;
  pick("itf", "0123");
  eq("exactly as if the 0 had been typed", scan(3, 10, 10).bits, padded);
  pick("itf", "12a4");
  has("a letter is named", txt("msg"), "Digits only");
  pick("itf", "");
  has("nothing typed says so", txt("msg"), "Type something");
  pick("itf", "12 34-56");
  eq("spaces and hyphens are ignored", decodeItf(scan(3, 10, 10).bits).text, "123456");

  /* ================= refusing what does not fit ================= */
  pick("code128", "");
  ok("an empty box draws nothing", !drawn());
  ok("and switches the downloads off", pngBtn.disabled && svgBtn.disabled);
  pick("code128", "caf" + String.fromCharCode(0xE9));
  has("a character outside ASCII is named", txt("msg"), "ASCII");
  ok("and nothing is drawn", !drawn());
  var long81 = "";
  for (i = 0; i < 81; i++) { long81 += "A"; }
  pick("code128", long81);
  has("eighty-one characters are refused with the count", txt("msg"), "81 characters");
  var long80 = long81.slice(1);
  pick("code128", long80);
  ok("eighty are fine", drawn());
  eq("and read back", decode128(scan(3, 10, 10).bits).text, long80);

  pick("code39", long80);
  set("scale", "10");
  has("a barcode too wide for a canvas is refused with its width", txt("msg"), "pixels wide");
  ok("and draws nothing", !drawn());
  set("scale", "3");
  ok("a narrower bar width brings it back", drawn());

  /* ================= the size controls ================= */
  pick("code128", "AB");
  var narrow = scan(3, 10, 10);
  var w3 = stage.width, h100 = stage.height;
  set("scale", "5");
  eq("bar width 5 makes every module 5 px", stage.width, (narrow.bits.length + 20) * 5);
  eq("and the bars are the same bars", scan(5, 10, 10).bits, narrow.bits);
  set("scale", "1");
  eq("bar width 1 makes it one pixel a module", stage.width, narrow.bits.length + 20);
  eq("and they are still the same bars", scan(1, 10, 10).bits, narrow.bits);
  set("scale", "3");
  set("height", "200");
  eq("a taller bar adds exactly that much height", stage.height, h100 + 100);
  eq("and leaves the width alone", stage.width, w3);
  set("height", "10");
  has("a bar height below 20 is refused", txt("msg"), "from 20 to 400");
  ok("and nothing is drawn", !drawn());
  set("height", "500");
  has("a bar height above 400 is refused", txt("msg"), "from 20 to 400");
  set("height", "");
  has("an empty height is refused", txt("msg"), "from 20 to 400");
  set("height", "100");
  ok("a sensible height brings it back", drawn());
  eq("at the height it was", stage.height, h100);
  tick("showText", false);
  ok("without the text the picture is shorter", stage.height < h100, stage.height + " vs " + h100);
  eq("and the bars are unchanged", scan(3, 10, 10).bits, narrow.bits);
  tick("showText", true);
  eq("the text brings the height back", stage.height, h100);

  /* ================= switching type ================= */
  set("format", "code128");
  set("text", "108TOOLBOX");
  set("format", "ean13");
  eq("an untouched example is swapped for the new type's example", val("text"), "590123412345");
  has("and the hint changes with the type", txt("hint"), "13th");
  set("text", "111111111111");
  set("format", "upca");
  eq("the visitor's own text is kept when the type changes", val("text"), "111111111111");
  set("format", "code39");
  set("text", "HELLO");
  set("format", "itf");
  eq("text of their own survives a switch to a type that refuses it", val("text"), "HELLO");
  has("and the refusal says why", txt("msg"), "Digits only");

  /* ================= hostile text ================= */
  pick("code128", "<iframe onload=zq>");
  ok("markup typed into the box still draws a barcode", drawn());
  eq("and reads back as text", decode128(scan(3, 10, 10).bits).text, "<iframe onload=zq>");
  eq("with no element made out of it", document.querySelectorAll("iframe").length, 0);
  pick("code128", "<iframe onload=zq>" + String.fromCharCode(0xE9));
  ok("and a refusal that echoes the text makes no element either", document.querySelectorAll("iframe").length === 0);

  /* ================= reset ================= */
  pick("ean13", "5901234123457");
  set("scale", "6");
  set("height", "60");
  tick("showText", false);
  click("resetBtn");
  eq("reset returns to Code 128", val("format"), "code128");
  eq("with the example text", val("text"), "108TOOLBOX");
  eq("and bar width 3", val("scale"), "3");
  eq("and height 100", val("height"), "100");
  ok("and the text switched on", document.getElementById("showText").checked);
  ok("and a barcode drawn", drawn());
  eq("the same picture as at the start", scan(3, 10, 10).bits, first.bits);

  /* ================= saving the files ================= */
  pick("ean13", "590123412345");
  var pngSeen = false, svgSeen = false;
  click("pngBtn");
  waitFor("the PNG is handed over", function () { return window.__saved.length === 1; }, function () {
    var saved = window.__saved[0];
    eq("named for the type and the whole number", saved.name, "barcode-ean13-5901234123457.png");
    eq("as a PNG", saved.blob.type, "image/png");
    createImageBitmap(saved.blob).then(function (bmp) {
      eq("with the width of the picture on screen", bmp.width, stage.width);
      eq("and its height", bmp.height, stage.height);
      var c = document.createElement("canvas");
      c.width = bmp.width; c.height = bmp.height;
      var cx = c.getContext("2d");
      cx.drawImage(bmp, 0, 0);
      var a = cx.getImageData(0, 0, c.width, c.height).data;
      var b = stage.getContext("2d").getImageData(0, 0, stage.width, stage.height).data;
      var same = a.length === b.length;
      for (var k = 0; same && k < a.length; k++) { if (a[k] !== b[k]) { same = false; } }
      ok("every pixel of the file is the pixel on screen", same);
      pngSeen = true;
    });
  });

  waitFor("the PNG has been checked", function () { return pngSeen; }, function () {
    click("svgBtn");
    waitFor("the SVG is handed over", function () { return window.__saved.length === 2; }, function () {
      var saved = window.__saved[1];
      eq("named the same, ending .svg", saved.name, "barcode-ean13-5901234123457.svg");
      eq("as an SVG", saved.blob.type, "image/svg+xml");
      saved.blob.text().then(function (text) {
        var doc = new DOMParser().parseFromString(text, "image/svg+xml");
        ok("it is well-formed XML", doc.getElementsByTagName("parsererror").length === 0, text.slice(0, 100));
        var root = doc.documentElement;
        eq("its width is the picture's", root.getAttribute("width"), String(stage.width));
        eq("its height is the picture's", root.getAttribute("height"), String(stage.height));
        var modules = stage.width / 3, bits = [];
        for (var m = 0; m < modules; m++) { bits.push("0"); }
        Array.prototype.forEach.call(doc.querySelectorAll("g rect"), function (r) {
          var x = Number(r.getAttribute("x")) / 3, w = Number(r.getAttribute("width")) / 3;
          for (var k = 0; k < w; k++) { bits[x + k] = "1"; }
        });
        eq("its bars are the bars on screen", bits.join(""), scan(3, 11, 7).all);
        var digits = "";
        Array.prototype.forEach.call(doc.querySelectorAll("text"), function (t) { digits += t.textContent; });
        eq("and its text is the whole number", digits, "5901234123457");
        svgSeen = true;
      });
    });
  });

  waitFor("the SVG has been checked", function () { return svgSeen; }, function () {
    /* A caption full of characters XML cares about must still be a valid file. */
    pick("code128", "<&>\"'");
    click("svgBtn");
    waitFor("the second SVG is handed over", function () { return window.__saved.length === 3; }, function () {
      var saved = window.__saved[2];
      eq("its name keeps only letters and digits from the text", saved.name, "barcode-code128.svg");
      saved.blob.text().then(function (text) {
        var doc = new DOMParser().parseFromString(text, "image/svg+xml");
        ok("markup characters in the caption leave it well-formed", doc.getElementsByTagName("parsererror").length === 0, text.slice(-160));
        var caption = doc.querySelector("text");
        eq("and the caption reads exactly what was typed", caption ? caption.textContent : "(no text)", "<&>\"'");

        pick("code128", "ID 2026/09#1");
        click("pngBtn");
        waitFor("the third file is handed over", function () { return window.__saved.length === 4; }, function () {
          eq("a name built from text has the odd characters turned into hyphens",
             window.__saved[3].name, "barcode-code128-ID-2026-09-1.png");
          pick("code128", "");
          click("pngBtn");
          click("svgBtn");
          setTimeout(function () {
            eq("with nothing to draw, neither button hands anything over", window.__saved.length, 4);
            finish();
          }, 300);
        });
      });
    });
  });
"""
# ---- END: the test for barcode-generator ----

# ---- START: the test for qr-code-generator ----
T["qr-code-generator"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TIMES = String.fromCharCode(0x00D7);
  var stage = document.getElementById("stage");
  var pngBtn = document.getElementById("pngBtn");
  var svgBtn = document.getElementById("svgBtn");

  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  var GOLD = {"hello": {"version": 1, "n": 21, "hash": 1425558341, "bits": "111111100010101111111100000101110001000001101110100010101011101101110100010101011101101110101011101011101100000100111001000001111111101010101111111000000000000000000000101010100100100010010011110001001000010001000111111101001011000111101011001110101110010011110101001110101000000001010001000101111111100000100101100100000100110001101000101110101100101111111101110100011010100010101110101111011101001100000100001110001011111111101101011100001"}, "default": {"version": 2, "n": 25, "hash": 1112059023, "bits": "1111111011010000001111111100000100011100010100000110111010111001100010111011011101000001101001011101101110100101101000101110110000010101001100010000011111111010101010101111111000000000001000100000000010100011011000011001001011011100101000101111001011111011101100011111101110111111100111100101000010001100111101001000111000001001001001110110111100001111101110100010010100011010000100001011001100111000110101111100011011111001000000000110001001000100011111111010110000101010001100000100110010110001001010111010000111011111100011011101001101001110010110101110101000111000011101110000010011110101101100001111111011000110111001001"}, "digits": {"version": 1, "n": 21, "hash": 4161546187, "bits": "111111101011101111111100000100011001000001101110101101001011101101110101100101011101101110101001001011101100000100111101000001111111101010101111111000000000001100000000111100101111110011101011011000101111001100100100100011000000010111111010111001111010111011100010100100000000000001011001000001111111100001100100111100000100110000110001101110100010111111101101110101011001011010101110101110101100000100000101100010001111111111101000010011010"}, "telugu": {"version": 2, "n": 25, "hash": 3854196825, "bits": "1111111010000001001111111100000101001101110100000110111010001011101010111011011101010010001101011101101110100101011100101110110000010010000111010000011111111010101010101111111000000001000111000000000010110111011011101010010110110000011010000100110000010110111011001111000001110001001000011110001010000111001110000001100111101011010011111010000100000101000010100010111001001011011100011001101011001101000001111101000011111111000000000111011011000101011111111011111000101010010100000101010100010001011110111010010111011111101001011101010001111000110011101110101101110001000011010000010011000000001010001111111010001010111001111"}, "v7": {"version": 7, "n": 45, "hash": 2079917253}, "v10": {"version": 10, "n": 57, "hash": 3771208583}, "v40": {"version": 40, "n": 177, "hash": 410974329}, "wifi": {"version": 3, "n": 29, "hash": 2351303086}};
  var CHART = {"1": [17, 14, 11, 7], "2": [32, 26, 20, 14], "3": [53, 42, 32, 24], "4": [78, 62, 46, 34], "5": [106, 84, 60, 44], "6": [134, 106, 74, 58], "7": [154, 122, 86, 64], "8": [192, 152, 108, 84], "9": [230, 180, 130, 98], "10": [271, 213, 151, 119]};
  var MAXIMA = {"numeric": [7089, 5596, 3993, 3057], "alphanumeric": [4296, 3391, 2420, 1852], "byte": [2953, 2331, 1663, 1273]};
  var STRUCT = [[2, 1, 26], [7, 0, 154], [10, 1, 213], [14, 0, 458], [32, 0, 1952], [36, 0, 2431], [40, 0, 2953]];

  /* Positions of the alignment patterns, and the version information, from the tables of the standard. */
  var ALIGN = { 2: [6, 18], 7: [6, 22, 38], 10: [6, 28, 50], 14: [6, 26, 46, 66],
                32: [6, 34, 60, 86, 112, 138], 36: [6, 24, 50, 76, 102, 128, 154],
                40: [6, 30, 58, 86, 114, 142, 170] };
  var VERSION_INFO = { 7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3, 14: 0x0E60D,
                       32: 0x209D5, 36: 0x24B0B, 40: 0x28C69 };
  var FORMAT_LEVEL = [1, 0, 3, 2];   /* L M Q H as the standard writes them */

  function rep(ch, n) { return new Array(n + 1).join(ch); }
  function cyc(pattern, n) { return (rep(pattern, Math.ceil(n / pattern.length) + 1)).slice(0, n); }
  function fnv(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  }
  function drawn() { return stage.style.display !== "none"; }
  function plain(text) { return String(text).replace(/,/g, ""); }

  /* One render per call: the kind and level are put in place quietly, and the
     text event that follows draws the code. */
  function text(t, level) {
    if (val("kind") !== "text") { set("kind", "text"); }
    document.getElementById("ecl").value = String(level === undefined ? 1 : level);
    set("text", t);
  }

  /* Read the modules off the canvas: one sample in the middle of each square,
     inside a blank border of four squares. */
  function read(scale) {
    var w = stage.width;
    var n = w / scale - 8;
    var data = stage.getContext("2d").getImageData(0, 0, w, stage.height).data;
    var rows = [], flat = "";
    for (var r = 0; r < n; r++) {
      var row = [];
      for (var c = 0; c < n; c++) {
        var at = (((r + 4) * scale + (scale >> 1)) * w + (c + 4) * scale + (scale >> 1)) * 4;
        var dark = data[at] < 128 ? 1 : 0;
        row.push(dark);
        flat += dark;
      }
      rows.push(row);
    }
    return { n: n, rows: rows, bits: flat };
  }

  /* Everything a scanner finds a code by: the three finders and their
     separators, the timing lines, the dark module and the alignment patterns. */
  function structure(m, aligns) {
    var n = m.n, r = m.rows, bad = [], i, dy, dx;
    [[0, 0], [0, n - 7], [n - 7, 0]].forEach(function (c) {
      for (dy = -1; dy <= 7; dy++) {
        for (dx = -1; dx <= 7; dx++) {
          var y = c[0] + dy, x = c[1] + dx;
          if (y < 0 || x < 0 || y >= n || x >= n) { continue; }
          var inside = dy >= 0 && dy <= 6 && dx >= 0 && dx <= 6;
          var d = Math.max(Math.abs(dx - 3), Math.abs(dy - 3));
          var want = inside && d !== 2 ? 1 : 0;
          if (r[y][x] !== want) { bad.push("finder at " + c + " offset " + dy + "," + dx); }
        }
      }
    });
    for (i = 8; i <= n - 9; i++) {
      if (r[6][i] !== (i % 2 === 0 ? 1 : 0)) { bad.push("timing row at " + i); }
      if (r[i][6] !== (i % 2 === 0 ? 1 : 0)) { bad.push("timing column at " + i); }
    }
    if (r[n - 8][8] !== 1) { bad.push("the dark module"); }
    if (aligns) {
      var last = aligns.length - 1;
      aligns.forEach(function (a, ai) {
        aligns.forEach(function (b, bi) {
          if ((ai === 0 && bi === 0) || (ai === 0 && bi === last) || (ai === last && bi === 0)) { return; }
          for (dy = -2; dy <= 2; dy++) {
            for (dx = -2; dx <= 2; dx++) {
              var want = Math.max(Math.abs(dx), Math.abs(dy)) !== 1 ? 1 : 0;
              if (r[a + dy][b + dx] !== want) { bad.push("alignment at " + a + "," + b); }
            }
          }
        });
      });
    }
    return bad;
  }

  /* The 15 format bits, from both copies. */
  function formatBits(m) {
    var r = m.rows, n = m.n, first = 0, second = 0, i;
    for (i = 0; i <= 5; i++) { first |= r[i][8] << i; }
    first |= r[7][8] << 6; first |= r[8][8] << 7; first |= r[8][7] << 8;
    for (i = 9; i < 15; i++) { first |= r[8][14 - i] << i; }
    for (i = 0; i < 8; i++) { second |= r[8][n - 1 - i] << i; }
    for (i = 8; i < 15; i++) { second |= r[n - 15 + i][8] << i; }
    return { first: first, second: second };
  }
  function bchRemainder(v) {
    for (var i = 14; i >= 10; i--) { if ((v >>> i) & 1) { v ^= 0x537 << (i - 10); } }
    return v;
  }
  function checkFormat(label, m, level) {
    var f = formatBits(m);
    eq(label + ": both copies of the format information agree", f.first, f.second);
    var c = f.first ^ 0x5412;
    eq(label + ": the format bits are a valid BCH code word", bchRemainder(c), 0);
    eq(label + ": and name the level that was chosen", (c >> 13) & 3, FORMAT_LEVEL[level]);
    return f.first;
  }
  function checkVersionInfo(label, m, version) {
    var n = m.n, r = m.rows, a = 0, b = 0;
    for (var i = 0; i < 18; i++) {
      a |= r[n - 11 + i % 3][Math.floor(i / 3)] << i;
      b |= r[Math.floor(i / 3)][n - 11 + i % 3] << i;
    }
    eq(label + ": both copies of the version information agree", a, b);
    eq(label + ": and are the table's value for version " + version, a, VERSION_INFO[version]);
  }

  /* ================= the page as it opens ================= */
  ok("a QR code is on the screen when the page opens", drawn());
  eq("it holds the default link", txt("payload"), "https://108toolbox.in");
  eq("version 2, as the capacity chart says for 21 bytes at level M", txt("sVer"), "2");
  eq("25 squares each way", txt("sGrid"), "25 " + TIMES + " 25");
  eq("23 of the 28 data bytes of a version 2-M code: 4 + 8 + 168 bits", txt("sFill"), "23 of 28");
  eq("the picture is that many squares plus a border of four each side, 8 px each", stage.width, (25 + 8) * 8);
  has("the message names the mode", txt("msg"), "byte mode, 21 bytes");
  has("and the version", txt("msg"), "version 2");
  has("and how much damage it survives", txt("msg"), "15%");
  has("and says where it was drawn", txt("msg"), "Drawn in your browser");
  ok("both downloads are ready", !pngBtn.disabled && !svgBtn.disabled);
  shown("the link box is showing", "textFields");
  gone("and the Wi-Fi fields are not", "wifiFields");
  var m0 = read(8);
  eq("it is the matrix OpenCV read back as exactly this link", m0.bits, GOLD["default"].bits);
  eq("with the right structure", structure(m0, ALIGN[2]).join("; "), "");
  checkFormat("the default code", m0, 1);

  /* ================= the published worked example ================= */
  text("HELLO WORLD", 1);
  eq("HELLO WORLD is version 1", txt("sVer"), "1");
  has("capitals and a space use alphanumeric mode", txt("msg"), "alphanumeric mode, 11 characters");
  eq("4 + 9 + 61 bits is 74 bits, which is 10 of the 16 data bytes", txt("sFill"), "10 of 16");
  var hello = read(8);
  eq("its matrix is the one whose codewords match the published example", hello.bits, GOLD.hello.bits);
  var helloFormat = checkFormat("HELLO WORLD", hello, 1);
  eq("its format string at mask 0, level M, is the published 101010000010010",
     ("000000000000000" + helloFormat.toString(2)).slice(-15), "101010000010010");
  eq("with the right structure", structure(hello, null).join("; "), "");

  text("0123456789", 0);
  has("digits use numeric mode", txt("msg"), "numeric mode, 10 characters");
  eq("and version 1 at level L", txt("sVer"), "1");
  eq("and this matrix", read(8).bits, GOLD.digits.bits);

  /* ================= what goes in ================= */
  text("\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41", 1);
  ok("Telugu text draws", drawn());
  has("and is counted in UTF-8 bytes, three for each of its six letters", txt("msg"), "byte mode, 18 bytes");
  eq("as version 2", txt("sVer"), "2");
  eq("with this matrix", read(8).bits, GOLD.telugu.bits);
  text("caf" + String.fromCharCode(0xE9), 1);
  has("an accented letter is two bytes", txt("msg"), "5 bytes");
  text(String.fromCodePoint(0x1F600), 1);
  has("an emoji is four bytes", txt("msg"), "4 bytes");
  text("a" + String.fromCodePoint(0x1F600) + "b", 1);
  has("and they add up", txt("msg"), "6 bytes");
  text("line one\nline two", 1);
  has("a line break is one byte", txt("msg"), "17 bytes");
  text("HTTPS://108TOOLBOX.IN", 1);
  has("capitals and slashes stay in alphanumeric mode", txt("msg"), "alphanumeric mode");
  text("https://108toolbox.in", 1);
  has("lowercase forces byte mode", txt("msg"), "byte mode");
  text("1234567890123456789012345678901234567890", 1);
  has("a long number stays numeric", txt("msg"), "numeric mode, 40 characters");

  /* ================= the capacity chart, every version to 10 at every level ================= */
  var v, lvl;
  for (v = 1; v <= 10; v++) {
    for (lvl = 0; lvl < 4; lvl++) {
      var cap = CHART[v][lvl];
      text(rep("a", cap), lvl);
      eq("version " + v + " level " + "LMQH".charAt(lvl) + " holds " + cap + " bytes", txt("sVer"), String(v));
      text(rep("a", cap + 1), lvl);
      eq("and one more byte needs version " + (v + 1), txt("sVer"), String(v + 1));
    }
  }

  /* ================= the most one code can hold, every mode and level ================= */
  var modes = [["numeric", "7"], ["alphanumeric", "Z"], ["byte", "x"]];
  modes.forEach(function (mode) {
    for (var l = 0; l < 4; l++) {
      var most = MAXIMA[mode[0]][l];
      text(rep(mode[1], most), l);
      eq(mode[0] + " at level " + "LMQH".charAt(l) + ": " + most + " fit in version 40", drawn() ? txt("sVer") : "refused", "40");
      text(rep(mode[1], most + 1), l);
      ok("and one more is refused", !drawn());
      has("naming the limit", plain(txt("msg")), String(most));
    }
  });

  /* ================= every row of both tables, locked =================
     For each of the 160 combinations of version and level, the smallest and the
     largest text that fits: each was decoded to exactly its text by an
     independent decoder (block by block, Reed-Solomon re-derived) before its
     matrix was recorded. A typo in one table entry, one alignment position or
     one block count fails here by name. */
  var SWEEP = [[0, 1, 1, 2087236283], [0, 1, 17, 454188295], [0, 2, 18, 3449790025], [0, 2, 32, 3452831619], [0, 3, 33, 2236000884], [0, 3, 53, 1301206725], [0, 4, 54, 114053048], [0, 4, 78, 3900377604], [0, 5, 79, 2894960073], [0, 5, 106, 1352797307], [0, 6, 107, 2338867650], [0, 6, 134, 2666943548], [0, 7, 135, 2691799315], [0, 7, 154, 567230587], [0, 8, 155, 1588778675], [0, 8, 192, 2321344831], [0, 9, 193, 3893691483], [0, 9, 230, 3783627015], [0, 10, 231, 2728851593], [0, 10, 271, 2703756617], [0, 11, 272, 910651417], [0, 11, 321, 335074341], [0, 12, 322, 4281662673], [0, 12, 367, 3667002327], [0, 13, 368, 3678274523], [0, 13, 425, 151055407], [0, 14, 426, 2832715091], [0, 14, 458, 558492223], [0, 15, 459, 2796571021], [0, 15, 520, 3765519125], [0, 16, 521, 3824185162], [0, 16, 586, 1040247138], [0, 17, 587, 3302207081], [0, 17, 644, 3517373307], [0, 18, 645, 2620196579], [0, 18, 718, 1010492251], [0, 19, 719, 348488509], [0, 19, 792, 2372948391], [0, 20, 793, 1538267827], [0, 20, 858, 1875524343], [0, 21, 859, 3978536181], [0, 21, 929, 2487612403], [0, 22, 930, 4177796517], [0, 22, 1003, 707325409], [0, 23, 1004, 666562899], [0, 23, 1091, 3394084161], [0, 24, 1092, 647075969], [0, 24, 1171, 1624901793], [0, 25, 1172, 2157100895], [0, 25, 1273, 3362530961], [0, 26, 1274, 304233553], [0, 26, 1367, 4220189715], [0, 27, 1368, 4098961953], [0, 27, 1465, 724481331], [0, 28, 1466, 3045876588], [0, 28, 1528, 3003186436], [0, 29, 1529, 1282238267], [0, 29, 1628, 3503569013], [0, 30, 1629, 1854959131], [0, 30, 1732, 446963545], [0, 31, 1733, 214372018], [0, 31, 1840, 1906839446], [0, 32, 1841, 3532341502], [0, 32, 1952, 3178177086], [0, 33, 1953, 2173711680], [0, 33, 2068, 855884896], [0, 34, 2069, 3457066733], [0, 34, 2188, 171135621], [0, 35, 2189, 2617494511], [0, 35, 2303, 622344037], [0, 36, 2304, 37166631], [0, 36, 2431, 3716328121], [0, 37, 2432, 2244996419], [0, 37, 2563, 2396042905], [0, 38, 2564, 714187519], [0, 38, 2699, 206837987], [0, 39, 2700, 3332177007], [0, 39, 2809, 3619384371], [0, 40, 2810, 3538470801], [0, 40, 2953, 3533345427], [1, 1, 1, 2648599305], [1, 1, 14, 648859603], [1, 2, 15, 33534777], [1, 2, 26, 664108763], [1, 3, 27, 1255121150], [1, 3, 42, 3197698270], [1, 4, 43, 2561723302], [1, 4, 62, 837875998], [1, 5, 63, 1687985183], [1, 5, 84, 3212899343], [1, 6, 85, 3869207038], [1, 6, 106, 4176704502], [1, 7, 107, 1149351661], [1, 7, 122, 1576209451], [1, 8, 123, 2220700961], [1, 8, 152, 1667655543], [1, 9, 153, 700915055], [1, 9, 180, 1548862145], [1, 10, 181, 3597448727], [1, 10, 213, 3179999575], [1, 11, 214, 100492569], [1, 11, 251, 697833449], [1, 12, 252, 4043490867], [1, 12, 287, 319121847], [1, 13, 288, 1797101023], [1, 13, 331, 2381461339], [1, 14, 332, 2254308223], [1, 14, 362, 2611491735], [1, 15, 363, 3843354125], [1, 15, 412, 473778611], [1, 16, 413, 4240792976], [1, 16, 450, 3839069858], [1, 17, 451, 3220583913], [1, 17, 504, 2517152995], [1, 18, 505, 3505555019], [1, 18, 560, 387408279], [1, 19, 561, 1911786851], [1, 19, 624, 3593568355], [1, 20, 625, 784333061], [1, 20, 666, 2343065641], [1, 21, 667, 519203445], [1, 21, 711, 85114961], [1, 22, 712, 1320757007], [1, 22, 779, 2668727487], [1, 23, 780, 1070395539], [1, 23, 857, 2038013837], [1, 24, 858, 2968945863], [1, 24, 911, 3944981235], [1, 25, 912, 2204019729], [1, 25, 997, 403475841], [1, 26, 998, 1887018363], [1, 26, 1059, 1233293397], [1, 27, 1060, 275402197], [1, 27, 1125, 2675517075], [1, 28, 1126, 1819173832], [1, 28, 1190, 3525833430], [1, 29, 1191, 3935883031], [1, 29, 1264, 2283193421], [1, 30, 1265, 2350811085], [1, 30, 1370, 389323977], [1, 31, 1371, 4184639130], [1, 31, 1452, 417519210], [1, 32, 1453, 2780285178], [1, 32, 1538, 3143076190], [1, 33, 1539, 4263181888], [1, 33, 1628, 3614623702], [1, 34, 1629, 509839605], [1, 34, 1722, 3706099715], [1, 35, 1723, 4259256305], [1, 35, 1809, 2264845619], [1, 36, 1810, 1810020839], [1, 36, 1911, 1573317711], [1, 37, 1912, 3790160463], [1, 37, 1989, 2097664569], [1, 38, 1990, 1446777477], [1, 38, 2099, 932230743], [1, 39, 2100, 2935424751], [1, 39, 2213, 1069402041], [1, 40, 2214, 1604212909], [1, 40, 2331, 3620783465], [2, 1, 1, 585942405], [2, 1, 11, 3838682755], [2, 2, 12, 3805864332], [2, 2, 20, 1431486903], [2, 3, 21, 1521857199], [2, 3, 32, 1103898008], [2, 4, 33, 2186133881], [2, 4, 46, 1054288759], [2, 5, 47, 881869843], [2, 5, 60, 1452825069], [2, 6, 61, 1577631720], [2, 6, 74, 570068119], [2, 7, 75, 2027069851], [2, 7, 86, 1157409801], [2, 8, 87, 1652684101], [2, 8, 108, 4194315335], [2, 9, 109, 4111493207], [2, 9, 130, 2099937391], [2, 10, 131, 4007021065], [2, 10, 151, 459289549], [2, 11, 152, 3082279157], [2, 11, 177, 2961449207], [2, 12, 178, 534050601], [2, 12, 203, 569289744], [2, 13, 204, 1202539993], [2, 13, 241, 787745587], [2, 14, 242, 2232433511], [2, 14, 258, 2625583363], [2, 15, 259, 565425444], [2, 15, 292, 909953705], [2, 16, 293, 146626072], [2, 16, 322, 3846530078], [2, 17, 323, 739578293], [2, 17, 364, 3170210391], [2, 18, 365, 3611643511], [2, 18, 394, 1761541082], [2, 19, 395, 172309175], [2, 19, 442, 3743853097], [2, 20, 443, 1546778167], [2, 20, 482, 334724301], [2, 21, 483, 4068942449], [2, 21, 509, 1414872091], [2, 22, 510, 1116775559], [2, 22, 565, 580511009], [2, 23, 566, 2819170603], [2, 23, 611, 2541664023], [2, 24, 612, 4063366115], [2, 24, 661, 2910797355], [2, 25, 662, 323398717], [2, 25, 715, 1016722848], [2, 26, 716, 2951221531], [2, 26, 751, 3222940297], [2, 27, 752, 2692394611], [2, 27, 805, 1092444791], [2, 28, 806, 1389644776], [2, 28, 868, 631502560], [2, 29, 869, 3078317521], [2, 29, 908, 660826271], [2, 30, 909, 891732281], [2, 30, 982, 3562352381], [2, 31, 983, 3747494154], [2, 31, 1030, 1720773370], [2, 32, 1031, 2066664448], [2, 32, 1112, 3312944852], [2, 33, 1113, 898844792], [2, 33, 1168, 3860477180], [2, 34, 1169, 1149154225], [2, 34, 1228, 3105766979], [2, 35, 1229, 299098893], [2, 35, 1283, 2648820601], [2, 36, 1284, 1116129441], [2, 36, 1351, 2652816325], [2, 37, 1352, 803186787], [2, 37, 1423, 2797191991], [2, 38, 1424, 746523299], [2, 38, 1499, 3911911759], [2, 39, 1500, 1494274583], [2, 39, 1579, 915904475], [2, 40, 1580, 1063136637], [2, 40, 1663, 3299847901], [3, 1, 1, 2889886831], [3, 1, 7, 3193784607], [3, 2, 8, 2004731131], [3, 2, 14, 2511902151], [3, 3, 15, 2981765282], [3, 3, 24, 1930792626], [3, 4, 25, 2123966586], [3, 4, 34, 3332639714], [3, 5, 35, 2456203023], [3, 5, 44, 3012374064], [3, 6, 45, 1360429350], [3, 6, 58, 246088047], [3, 7, 59, 3143959583], [3, 7, 64, 1970339891], [3, 8, 65, 1770907467], [3, 8, 84, 3814092785], [3, 9, 85, 1018504185], [3, 9, 98, 1636216948], [3, 10, 99, 2306442717], [3, 10, 119, 4164204761], [3, 11, 120, 4183230796], [3, 11, 137, 2272209609], [3, 12, 138, 1581013222], [3, 12, 155, 643967850], [3, 13, 156, 1003939919], [3, 13, 177, 1891857015], [3, 14, 178, 1443888015], [3, 14, 194, 3177111145], [3, 15, 195, 1285711228], [3, 15, 220, 635112521], [3, 16, 221, 3395170492], [3, 16, 250, 3631801938], [3, 17, 251, 1279302089], [3, 17, 280, 2066204199], [3, 18, 281, 1229339965], [3, 18, 310, 2469188637], [3, 19, 311, 1062673151], [3, 19, 338, 4272458563], [3, 20, 339, 1462688003], [3, 20, 382, 1737796725], [3, 21, 383, 3631535629], [3, 21, 403, 3711193519], [3, 22, 404, 142346597], [3, 22, 439, 2576323981], [3, 23, 440, 3767312960], [3, 23, 461, 3821485838], [3, 24, 462, 3105057417], [3, 24, 511, 2443022434], [3, 25, 512, 1750530665], [3, 25, 535, 876494677], [3, 26, 536, 3531011935], [3, 26, 593, 3878241397], [3, 27, 594, 2330511591], [3, 27, 625, 2033881295], [3, 28, 626, 2800858380], [3, 28, 658, 1555125468], [3, 29, 659, 3210401747], [3, 29, 698, 2921464477], [3, 30, 699, 1096131525], [3, 30, 742, 2027177717], [3, 31, 743, 3314156408], [3, 31, 790, 3778989396], [3, 32, 791, 3648992656], [3, 32, 842, 2844491820], [3, 33, 843, 1736188], [3, 33, 898, 2480527867], [3, 34, 899, 1139197976], [3, 34, 958, 505693910], [3, 35, 959, 1560500757], [3, 35, 983, 1481739175], [3, 36, 984, 2688323629], [3, 36, 1051, 1134949745], [3, 37, 1052, 924850833], [3, 37, 1093, 1104119215], [3, 38, 1094, 1322166253], [3, 38, 1139, 4125234223], [3, 39, 1140, 2561890629], [3, 39, 1219, 31554291], [3, 40, 1220, 2168482327], [3, 40, 1273, 3053909089]];
  set("scale", "2");
  SWEEP.forEach(function (s) {
    text(cyc("the quick brown fox jumps over 0123456789 the lazy dog?", s[2]), s[0]);
    var m = drawn() ? read(2) : null;
    eq("version " + s[1] + " level " + "LMQH".charAt(s[0]) + " with " + s[2] + " bytes: version and matrix",
       m ? m.n + ":" + fnv(m.bits) : "refused", (17 + 4 * s[1]) + ":" + s[3]);
  });
  set("scale", "8");

  /* ================= structure at every kind of version ================= */
  STRUCT.forEach(function (s) {
    var version = s[0], level = s[1], length = s[2];
    text(cyc("abcdefghij", length), level);
    eq("version " + version + " is what " + length + " bytes at level " + "LMQH".charAt(level) + " needs", txt("sVer"), String(version));
    var m = read(8);
    eq("version " + version + " has " + (17 + 4 * version) + " squares a side", m.n, 17 + 4 * version);
    eq("version " + version + ": finders, timing, dark module and alignment patterns are right",
       structure(m, ALIGN[version]).join("; "), "");
    checkFormat("version " + version, m, level);
    if (VERSION_INFO[version]) { checkVersionInfo("version " + version, m, version); }
  });

  /* ================= goldens for larger codes ================= */
  text(cyc("abcdefghij", 135), 0);
  eq("version 7 matrix, as OpenCV read it", fnv(read(8).bits), GOLD.v7.hash);
  text(cyc("0123456789abcdef", 181), 1);
  eq("version 10 matrix, as OpenCV read it", fnv(read(8).bits), GOLD.v10.hash);
  text(rep("x", 2953), 0);
  eq("version 40 matrix, as OpenCV read it", fnv(read(8).bits), GOLD.v40.hash);

  /* ================= Wi-Fi ================= */
  function wifi(ssid, pass, sec, hidden) {
    set("kind", "wifi");
    set("ssid", ssid);
    set("wpass", pass);
    set("wsec", sec);
    tick("whidden", !!hidden);
  }
  set("ecl", "1");
  wifi("HomeNet", "correct horse", "WPA", false);
  gone("the link box is hidden for Wi-Fi", "textFields");
  shown("and the Wi-Fi fields show", "wifiFields");
  eq("a plain network", txt("payload"), "WIFI:T:WPA;S:HomeNet;P:correct horse;;");
  eq("as version 3", txt("sVer"), "3");
  has("labelled as Wi-Fi", txt("msg"), "Wi-Fi, byte mode");
  eq("with the matrix OpenCV read back", fnv(read(8).bits), GOLD.wifi.hash);
  wifi("My;Net:1", "pa,ss\\word\"", "WPA", false);
  eq("a name and password full of special characters are escaped",
     txt("payload"), "WIFI:T:WPA;S:My\\;Net\\:1;P:pa\\,ss\\\\word\\\";;");
  wifi("Guest", "ignored", "nopass", false);
  eq("an open network has no password field, whatever is typed", txt("payload"), "WIFI:T:nopass;S:Guest;;");
  wifi("Hidden", "secret123", "WPA", true);
  eq("a hidden network says so", txt("payload"), "WIFI:T:WPA;S:Hidden;P:secret123;H:true;;");
  wifi("OldRouter", "12345", "WEP", false);
  eq("WEP", txt("payload"), "WIFI:T:WEP;S:OldRouter;P:12345;;");
  wifi("\u0c07\u0c32\u0c4d\u0c32\u0c41", "abc", "WPA", false);
  ok("a Telugu network name draws", drawn());
  wifi("", "abc", "WPA", false);
  ok("no network name draws nothing", !drawn());
  has("and asks for it", txt("msg"), "network name");
  wifi("HomeNet", "", "WPA", false);
  ok("no password with WPA draws nothing", !drawn());
  has("and asks for it", txt("msg"), "password");
  wifi("HomeNet", "", "nopass", false);
  ok("no password is fine for an open network", drawn());
  wifi("HomeNet", "abc", "WEP", false);
  set("wsec", "");
  ok("a security value that is not on the list is refused", !drawn());
  has("and says so", txt("msg"), "security");
  set("wsec", "WPA");
  set("kind", "text");
  shown("switching back shows the link box", "textFields");
  gone("and hides the Wi-Fi fields", "wifiFields");
  ok("the text that was there before is 2953 bytes, more than level M can hold, so it is refused", !drawn());
  has("naming the level's limit", plain(txt("msg")), "2331");

  /* ================= refusing what does not fit ================= */
  text("", 1);
  ok("nothing typed draws nothing", !drawn());
  has("and says what to do", txt("msg"), "Type or paste");
  eq("with the tiles blanked", txt("sVer") + txt("sGrid") + txt("sFill"), DASH + DASH + DASH);
  eq("and the readout empty", txt("payload"), "");
  ok("and both downloads off", pngBtn.disabled && svgBtn.disabled);
  text(rep("a", 10001), 1);
  ok("ten thousand characters is refused at once", !drawn());
  has("with the count", plain(txt("msg")), "10001 characters");
  text("hello", 1);
  set("ecl", "9");
  ok("a level that is not on the list is refused, not quietly turned into Low", !drawn());
  has("and says so", txt("msg"), "error-correction level");
  set("ecl", "1");
  ok("choosing a real one brings the code back", drawn());
  set("scale", "999");
  ok("a module size that is not on the list is refused", !drawn());
  set("scale", "8");

  /* ================= the choices ================= */
  text(rep("a", 100), 0); eq("100 bytes at L is version 5, from the chart", txt("sVer"), "5");
  set("ecl", "1"); eq("at M version 6", txt("sVer"), "6");
  set("ecl", "2"); eq("at Q version 8", txt("sVer"), "8");
  set("ecl", "3"); eq("at H version 10", txt("sVer"), "10");
  has("and the message says how much damage H survives", txt("msg"), "30%");
  set("ecl", "0"); has("and L", txt("msg"), "7%");

  text("https://108toolbox.in", 1);
  var at8 = read(8);
  set("scale", "4");
  eq("half the module size makes half the picture", stage.width, 33 * 4);
  eq("of the same matrix", read(4).bits, at8.bits);
  set("scale", "16");
  eq("and 16 makes it twice as big as 8", stage.width, 33 * 16);
  eq("still the same matrix", read(16).bits, at8.bits);
  set("scale", "8");

  /* ================= colours ================= */
  set("fg", "#1a2b6d");
  set("bg", "#fff3b0");
  var px = stage.getContext("2d").getImageData(1, 1, 1, 1).data;
  eq("the border takes the background colour", px[0] + "," + px[1] + "," + px[2], "255,243,176");
  var dark = stage.getContext("2d").getImageData(4 * 8 + 3, 4 * 8 + 3, 1, 1).data;
  eq("and the corner of the first finder takes the code colour", dark[0] + "," + dark[1] + "," + dark[2], "26,43,109");
  has("good colours draw normally", txt("msg"), "Drawn in your browser");
  set("fg", "#777777"); set("bg", "#888888");
  ok("colours that are too close still draw", drawn());
  has("but are warned about", txt("msg"), "too close");
  has("starting with the word Drawn", txt("msg"), "Drawn, but");
  set("fg", "#ffffff"); set("bg", "#000000");
  has("a light code on a dark background is warned about", txt("msg"), "lighter than its background");
  set("fg", "#000000"); set("bg", "#ffffff");
  has("black on white is not", txt("msg"), "Drawn in your browser");

  /* ================= hostile text ================= */
  text("<iframe onload=zq>", 1);
  ok("markup typed into the box still draws a code", drawn());
  eq("the readout shows it as text", txt("payload"), "<iframe onload=zq>");
  eq("with no element made out of it", document.querySelectorAll("iframe").length, 0);
  wifi("<iframe onload=zq>", "<b>x</b>", "WPA", false);
  ok("and the same for a network name", drawn() && document.querySelectorAll("iframe, b").length === 0);
  set("kind", "text");

  /* ================= reset ================= */
  text("something else", 3);
  set("scale", "3");
  set("fg", "#1a2b6d");
  click("resetBtn");
  eq("reset brings back the link box", val("kind"), "text");
  eq("the default text", val("text"), "https://108toolbox.in");
  eq("level M", val("ecl"), "1");
  eq("module size 8", val("scale"), "8");
  eq("and black", val("fg"), "#000000");
  ok("and a code drawn", drawn());
  eq("the same picture as at the start", read(8).bits, GOLD["default"].bits);

  /* ================= saving the files ================= */
  var pngSeen = false, svgSeen = false;
  click("pngBtn");
  waitFor("the PNG is handed over", function () { return window.__saved.length === 1; }, function () {
    var saved = window.__saved[0];
    eq("named for the text", saved.name, "qr-code-https-108toolbox-in.png");
    eq("as a PNG", saved.blob.type, "image/png");
    createImageBitmap(saved.blob).then(function (bmp) {
      eq("the width of the picture on screen", bmp.width, stage.width);
      var c = document.createElement("canvas");
      c.width = bmp.width; c.height = bmp.height;
      var cx = c.getContext("2d");
      cx.drawImage(bmp, 0, 0);
      var a = cx.getImageData(0, 0, c.width, c.height).data;
      var b = stage.getContext("2d").getImageData(0, 0, stage.width, stage.height).data;
      var same = a.length === b.length;
      for (var k = 0; same && k < a.length; k++) { if (a[k] !== b[k]) { same = false; } }
      ok("every pixel of the file is the pixel on screen", same);
      pngSeen = true;
    });
  });

  waitFor("the PNG has been checked", function () { return pngSeen; }, function () {
    set("fg", "#1a2b6d");
    set("bg", "#fff3b0");
    click("svgBtn");
    waitFor("the SVG is handed over", function () { return window.__saved.length === 2; }, function () {
      var saved = window.__saved[1];
      eq("named the same, ending .svg", saved.name, "qr-code-https-108toolbox-in.svg");
      eq("as an SVG", saved.blob.type, "image/svg+xml");
      saved.blob.text().then(function (svg) {
        var doc = new DOMParser().parseFromString(svg, "image/svg+xml");
        ok("it is well-formed XML", doc.getElementsByTagName("parsererror").length === 0, svg.slice(0, 100));
        var root = doc.documentElement;
        eq("its viewBox is in squares, border included", root.getAttribute("viewBox"), "0 0 33 33");
        eq("its width is the picture's", root.getAttribute("width"), String(stage.width));
        var back = doc.querySelector("rect");
        eq("its background is the chosen colour", back.getAttribute("fill"), "#fff3b0");
        var path = doc.querySelector("path");
        eq("its code is the chosen colour", path.getAttribute("fill"), "#1a2b6d");
        var rows = [];
        for (var y = 0; y < 25; y++) { rows.push(rep("0", 25).split("")); }
        var pattern = /M(\d+) (\d+)h(\d+)v1h-(\d+)z/g, part, pieces = 0;
        while ((part = pattern.exec(path.getAttribute("d"))) !== null) {
          pieces++;
          for (var k = 0; k < Number(part[3]); k++) { rows[Number(part[2]) - 4][Number(part[1]) - 4 + k] = "1"; }
        }
        ok("the path has pieces", pieces > 10, String(pieces));
        eq("and they are exactly the modules of the code", rows.map(function (r) { return r.join(""); }).join(""), GOLD["default"].bits);
        svgSeen = true;
      });
    });
  });

  waitFor("the SVG has been checked", function () { return svgSeen; }, function () {
    wifi("HomeNet", "correct horse", "WPA", false);
    click("pngBtn");
    waitFor("the Wi-Fi PNG is handed over", function () { return window.__saved.length === 3; }, function () {
      eq("a Wi-Fi file never has the network name or password in its name", window.__saved[2].name, "qr-code-wifi.png");
      set("kind", "text");
      text("", 1);
      click("pngBtn");
      click("svgBtn");
      setTimeout(function () {
        eq("with nothing to draw, neither button hands anything over", window.__saved.length, 3);
        finish();
      }, 300);
    });
  });
"""
# ---- END: the test for qr-code-generator ----

# ---- START: the test for js-minifier ----
T["js-minifier"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var CASES = [{"label": "ASI: statements without semicolons", "source": "var a = 1\nvar b = 2\nfoo(a, b)\n", "out": "var a=1\nvar b=2\nfoo(a,b)"}, {"label": "return then a new line returns nothing", "source": "function f() {\n  return\n  42\n}\n", "out": "function f(){return\n42}"}, {"label": "return with the value on the same line", "source": "function f() {\n  return   42\n}\n", "out": "function f(){return 42}"}, {"label": "prefix ++ on the next line", "source": "var a = b\n++c\n", "out": "var a=b\n++c"}, {"label": "postfix ++ then a statement", "source": "a++\nb\n", "out": "a++\nb"}, {"label": "a call split across lines", "source": "var x = y\n(z)\n", "out": "var x=y(z)"}, {"label": "IIFE after a function expression", "source": "var f = function () {}\n(function () {})()\n", "out": "var f=function(){}(function(){})()"}, {"label": "IIFE after a declaration", "source": "function f() {}\n(function () {})()\n", "out": "function f(){}(function(){})()"}, {"label": "index access on the next line", "source": "x\n[1, 2].forEach(g)\n", "out": "x[1,2].forEach(g)"}, {"label": "method chain with leading dots", "source": "foo\n  .bar()\n  .baz\n  ?.qux\n", "out": "foo.bar().baz?.qux"}, {"label": "operators at the start of a line", "source": "var t = a\n  + b\n  - c\n  && d\n  || e\nvar u = (g\n  ?? h)\n", "out": "var t=a+b-c&&d||e\nvar u=(g??h)"}, {"label": "else, catch and finally after a bare statement", "source": "if (a) f()\nelse g()\ntry { x() }\ncatch (e) { y() }\nfinally { z() }\nif (a) { f() }\nelse { g() }\n", "out": "if(a)f()\nelse g()\ntry{x()}\ncatch(e){y()}\nfinally{z()}\nif(a){f()}\nelse{g()}"}, {"label": "do while after a bare statement", "source": "do x()\nwhile (y)\ndo { x() }\nwhile (y)\n", "out": "do x()\nwhile(y)\ndo{x()}\nwhile(y)"}, {"label": "ternary across lines", "source": "var v = a ?\n  b :\n  c\n", "out": "var v=a?b:c"}, {"label": "division, not a regex", "source": "a = b / c / d\n", "out": "a=b/c/d"}, {"label": "division after a call", "source": "x = f(y) / 2 / 3\n", "out": "x=f(y)/2/3"}, {"label": "division after a parenthesis", "source": "x = (a + b) / c / d\n", "out": "x=(a+b)/c/d"}, {"label": "division after a bracket", "source": "x = a[0] / 2 / 3\n", "out": "x=a[0]/2/3"}, {"label": "division after postfix ++", "source": "x = a++ / 2 / 3\n", "out": "x=a++/2/3"}, {"label": "division on the next line", "source": "x = b\n/ c\n/ d\n", "out": "x=b/c/d"}, {"label": "a regex with spaces inside", "source": "x = /a  b/g.test(s)\n", "out": "x=/a  b/g.test(s)"}, {"label": "a regex containing a slash in a class", "source": "var r = /[/]\\//.source\n", "out": "var r=/[/]\\//.source"}, {"label": "a regex containing a brace and quotes", "source": "var r = /}\"'`/g\n", "out": "var r=/}\"'`/g"}, {"label": "regex after return", "source": "function f(s) { return /a b/.test(s) }\n", "out": "function f(s){return/a b/.test(s)}"}, {"label": "regex after typeof", "source": "var t = typeof /a b/\n", "out": "var t=typeof/a b/"}, {"label": "regex after a control header", "source": "if (x) /a , b/.test(y)\nwhile (x) /a , b/.test(y)\nfor (;;) /a , b/.test(y)\nwith (o) /a , b/.test(y)\n", "out": "if(x)/a , b/.test(y)\nwhile(x)/a , b/.test(y)\nfor(;;)/a , b/.test(y)\nwith(o)/a , b/.test(y)"}, {"label": "regex after a block", "source": "function f() {}\n/a b/.test(s)\nif (a) {}\n/c d/.test(s)\n", "out": "function f(){}\n/a b/.test(s)\nif(a){}\n/c d/.test(s)"}, {"label": "regex in a case", "source": "switch (x) { case /a b/.test(s): y() }\n", "out": "switch(x){case/a b/.test(s):y()}"}, {"label": "regex after =>", "source": "var f = s => /a b/.test(s)\n", "out": "var f=s=>/a b/.test(s)"}, {"label": "regex as an argument", "source": "s.replace(/  /g, ' ').split(/ , /)\n", "out": "s.replace(/  /g,' ').split(/ , /)"}, {"label": "a regex followed by a word", "source": "x = /a/ in o\ny = /b/g instanceof RegExp\n", "out": "x=/a/ in o\ny=/b/g instanceof RegExp"}, {"label": "template: escaped backtick, dollar and backslash", "source": "var s = `a\\`b \\${c} \\\\ d ${ e }`\n", "out": "var s=`a\\`b \\${c} \\\\ d ${ e }`"}, {"label": "uppercase and signed exponents", "source": "var n = [1E3, 1E+3, 2e-2, 3.5E-1]\n", "out": "var n=[1E3,1E+3,2e-2,3.5E-1]"}, {"label": "a unicode escape in a name and a braced one", "source": "var \\u0061b = 1, \\u{62}c = 2\n", "out": "var \\u0061b=1,\\u{62}c=2"}, {"label": "an escaped name before a keyword", "source": "var \\u{62} = {}\nx = \\u{62} in o\ny = \\u{62} instanceof Object\n", "out": "var \\u{62}={}\nx=\\u{62} in o\ny=\\u{62} instanceof Object"}, {"label": "a number ending in a dot before a keyword", "source": "x = 5. in o\ny = 5. instanceof Number\nz = 1 in o\n", "out": "x=5. in o\ny=5. instanceof Number\nz=1 in o"}, {"label": "keyword-named property then division", "source": "x = o.return / 2 / 3\ny = o.typeof / 2 / 3\nz = o.in / 2 / 3\n", "out": "x=o.return/2/3\ny=o.typeof/2/3\nz=o.in/2/3"}, {"label": "a division then a regex", "source": "x = a / /b c/.source.length\n", "out": "x=a/ /b c/.source.length"}, {"label": "the /= operator and a regex starting with =", "source": "a /= 2\nb = /=/.test(c)\n", "out": "a/=2\nb=/=/.test(c)"}, {"label": "a comment after a division", "source": "x = 1 / 2 / 3 // trailing / comment\n", "out": "x=1/2/3"}, {"label": "template: plain", "source": "var s = `a  b`\n", "out": "var s=`a  b`"}, {"label": "template: expression", "source": "var s = `total: ${ a + b }`\n", "out": "var s=`total: ${ a + b }`"}, {"label": "template: nested", "source": "var s = `a ${ `b ${ c } d` } e`\n", "out": "var s=`a ${ `b ${ c } d` } e`"}, {"label": "template: brace and backtick in a string inside", "source": "var s = `x ${ '}' + \"`\" + '\\'' } y`\n", "out": "var s=`x ${ '}' + \"`\" + '\\'' } y`"}, {"label": "template: regex with a brace inside", "source": "var s = `x ${ /}/.test(t) } y`\n", "out": "var s=`x ${ /}/.test(t) } y`"}, {"label": "template: object literal inside", "source": "var s = `x ${ { a: 1 }.a } y`\n", "out": "var s=`x ${ { a: 1 }.a } y`"}, {"label": "template: tagged", "source": "var s = tag`a ${ b } c`\nvar t = tag\n`x`\n", "out": "var s=tag`a ${ b } c`\nvar t=tag`x`"}, {"label": "template: line breaks kept", "source": "var s = `line one\n  line two\n`\n", "out": "var s=`line one\n  line two\n`"}, {"label": "string containing comment markers", "source": "var s = 'a // b /* c */ d'\nvar t = \"e // f\"\n", "out": "var s='a // b /* c */ d'\nvar t=\"e // f\""}, {"label": "string with escapes and a continuation", "source": "var s = 'it\\'s \\\\ done' + \"say \\\"hi\\\"\" + 'line \\\nnext'\n", "out": "var s='it\\'s \\\\ done'+\"say \\\"hi\\\"\"+'line \\\nnext'"}, {"label": "string with a script tag", "source": "var s = '<\/script>'\n", "out": "var s='<\/script>'"}, {"label": "a unicode line separator inside a string", "source": "var s = 'a\u2028b'\n", "out": "var s='a\u2028b'"}, {"label": "numbers", "source": "var n = [1, 1.5, .5, 5., 0.5e-3, 1_000, 0xFF, 0b101, 0o17, 10n, 0xFFn, 1e3]\n", "out": "var n=[1,1.5,.5,5.,0.5e-3,1_000,0xFF,0b101,0o17,10n,0xFFn,1e3]"}, {"label": "a dot after a whole number", "source": "var a = 1..toString()\nvar b = 1 .toString()\nvar c = 0.5.toFixed(1)\nvar d = 1.5.toFixed(1)\n", "out": "var a=1..toString()\nvar b=1 .toString()\nvar c=0.5.toFixed(1)\nvar d=1.5.toFixed(1)"}, {"label": "a conditional with a fraction", "source": "var a = x?.5:1\nvar b = x ? .5 : 1\nvar c = x?.y\n", "out": "var a=x? .5:1\nvar b=x? .5:1\nvar c=x?.y"}, {"label": "operators that would fuse", "source": "a = b + +c\na = b - -c\na = b + ++c\na = b - --c\na = b++ + c\na = b-- - c\na = b++ + ++c\na = b - - - c\n", "out": "a=b+ +c\na=b- -c\na=b+ ++c\na=b- --c\na=b+++c\na=b---c\na=b+++ ++c\na=b- - -c"}, {"label": "keywords next to names", "source": "var t = typeof x, u = void 0, v = a in b, w = a instanceof B\nnew Foo\ndelete a.b\n", "out": "var t=typeof x,u=void 0,v=a in b,w=a instanceof B\nnew Foo\ndelete a.b"}, {"label": "else and do", "source": "if (a) b(); else if (c) d(); else e()\ndo x(); while (y)\n", "out": "if(a)b();else if(c)d();else e()\ndo x();while(y)"}, {"label": "loops", "source": "for (var k in o) f(k)\nfor (const v of xs) g(v)\nfor (var i = 0, j = 9; i < j; i++, j--) h()\n", "out": "for(var k in o)f(k)\nfor(const v of xs)g(v)\nfor(var i=0,j=9;i<j;i++,j--)h()"}, {"label": "a comment acting as a line break", "source": "var a = b /*\n*/ ++c\n", "out": "var a=b\n++c"}, {"label": "a comment on the same line as return", "source": "function f() { return /* x */ 1 }\n", "out": "function f(){return 1}"}, {"label": "a comment at the end with no newline", "source": "foo() // done", "out": "foo()"}, {"label": "line comment inside an expression", "source": "var a = 1 + // two\n  2\n", "out": "var a=1+2"}, {"label": "classes", "source": "class A extends B {\n  static x = 1\n  #p = 2\n  get v() { return this.#p }\n  set v(n) { this.#p = n }\n  static async *gen() { yield 1 }\n  [k]() {}\n  static { init() }\n}\n", "out": "class A extends B{static x=1\n#p=2\nget v(){return this.#p}\nset v(n){this.#p=n}\nstatic async*gen(){yield 1}[k](){}\nstatic{init()}}"}, {"label": "a field named get, then a computed method", "source": "class A {\n  get\n  x() {}\n}\n", "out": "class A{get\nx(){}}"}, {"label": "async and await", "source": "async function f() { await g(); for await (const x of y) {} }\nvar h = async () => await k()\nvar i = async x => x\n", "out": "async function f(){await g();for await(const x of y){}}\nvar h=async()=>await k()\nvar i=async x=>x"}, {"label": "generators", "source": "function* g() { yield\n 1; yield* other(); const x = yield y }\n", "out": "function*g(){yield\n1;yield*other();const x=yield y}"}, {"label": "arrow functions", "source": "var f = (a, b) =>\n  a + b\nvar g = x => ({ x })\nvar h = () => {}\n", "out": "var f=(a,b)=>a+b\nvar g=x=>({x})\nvar h=()=>{}"}, {"label": "labels", "source": "outer: for (;;) { for (;;) { break outer } }\nlbl: { break lbl }\n", "out": "outer:for(;;){for(;;){break outer}}\nlbl:{break lbl}"}, {"label": "break and continue on their own lines", "source": "for (;;) { break\n  foo() }\nfor (;;) { continue\n  bar() }\n", "out": "for(;;){break\nfoo()}\nfor(;;){continue\nbar()}"}, {"label": "destructuring and spread", "source": "var { a, b: [c, ...d], ...e } = f\nvar g = [...h, ...i]\nfunction j(k = 1, ...l) {}\n", "out": "var{a,b:[c,...d],...e}=f\nvar g=[...h,...i]\nfunction j(k=1,...l){}"}, {"label": "nullish and exponent", "source": "a ??= b\nc ||= d\ne &&= f\nvar g = h ** 2 ** 3\n", "out": "a??=b\nc||=d\ne&&=f\nvar g=h**2**3"}, {"label": "optional catch binding", "source": "try { a() } catch { b() } finally { c() }\n", "out": "try{a()}catch{b()}finally{c()}"}, {"label": "unicode names", "source": "var \u00e9 = 1, \u0c24 = 2, \\u0061bc = 3\n", "out": "var \u00e9=1,\u0c24=2,\\u0061bc=3"}, {"label": "object literals", "source": "var o = {\n  a: 1,\n  'b-c': 2,\n  3: 4,\n  get g() { return 1 },\n  set g(v) {},\n  [k]: 5,\n  async m() {},\n  *n() {}\n}\n", "out": "var o={a:1,'b-c':2,3:4,get g(){return 1},set g(v){},[k]:5,async m(){},*n(){}}"}, {"label": "a directive", "source": "'use strict'\nfoo()\n", "out": "'use strict'\nfoo()"}, {"label": "a shebang", "source": "#!/usr/bin/env  node  -r  x\nfoo()\n", "out": "#!/usr/bin/env  node  -r  x\nfoo()"}, {"label": "getters and setters as names", "source": "var get = 1, set = 2\nget\n(x)\n", "out": "var get=1,set=2\nget\n(x)"}, {"label": "the identifier of", "source": "var of = 1\nof\n++x\n", "out": "var of=1\nof\n++x"}, {"label": "comma and sequence", "source": "a = (b, c)\nfor (;;) d(), e()\n", "out": "a=(b,c)\nfor(;;)d(),e()"}, {"label": "object at the start of a line", "source": "x = {\n  a: 1\n}\nfoo()\n", "out": "x={a:1}\nfoo()"}, {"label": "empty statements", "source": ";;; if (a) ; else ;\n", "out": ";;;if(a);else;"}, {"label": "in operator inside for", "source": "for (var i = (a in b); i < 1; i++) {}\n", "out": "for(var i=(a in b);i<1;i++){}"}, {"label": "the void and comma operators", "source": "void 0, void (0)\n", "out": "void 0,void(0)"}, {"label": "long chains", "source": "a.b.c\n  .d(e)\n  [f]\n  (g)\n", "out": "a.b.c.d(e)[f](g)"}, {"label": "new with and without arguments", "source": "new Foo\nnew Foo(1)\nnew (foo())()\nnew new X()()\n", "out": "new Foo\nnew Foo(1)\nnew(foo())()\nnew new X()()"}, {"label": "getter on a number", "source": "0..a\n1.0.a\n", "out": "0..a\n1.0.a"}, {"label": "an HTML-ish comparison", "source": "if (a < !b) {}\nif (a<!c) {}\n", "out": "if(a< !b){}\nif(a< !c){}"}, {"label": "decrement then greater", "source": "if (a-- > b) {}\n", "out": "if(a-->b){}"}, {"label": "big real-looking snippet", "source": "(function (root, factory) {\n  if (typeof define === 'function' && define.amd) define([], factory)\n  else root.lib = factory()\n}(this, function () {\n  'use strict'\n  var x = 1 /* one */\n  function y() { return x / 2 }\n  return { y: y }\n}))\n", "out": "(function(root,factory){if(typeof define==='function'&&define.amd)define([],factory)\nelse root.lib=factory()}(this,function(){'use strict'\nvar x=1\nfunction y(){return x/2}\nreturn{y:y}}))"}];

  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  window.__copied = [];
  window.copyText = function (text) { window.__copied.push(text); };

  /* The page's policy has no unsafe-eval, so a program cannot be compiled from
     here to see whether it is valid. That was acorn's job, done in Node on
     thousands of programs; and every tool page on the site is later run, in
     this browser, with its script minified by this page. */
  function say(source) { set("input", source); }

  /* ================= the page as it opens ================= */
  var first = out();
  ok("the sample is minified on load", first.length > 0);
  eq("the licence notice comes first, on its own line", first.split("\n")[0], "/*! demo 1.0 | MIT */");
  ok("the ordinary comments are gone", first.indexOf("Add up") < 0 && first.indexOf("price times") < 0);
  has("the regular expression keeps its backslash and its flag", first, "/\\s+/g");
  has("the template literal is untouched, expression and all", first, "`Total: ${ total([{ price: 5, qty: 2 }]) }`");
  ok("the result is shorter", first.length < val("input").length);
  say(first);
  eq("minifying the minified text changes nothing", out(), first);
  say(val("input"));

  /* ================= the numbers ================= */
  say("var  a  =  1;  // c");
  eq("spaces and the comment go", out(), "var a=1;");
  var before = new TextEncoder().encode("var  a  =  1;  // c").length;
  var after = new TextEncoder().encode("var a=1;").length;
  eq("the before tile counts bytes", txt("sBefore"), formatBytes(before));
  eq("the after tile counts bytes", txt("sAfter"), formatBytes(after));
  eq("the saving is worked out from them", txt("sSaved"), formatNumber((before - after) / before * 100, 1) + "%");
  has("the message says nothing was renamed", txt("msg"), "Nothing was renamed");

  /* Sizes are in bytes, not characters: a Telugu letter is three of them. */
  var teluguSource = "var t = 'a'; // " + String.fromCodePoint(0x0C05, 0x0C06);
  say(teluguSource);
  eq("a comment in Telugu goes", out(), "var t='a';");
  eq("and the before tile counts its bytes, not its letters", txt("sBefore"),
     formatBytes(new TextEncoder().encode(teluguSource).length));
  ok("which is not the letter count", new TextEncoder().encode(teluguSource).length !== teluguSource.length);

  /* ================= every trap case, locked ================= */
  var unstable = 0;
  CASES.forEach(function (c) {
    say(c.source);
    eq(c.label, out(), c.out);
    say(out());
    if (out() !== c.out) { unstable++; ok(c.label + ": minifying the output changes nothing", false, out()); }
  });
  eq("minifying any of those outputs again changes nothing", unstable, 0);

  /* ================= licence notices ================= */
  say("/*! keep me */\nvar a = 1 /* drop me */");
  eq("a /*! notice at the top is kept, the ordinary comment is not", out(), "/*! keep me */\nvar a=1");
  say("/** @license MIT */\nvar a = 1");
  eq("an @license comment is kept", out(), "/** @license MIT */\nvar a=1");
  say("/** @preserve x */\nvar a = 1");
  eq("and so is @preserve", out(), "/** @preserve x */\nvar a=1");
  say("var a = 1 /*! in the middle of a line */ + 2");
  eq("a notice in the middle of a line is dropped, so no statement can move", out(), "var a=1+2");
  say("var a = 1\n/*! on its own line */\nvar b = 2");
  eq("one on its own line, between statements, is kept", out(), "var a=1\n/*! on its own line */\nvar b=2");
  say("#!/usr/bin/env  node  -r  x\nfoo()");
  eq("a shebang line is kept", out(), "#!/usr/bin/env  node  -r  x\nfoo()");
  say("/*! keep me */\nvar a = 1");
  tick("optLicense", false);
  eq("with the option off the notice goes", out(), "var a=1");
  tick("optLicense", true);
  eq("and back on it returns", out(), "/*! keep me */\nvar a=1");

  /* ================= problems in the input ================= */
  say("var s = \"never closed\nvar t = 2");
  has("an unclosed string is named", txt("msg"), "This string is never closed");
  has("with its line", txt("msg"), "line 1");
  eq("and there is no output", out(), "");
  eq("and the tiles are blank", txt("sBefore") + txt("sAfter") + txt("sSaved"), DASH + DASH + DASH);
  say("var a = 1\nvar b = `open\nstill open");
  has("an unclosed template is named", txt("msg"), "template literal is never closed");
  has("with its own line", txt("msg"), "line 2");
  say("var a = 1\nvar b = 2\n/* never ends");
  has("an unclosed comment is named", txt("msg"), "This comment is never closed");
  has("with its line", txt("msg"), "line 3");
  say("var s = `a ${ b ` c");
  has("an unclosed expression inside a template is caught", txt("msg"), "never closed");
  say("var a = 1");
  eq("fixing it brings the output back", out(), "var a=1");
  say("");
  has("nothing typed asks for something", txt("msg"), "Paste some JavaScript");
  eq("with blank tiles", txt("sBefore") + txt("sAfter") + txt("sSaved"), DASH + DASH + DASH);
  say("   \n\t  ");
  has("only whitespace is treated as nothing", txt("msg"), "Paste some JavaScript");

  /* ================= names that are also object properties ================= */
  say("var constructor = 1\nvar __proto__ = 2\nvar hasOwnProperty = 3\nconstructor\n(x)");
  eq("words like constructor and __proto__ are ordinary names", out(),
     "var constructor=1\nvar __proto__=2\nvar hasOwnProperty=3\nconstructor(x)");

  /* ================= a big program ================= */
  var piece = "function f(a, b) {\n  // add\n  var c = a + b;   /* sum */\n  return c * 2;\n}\n";
  var big = "";
  for (var i = 0; i < 3000; i++) { big += piece.replace(/f\(/, "f" + i + "("); }
  say(big);
  ok("a big program is minified", out().length > 1000 && out().length < big.length * 0.75,
     out().length + " of " + big.length);
  eq("with all 3000 functions still in it", (out().match(/function f\d+/g) || []).length, 3000);
  eq("and each comment gone", out().indexOf("//") + out().indexOf("/*"), -2);

  /* ================= hostile text ================= */
  say("<iframe onload=zq>");
  eq("markup typed into the box comes out as text", out(), "<iframe onload=zq>");
  eq("and makes no element", document.querySelectorAll("iframe").length, 0);
  say("var a = 1");

  /* ================= copying, saving and clearing ================= */
  click("copyBtn");
  eq("copy hands over the minified text", window.__copied[window.__copied.length - 1], "var a=1");
  click("dlBtn");
  eq("download hands over one file", window.__saved.length, 1);
  eq("named for a minified script", window.__saved[0].name, "script.min.js");
  eq("as JavaScript", window.__saved[0].blob.type, "text/javascript");
  window.__saved[0].blob.text().then(function (text) {
    eq("holding the minified text", text, "var a=1");

    waitFor("the gzipped sizes appear",
      function () { return txt("sGzBefore") !== DASH && txt("sGzAfter") !== DASH; },
      function () {
        ok("the gzipped sizes are real sizes or a plain n/a",
           /^(n\/a|[0-9.,]+ ?[A-Za-z]*)$/.test(txt("sGzBefore")) && /^(n\/a|[0-9.,]+ ?[A-Za-z]*)$/.test(txt("sGzAfter")),
           txt("sGzBefore") + " / " + txt("sGzAfter"));

        click("clearBtn");
        eq("clear empties the box", val("input"), "");
        eq("and the output", out(), "");
        has("and asks for something", txt("msg"), "Paste some JavaScript");
        click("copyBtn");
        click("dlBtn");
        eq("with nothing to copy, nothing is copied", window.__copied.length, 1);
        eq("and nothing is saved", window.__saved.length, 1);
        finish();
      });
  });
"""
# ---- END: the test for js-minifier ----

# ---- START: the test for markdown-previewer ----
T["markdown-previewer"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var GOLDENS = [["\tfoo\tbaz\t\tbim","\u003cpre>\u003ccode>foo\tbaz\t\tbim\n\u003c/code>\u003c/pre>\n"],["  \tfoo\tbaz\t\tbim","\u003cpre>\u003ccode>foo\tbaz\t\tbim\n\u003c/code>\u003c/pre>\n"],["    a\ta\n    \u1f50\ta","\u003cpre>\u003ccode>a\ta\n\u1f50\ta\n\u003c/code>\u003c/pre>\n"],["  - foo\n\n\tbar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- foo\n\n\t\tbar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>  bar\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ul>\n"],[">\t\tfoo","\u003cblockquote>\n\u003cpre>\u003ccode>  foo\n\u003c/code>\u003c/pre>\n\u003c/blockquote>\n"],["-\t\tfoo","\u003cul>\n\u003cli>\n\u003cpre>\u003ccode>  foo\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ul>\n"],["    foo\n\tbar","\u003cpre>\u003ccode>foo\nbar\n\u003c/code>\u003c/pre>\n"],[" - foo\n   - bar\n\t - baz","\u003cul>\n\u003cli>foo\n\u003cul>\n\u003cli>bar\n\u003cul>\n\u003cli>baz\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["#\tFoo","\u003ch1>Foo\u003c/h1>\n"],["*\t*\t*\t","\u003chr>\n"],["- `one\n- two`","\u003cul>\n\u003cli>`one\u003c/li>\n\u003cli>two`\u003c/li>\n\u003c/ul>\n"],["***\n---\n___","\u003chr>\n\u003chr>\n\u003chr>\n"],["+++","\u003cp>+++\u003c/p>\n"],["===","\u003cp>===\u003c/p>\n"],["--\n**\n__","\u003cp>--\n**\n__\u003c/p>\n"],[" ***\n  ***\n   ***","\u003chr>\n\u003chr>\n\u003chr>\n"],["    ***","\u003cpre>\u003ccode>***\n\u003c/code>\u003c/pre>\n"],["Foo\n    ***","\u003cp>Foo\n***\u003c/p>\n"],["_____________________________________","\u003chr>\n"],[" - - -","\u003chr>\n"],[" **  * ** * ** * **","\u003chr>\n"],["-     -      -      -","\u003chr>\n"],["- - - -    ","\u003chr>\n"],["_ _ _ _ a\n\na------\n\n---a---","\u003cp>_ _ _ _ a\u003c/p>\n\u003cp>a------\u003c/p>\n\u003cp>---a---\u003c/p>\n"],[" *-*","\u003cp>\u003cem>-\u003c/em>\u003c/p>\n"],["- foo\n***\n- bar","\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n\u003chr>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n"],["Foo\n***\nbar","\u003cp>Foo\u003c/p>\n\u003chr>\n\u003cp>bar\u003c/p>\n"],["Foo\n---\nbar","\u003ch2>Foo\u003c/h2>\n\u003cp>bar\u003c/p>\n"],["* Foo\n* * *\n* Bar","\u003cul>\n\u003cli>Foo\u003c/li>\n\u003c/ul>\n\u003chr>\n\u003cul>\n\u003cli>Bar\u003c/li>\n\u003c/ul>\n"],["- Foo\n- * * *","\u003cul>\n\u003cli>Foo\u003c/li>\n\u003cli>\n\u003chr>\n\u003c/li>\n\u003c/ul>\n"],["# foo\n## foo\n### foo\n#### foo\n##### foo\n###### foo","\u003ch1>foo\u003c/h1>\n\u003ch2>foo\u003c/h2>\n\u003ch3>foo\u003c/h3>\n\u003ch4>foo\u003c/h4>\n\u003ch5>foo\u003c/h5>\n\u003ch6>foo\u003c/h6>\n"],["####### foo","\u003cp>####### foo\u003c/p>\n"],["#5 bolt\n\n#hashtag","\u003cp>#5 bolt\u003c/p>\n\u003cp>#hashtag\u003c/p>\n"],["\\## foo","\u003cp>## foo\u003c/p>\n"],["# foo *bar* \\*baz\\*","\u003ch1>foo \u003cem>bar\u003c/em> *baz*\u003c/h1>\n"],["#                  foo                     ","\u003ch1>foo\u003c/h1>\n"],[" ### foo\n  ## foo\n   # foo","\u003ch3>foo\u003c/h3>\n\u003ch2>foo\u003c/h2>\n\u003ch1>foo\u003c/h1>\n"],["    # foo","\u003cpre>\u003ccode># foo\n\u003c/code>\u003c/pre>\n"],["foo\n    # bar","\u003cp>foo\n# bar\u003c/p>\n"],["## foo ##\n  ###   bar    ###","\u003ch2>foo\u003c/h2>\n\u003ch3>bar\u003c/h3>\n"],["# foo ##################################\n##### foo ##","\u003ch1>foo\u003c/h1>\n\u003ch5>foo\u003c/h5>\n"],["### foo ###     ","\u003ch3>foo\u003c/h3>\n"],["### foo ### b","\u003ch3>foo ### b\u003c/h3>\n"],["# foo#","\u003ch1>foo#\u003c/h1>\n"],["### foo \\###\n## foo #\\##\n# foo \\#","\u003ch3>foo ###\u003c/h3>\n\u003ch2>foo ###\u003c/h2>\n\u003ch1>foo #\u003c/h1>\n"],["****\n## foo\n****","\u003chr>\n\u003ch2>foo\u003c/h2>\n\u003chr>\n"],["Foo bar\n# baz\nBar foo","\u003cp>Foo bar\u003c/p>\n\u003ch1>baz\u003c/h1>\n\u003cp>Bar foo\u003c/p>\n"],["## \n#\n### ###","\u003ch2>\u003c/h2>\n\u003ch1>\u003c/h1>\n\u003ch3>\u003c/h3>\n"],["Foo *bar*\n=========\n\nFoo *bar*\n---------","\u003ch1>Foo \u003cem>bar\u003c/em>\u003c/h1>\n\u003ch2>Foo \u003cem>bar\u003c/em>\u003c/h2>\n"],["Foo *bar\nbaz*\n====","\u003ch1>Foo \u003cem>bar\nbaz\u003c/em>\u003c/h1>\n"],["  Foo *bar\nbaz*\t\n====","\u003ch1>Foo \u003cem>bar\nbaz\u003c/em>\u003c/h1>\n"],["Foo\n-------------------------\n\nFoo\n=","\u003ch2>Foo\u003c/h2>\n\u003ch1>Foo\u003c/h1>\n"],["   Foo\n---\n\n  Foo\n-----\n\n  Foo\n  ===","\u003ch2>Foo\u003c/h2>\n\u003ch2>Foo\u003c/h2>\n\u003ch1>Foo\u003c/h1>\n"],["    Foo\n    ---\n\n    Foo\n---","\u003cpre>\u003ccode>Foo\n---\n\nFoo\n\u003c/code>\u003c/pre>\n\u003chr>\n"],["Foo\n   ----      ","\u003ch2>Foo\u003c/h2>\n"],["Foo\n    ---","\u003cp>Foo\n---\u003c/p>\n"],["Foo\n= =\n\nFoo\n--- -","\u003cp>Foo\n= =\u003c/p>\n\u003cp>Foo\u003c/p>\n\u003chr>\n"],["Foo  \n-----","\u003ch2>Foo\u003c/h2>\n"],["Foo\\\n----","\u003ch2>Foo\\\u003c/h2>\n"],["> Foo\n---","\u003cblockquote>\n\u003cp>Foo\u003c/p>\n\u003c/blockquote>\n\u003chr>\n"],["> foo\nbar\n===","\u003cblockquote>\n\u003cp>foo\nbar\n===\u003c/p>\n\u003c/blockquote>\n"],["- Foo\n---","\u003cul>\n\u003cli>Foo\u003c/li>\n\u003c/ul>\n\u003chr>\n"],["Foo\nBar\n---","\u003ch2>Foo\nBar\u003c/h2>\n"],["---\nFoo\n---\nBar\n---\nBaz","\u003chr>\n\u003ch2>Foo\u003c/h2>\n\u003ch2>Bar\u003c/h2>\n\u003cp>Baz\u003c/p>\n"],["\n====","\u003cp>====\u003c/p>\n"],["---\n---","\u003chr>\n\u003chr>\n"],["- foo\n-----","\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n\u003chr>\n"],["    foo\n---","\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003chr>\n"],["> foo\n-----","\u003cblockquote>\n\u003cp>foo\u003c/p>\n\u003c/blockquote>\n\u003chr>\n"],["\\> foo\n------","\u003ch2>&gt; foo\u003c/h2>\n"],["Foo\n\nbar\n---\nbaz","\u003cp>Foo\u003c/p>\n\u003ch2>bar\u003c/h2>\n\u003cp>baz\u003c/p>\n"],["Foo\nbar\n\n---\n\nbaz","\u003cp>Foo\nbar\u003c/p>\n\u003chr>\n\u003cp>baz\u003c/p>\n"],["Foo\nbar\n* * *\nbaz","\u003cp>Foo\nbar\u003c/p>\n\u003chr>\n\u003cp>baz\u003c/p>\n"],["Foo\nbar\n\\---\nbaz","\u003cp>Foo\nbar\n---\nbaz\u003c/p>\n"],["    a simple\n      indented code block","\u003cpre>\u003ccode>a simple\n  indented code block\n\u003c/code>\u003c/pre>\n"],["  - foo\n\n    bar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["1.  foo\n\n    - bar","\u003col>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ol>\n"],["    \u003ca/>\n    *hi*\n\n    - one","\u003cpre>\u003ccode>&lt;a/&gt;\n*hi*\n\n- one\n\u003c/code>\u003c/pre>\n"],["    chunk1\n\n    chunk2\n  \n \n \n    chunk3","\u003cpre>\u003ccode>chunk1\n\nchunk2\n\n\n\nchunk3\n\u003c/code>\u003c/pre>\n"],["    chunk1\n      \n      chunk2","\u003cpre>\u003ccode>chunk1\n  \n  chunk2\n\u003c/code>\u003c/pre>\n"],["Foo\n    bar","\u003cp>Foo\nbar\u003c/p>\n"],["    foo\nbar","\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003cp>bar\u003c/p>\n"],["# Heading\n    foo\nHeading\n------\n    foo\n----","\u003ch1>Heading\u003c/h1>\n\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003ch2>Heading\u003c/h2>\n\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003chr>\n"],["        foo\n    bar","\u003cpre>\u003ccode>    foo\nbar\n\u003c/code>\u003c/pre>\n"],["\n    \n    foo\n    ","\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n"],["    foo  ","\u003cpre>\u003ccode>foo  \n\u003c/code>\u003c/pre>\n"],["```\n\u003c\n >\n```","\u003cpre>\u003ccode>&lt;\n &gt;\n\u003c/code>\u003c/pre>\n"],["~~~\n\u003c\n >\n~~~","\u003cpre>\u003ccode>&lt;\n &gt;\n\u003c/code>\u003c/pre>\n"],["``\nfoo\n``","\u003cp>\u003ccode>foo\u003c/code>\u003c/p>\n"],["```\naaa\n~~~\n```","\u003cpre>\u003ccode>aaa\n~~~\n\u003c/code>\u003c/pre>\n"],["~~~\naaa\n```\n~~~","\u003cpre>\u003ccode>aaa\n```\n\u003c/code>\u003c/pre>\n"],["````\naaa\n```\n``````","\u003cpre>\u003ccode>aaa\n```\n\u003c/code>\u003c/pre>\n"],["~~~~\naaa\n~~~\n~~~~","\u003cpre>\u003ccode>aaa\n~~~\n\u003c/code>\u003c/pre>\n"],["```","\u003cpre>\u003ccode>\u003c/code>\u003c/pre>\n"],["`````\n\n```\naaa","\u003cpre>\u003ccode>\n```\naaa\u003c/code>\u003c/pre>\n"],["> ```\n> aaa\n\nbbb","\u003cblockquote>\n\u003cpre>\u003ccode>aaa\n\u003c/code>\u003c/pre>\n\u003c/blockquote>\n\u003cp>bbb\u003c/p>\n"],["```\n\n  \n```","\u003cpre>\u003ccode>\n  \n\u003c/code>\u003c/pre>\n"],["```\n```","\u003cpre>\u003ccode>\u003c/code>\u003c/pre>\n"],[" ```\n aaa\naaa\n```","\u003cpre>\u003ccode>aaa\naaa\n\u003c/code>\u003c/pre>\n"],["  ```\naaa\n  aaa\naaa\n  ```","\u003cpre>\u003ccode>aaa\naaa\naaa\n\u003c/code>\u003c/pre>\n"],["   ```\n   aaa\n    aaa\n  aaa\n   ```","\u003cpre>\u003ccode>aaa\n aaa\naaa\n\u003c/code>\u003c/pre>\n"],["    ```\n    aaa\n    ```","\u003cpre>\u003ccode>```\naaa\n```\n\u003c/code>\u003c/pre>\n"],["```\naaa\n  ```","\u003cpre>\u003ccode>aaa\n\u003c/code>\u003c/pre>\n"],["   ```\naaa\n  ```","\u003cpre>\u003ccode>aaa\n\u003c/code>\u003c/pre>\n"],["```\naaa\n    ```","\u003cpre>\u003ccode>aaa\n    ```\u003c/code>\u003c/pre>\n"],["``` ```\naaa","\u003cp>\u003ccode> \u003c/code>\naaa\u003c/p>\n"],["~~~~~~\naaa\n~~~ ~~","\u003cpre>\u003ccode>aaa\n~~~ ~~\u003c/code>\u003c/pre>\n"],["foo\n```\nbar\n```\nbaz","\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003cp>baz\u003c/p>\n"],["foo\n---\n~~~\nbar\n~~~\n# baz","\u003ch2>foo\u003c/h2>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003ch1>baz\u003c/h1>\n"],["```ruby\ndef foo(x)\n  return 3\nend\n```","\u003cpre>\u003ccode class=\"language-ruby\">def foo(x)\n  return 3\nend\n\u003c/code>\u003c/pre>\n"],["~~~~    ruby startline=3 $%@#$\ndef foo(x)\n  return 3\nend\n~~~~~~~","\u003cpre>\u003ccode class=\"language-ruby\">def foo(x)\n  return 3\nend\n\u003c/code>\u003c/pre>\n"],["````;\n````","\u003cpre>\u003ccode class=\"language-;\">\u003c/code>\u003c/pre>\n"],["``` aa ```\nfoo","\u003cp>\u003ccode>aa\u003c/code>\nfoo\u003c/p>\n"],["~~~ aa ``` ~~~\nfoo\n~~~","\u003cpre>\u003ccode class=\"language-aa\">foo\n\u003c/code>\u003c/pre>\n"],["```\n``` aaa\n```","\u003cpre>\u003ccode>``` aaa\n\u003c/code>\u003c/pre>\n"],["aaa\n\nbbb","\u003cp>aaa\u003c/p>\n\u003cp>bbb\u003c/p>\n"],["aaa\nbbb\n\nccc\nddd","\u003cp>aaa\nbbb\u003c/p>\n\u003cp>ccc\nddd\u003c/p>\n"],["aaa\n\n\nbbb","\u003cp>aaa\u003c/p>\n\u003cp>bbb\u003c/p>\n"],["  aaa\n bbb","\u003cp>aaa\nbbb\u003c/p>\n"],["aaa\n             bbb\n                                       ccc","\u003cp>aaa\nbbb\nccc\u003c/p>\n"],["   aaa\nbbb","\u003cp>aaa\nbbb\u003c/p>\n"],["    aaa\nbbb","\u003cpre>\u003ccode>aaa\n\u003c/code>\u003c/pre>\n\u003cp>bbb\u003c/p>\n"],["aaa     \nbbb     ","\u003cp>aaa\u003cbr>\nbbb\u003c/p>\n"],["  \n\naaa\n  \n\n# aaa\n\n  ","\u003cp>aaa\u003c/p>\n\u003ch1>aaa\u003c/h1>\n"],["> # Foo\n> bar\n> baz","\u003cblockquote>\n\u003ch1>Foo\u003c/h1>\n\u003cp>bar\nbaz\u003c/p>\n\u003c/blockquote>\n"],["># Foo\n>bar\n> baz","\u003cblockquote>\n\u003ch1>Foo\u003c/h1>\n\u003cp>bar\nbaz\u003c/p>\n\u003c/blockquote>\n"],["   > # Foo\n   > bar\n > baz","\u003cblockquote>\n\u003ch1>Foo\u003c/h1>\n\u003cp>bar\nbaz\u003c/p>\n\u003c/blockquote>\n"],["    > # Foo\n    > bar\n    > baz","\u003cpre>\u003ccode>&gt; # Foo\n&gt; bar\n&gt; baz\n\u003c/code>\u003c/pre>\n"],["> # Foo\n> bar\nbaz","\u003cblockquote>\n\u003ch1>Foo\u003c/h1>\n\u003cp>bar\nbaz\u003c/p>\n\u003c/blockquote>\n"],["> bar\nbaz\n> foo","\u003cblockquote>\n\u003cp>bar\nbaz\nfoo\u003c/p>\n\u003c/blockquote>\n"],["> foo\n---","\u003cblockquote>\n\u003cp>foo\u003c/p>\n\u003c/blockquote>\n\u003chr>\n"],["> - foo\n- bar","\u003cblockquote>\n\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n\u003c/blockquote>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n"],[">     foo\n    bar","\u003cblockquote>\n\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003c/blockquote>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n"],["> ```\nfoo\n```","\u003cblockquote>\n\u003cpre>\u003ccode>\u003c/code>\u003c/pre>\n\u003c/blockquote>\n\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>\u003c/code>\u003c/pre>\n"],["> foo\n    - bar","\u003cblockquote>\n\u003cp>foo\n- bar\u003c/p>\n\u003c/blockquote>\n"],[">","\u003cblockquote>\n\u003c/blockquote>\n"],[">\n>  \n> ","\u003cblockquote>\n\u003c/blockquote>\n"],[">\n> foo\n>  ","\u003cblockquote>\n\u003cp>foo\u003c/p>\n\u003c/blockquote>\n"],["> foo\n\n> bar","\u003cblockquote>\n\u003cp>foo\u003c/p>\n\u003c/blockquote>\n\u003cblockquote>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n"],["> foo\n> bar","\u003cblockquote>\n\u003cp>foo\nbar\u003c/p>\n\u003c/blockquote>\n"],["> foo\n>\n> bar","\u003cblockquote>\n\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n"],["foo\n> bar","\u003cp>foo\u003c/p>\n\u003cblockquote>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n"],["> aaa\n***\n> bbb","\u003cblockquote>\n\u003cp>aaa\u003c/p>\n\u003c/blockquote>\n\u003chr>\n\u003cblockquote>\n\u003cp>bbb\u003c/p>\n\u003c/blockquote>\n"],["> bar\nbaz","\u003cblockquote>\n\u003cp>bar\nbaz\u003c/p>\n\u003c/blockquote>\n"],["> bar\n\nbaz","\u003cblockquote>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n\u003cp>baz\u003c/p>\n"],["> bar\n>\nbaz","\u003cblockquote>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n\u003cp>baz\u003c/p>\n"],["> > > foo\nbar","\u003cblockquote>\n\u003cblockquote>\n\u003cblockquote>\n\u003cp>foo\nbar\u003c/p>\n\u003c/blockquote>\n\u003c/blockquote>\n\u003c/blockquote>\n"],[">>> foo\n> bar\n>>baz","\u003cblockquote>\n\u003cblockquote>\n\u003cblockquote>\n\u003cp>foo\nbar\nbaz\u003c/p>\n\u003c/blockquote>\n\u003c/blockquote>\n\u003c/blockquote>\n"],[">     code\n\n>    not code","\u003cblockquote>\n\u003cpre>\u003ccode>code\n\u003c/code>\u003c/pre>\n\u003c/blockquote>\n\u003cblockquote>\n\u003cp>not code\u003c/p>\n\u003c/blockquote>\n"],["A paragraph\nwith two lines.\n\n    indented code\n\n> A block quote.","\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n"],["1.  A paragraph\n    with two lines.\n\n        indented code\n\n    > A block quote.","\u003col>\n\u003cli>\n\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["- one\n\n two","\u003cul>\n\u003cli>one\u003c/li>\n\u003c/ul>\n\u003cp>two\u003c/p>\n"],["- one\n\n  two","\u003cul>\n\u003cli>\n\u003cp>one\u003c/p>\n\u003cp>two\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],[" -    one\n\n     two","\u003cul>\n\u003cli>one\u003c/li>\n\u003c/ul>\n\u003cpre>\u003ccode> two\n\u003c/code>\u003c/pre>\n"],[" -    one\n\n      two","\u003cul>\n\u003cli>\n\u003cp>one\u003c/p>\n\u003cp>two\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["   > > 1.  one\n>>\n>>     two","\u003cblockquote>\n\u003cblockquote>\n\u003col>\n\u003cli>\n\u003cp>one\u003c/p>\n\u003cp>two\u003c/p>\n\u003c/li>\n\u003c/ol>\n\u003c/blockquote>\n\u003c/blockquote>\n"],[">>- one\n>>\n  >  > two","\u003cblockquote>\n\u003cblockquote>\n\u003cul>\n\u003cli>one\u003c/li>\n\u003c/ul>\n\u003cp>two\u003c/p>\n\u003c/blockquote>\n\u003c/blockquote>\n"],["-one\n\n2.two","\u003cp>-one\u003c/p>\n\u003cp>2.two\u003c/p>\n"],["- foo\n\n\n  bar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["1.  foo\n\n    ```\n    bar\n    ```\n\n    baz\n\n    > bam","\u003col>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003cp>baz\u003c/p>\n\u003cblockquote>\n\u003cp>bam\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["- Foo\n\n      bar\n\n\n      baz","\u003cul>\n\u003cli>\n\u003cp>Foo\u003c/p>\n\u003cpre>\u003ccode>bar\n\n\nbaz\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ul>\n"],["123456789. ok","\u003col start=\"123456789\">\n\u003cli>ok\u003c/li>\n\u003c/ol>\n"],["1234567890. not ok","\u003cp>1234567890. not ok\u003c/p>\n"],["0. ok","\u003col start=\"0\">\n\u003cli>ok\u003c/li>\n\u003c/ol>\n"],["003. ok","\u003col start=\"3\">\n\u003cli>ok\u003c/li>\n\u003c/ol>\n"],["-1. not ok","\u003cp>-1. not ok\u003c/p>\n"],["- foo\n\n      bar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ul>\n"],["  10.  foo\n\n           bar","\u003col start=\"10\">\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ol>\n"],["    indented code\n\nparagraph\n\n    more code","\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cp>paragraph\u003c/p>\n\u003cpre>\u003ccode>more code\n\u003c/code>\u003c/pre>\n"],["1.     indented code\n\n   paragraph\n\n       more code","\u003col>\n\u003cli>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cp>paragraph\u003c/p>\n\u003cpre>\u003ccode>more code\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ol>\n"],["1.      indented code\n\n   paragraph\n\n       more code","\u003col>\n\u003cli>\n\u003cpre>\u003ccode> indented code\n\u003c/code>\u003c/pre>\n\u003cp>paragraph\u003c/p>\n\u003cpre>\u003ccode>more code\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ol>\n"],["   foo\n\nbar","\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n"],["-    foo\n\n  bar","\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n\u003cp>bar\u003c/p>\n"],["-  foo\n\n   bar","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["-\n  foo\n-\n  ```\n  bar\n  ```\n-\n      baz","\u003cul>\n\u003cli>foo\u003c/li>\n\u003cli>\n\u003cpre>\u003ccode>bar\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003cli>\n\u003cpre>\u003ccode>baz\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003c/ul>\n"],["-   \n  foo","\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n"],["-\n\n  foo","\u003cul>\n\u003cli>\u003c/li>\n\u003c/ul>\n\u003cp>foo\u003c/p>\n"],["- foo\n-\n- bar","\u003cul>\n\u003cli>foo\u003c/li>\n\u003cli>\u003c/li>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n"],["- foo\n-   \n- bar","\u003cul>\n\u003cli>foo\u003c/li>\n\u003cli>\u003c/li>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n"],["1. foo\n2.\n3. bar","\u003col>\n\u003cli>foo\u003c/li>\n\u003cli>\u003c/li>\n\u003cli>bar\u003c/li>\n\u003c/ol>\n"],["*","\u003cul>\n\u003cli>\u003c/li>\n\u003c/ul>\n"],["foo\n*\n\nfoo\n1.","\u003cp>foo\n*\u003c/p>\n\u003cp>foo\n1.\u003c/p>\n"],[" 1.  A paragraph\n     with two lines.\n\n         indented code\n\n     > A block quote.","\u003col>\n\u003cli>\n\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["  1.  A paragraph\n      with two lines.\n\n          indented code\n\n      > A block quote.","\u003col>\n\u003cli>\n\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["   1.  A paragraph\n       with two lines.\n\n           indented code\n\n       > A block quote.","\u003col>\n\u003cli>\n\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["    1.  A paragraph\n        with two lines.\n\n            indented code\n\n        > A block quote.","\u003cpre>\u003ccode>1.  A paragraph\n    with two lines.\n\n        indented code\n\n    &gt; A block quote.\n\u003c/code>\u003c/pre>\n"],["  1.  A paragraph\nwith two lines.\n\n          indented code\n\n      > A block quote.","\u003col>\n\u003cli>\n\u003cp>A paragraph\nwith two lines.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cblockquote>\n\u003cp>A block quote.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n"],["  1.  A paragraph\n    with two lines.","\u003col>\n\u003cli>A paragraph\nwith two lines.\u003c/li>\n\u003c/ol>\n"],["> 1. > Blockquote\ncontinued here.","\u003cblockquote>\n\u003col>\n\u003cli>\n\u003cblockquote>\n\u003cp>Blockquote\ncontinued here.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n\u003c/blockquote>\n"],["> 1. > Blockquote\n> continued here.","\u003cblockquote>\n\u003col>\n\u003cli>\n\u003cblockquote>\n\u003cp>Blockquote\ncontinued here.\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ol>\n\u003c/blockquote>\n"],["- foo\n  - bar\n    - baz\n      - boo","\u003cul>\n\u003cli>foo\n\u003cul>\n\u003cli>bar\n\u003cul>\n\u003cli>baz\n\u003cul>\n\u003cli>boo\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["- foo\n - bar\n  - baz\n   - boo","\u003cul>\n\u003cli>foo\u003c/li>\n\u003cli>bar\u003c/li>\n\u003cli>baz\u003c/li>\n\u003cli>boo\u003c/li>\n\u003c/ul>\n"],["10) foo\n    - bar","\u003col start=\"10\">\n\u003cli>foo\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ol>\n"],["10) foo\n   - bar","\u003col start=\"10\">\n\u003cli>foo\u003c/li>\n\u003c/ol>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n"],["- - foo","\u003cul>\n\u003cli>\n\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["1. - 2. foo","\u003col>\n\u003cli>\n\u003cul>\n\u003cli>\n\u003col start=\"2\">\n\u003cli>foo\u003c/li>\n\u003c/ol>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ol>\n"],["- # Foo\n- Bar\n  ---\n  baz","\u003cul>\n\u003cli>\n\u003ch1>Foo\u003c/h1>\n\u003c/li>\n\u003cli>\n\u003ch2>Bar\u003c/h2>\nbaz\u003c/li>\n\u003c/ul>\n"],["- foo\n- bar\n+ baz","\u003cul>\n\u003cli>foo\u003c/li>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n\u003cul>\n\u003cli>baz\u003c/li>\n\u003c/ul>\n"],["1. foo\n2. bar\n3) baz","\u003col>\n\u003cli>foo\u003c/li>\n\u003cli>bar\u003c/li>\n\u003c/ol>\n\u003col start=\"3\">\n\u003cli>baz\u003c/li>\n\u003c/ol>\n"],["Foo\n- bar\n- baz","\u003cp>Foo\u003c/p>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003cli>baz\u003c/li>\n\u003c/ul>\n"],["The number of windows in my house is\n14.  The number of doors is 6.","\u003cp>The number of windows in my house is\n14.  The number of doors is 6.\u003c/p>\n"],["The number of windows in my house is\n1.  The number of doors is 6.","\u003cp>The number of windows in my house is\u003c/p>\n\u003col>\n\u003cli>The number of doors is 6.\u003c/li>\n\u003c/ol>\n"],["- foo\n\n- bar\n\n\n- baz","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>baz\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- foo\n  - bar\n    - baz\n\n\n      bim","\u003cul>\n\u003cli>foo\n\u003cul>\n\u003cli>bar\n\u003cul>\n\u003cli>\n\u003cp>baz\u003c/p>\n\u003cp>bim\u003c/p>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["- a\n - b\n  - c\n   - d\n  - e\n - f\n- g","\u003cul>\n\u003cli>a\u003c/li>\n\u003cli>b\u003c/li>\n\u003cli>c\u003c/li>\n\u003cli>d\u003c/li>\n\u003cli>e\u003c/li>\n\u003cli>f\u003c/li>\n\u003cli>g\u003c/li>\n\u003c/ul>\n"],["1. a\n\n  2. b\n\n   3. c","\u003col>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>c\u003c/p>\n\u003c/li>\n\u003c/ol>\n"],["- a\n - b\n  - c\n   - d\n    - e","\u003cul>\n\u003cli>a\u003c/li>\n\u003cli>b\u003c/li>\n\u003cli>c\u003c/li>\n\u003cli>d\n- e\u003c/li>\n\u003c/ul>\n"],["1. a\n\n  2. b\n\n    3. c","\u003col>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003c/li>\n\u003c/ol>\n\u003cpre>\u003ccode>3. c\n\u003c/code>\u003c/pre>\n"],["- a\n- b\n\n- c","\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>c\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["* a\n*\n\n* c","\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\u003c/li>\n\u003cli>\n\u003cp>c\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- a\n- b\n\n  c\n- d","\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003cp>c\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>d\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- a\n- b\n\n  [ref]: /url\n- d","\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>d\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- a\n- ```\n  b\n\n\n  ```\n- c","\u003cul>\n\u003cli>a\u003c/li>\n\u003cli>\n\u003cpre>\u003ccode>b\n\n\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003cli>c\u003c/li>\n\u003c/ul>\n"],["- a\n  - b\n\n    c\n- d","\u003cul>\n\u003cli>a\n\u003cul>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003cp>c\u003c/p>\n\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003cli>d\u003c/li>\n\u003c/ul>\n"],["* a\n  > b\n  >\n* c","\u003cul>\n\u003cli>a\n\u003cblockquote>\n\u003cp>b\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003cli>c\u003c/li>\n\u003c/ul>\n"],["- a\n  > b\n  ```\n  c\n  ```\n- d","\u003cul>\n\u003cli>a\n\u003cblockquote>\n\u003cp>b\u003c/p>\n\u003c/blockquote>\n\u003cpre>\u003ccode>c\n\u003c/code>\u003c/pre>\n\u003c/li>\n\u003cli>d\u003c/li>\n\u003c/ul>\n"],["- a","\u003cul>\n\u003cli>a\u003c/li>\n\u003c/ul>\n"],["- a\n  - b","\u003cul>\n\u003cli>a\n\u003cul>\n\u003cli>b\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["1. ```\n   foo\n   ```\n\n   bar","\u003col>\n\u003cli>\n\u003cpre>\u003ccode>foo\n\u003c/code>\u003c/pre>\n\u003cp>bar\u003c/p>\n\u003c/li>\n\u003c/ol>\n"],["* foo\n  * bar\n\n  baz","\u003cul>\n\u003cli>\n\u003cp>foo\u003c/p>\n\u003cul>\n\u003cli>bar\u003c/li>\n\u003c/ul>\n\u003cp>baz\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- a\n  - b\n  - c\n\n- d\n  - e\n  - f","\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003cul>\n\u003cli>b\u003c/li>\n\u003cli>c\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003cli>\n\u003cp>d\u003c/p>\n\u003cul>\n\u003cli>e\u003c/li>\n\u003cli>f\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["\\!\\\"\\#\\$\\%\\&\\'\\(\\)\\*\\+\\,\\-\\.\\/\\:\\;\\\u003c\\=\\>\\?\\@\\[\\\\\\]\\^\\_\\`\\{\\|\\}\\~","\u003cp>!&quot;#$%&amp;'()*+,-./:;&lt;=&gt;?@[\\]^_`{|}~\u003c/p>\n"],["\\\t\\A\\a\\ \\3\\\u03c6\\\u00ab","\u003cp>\\\t\\A\\a\\ \\3\\\u03c6\\\u00ab\u003c/p>\n"],["\\*not emphasized*\n\\\u003cbr/> not a tag\n\\[not a link](/foo)\n\\`not code`\n1\\. not a list\n\\* not a list\n\\# not a heading\n\\[foo]: /url \"not a reference\"\n\\&ouml; not a character entity","\u003cp>*not emphasized*\n&lt;br/&gt; not a tag\n[not a link](/foo)\n`not code`\n1. not a list\n* not a list\n# not a heading\n[foo]: /url &quot;not a reference&quot;\n&amp;ouml; not a character entity\u003c/p>\n"],["\\\\*emphasis*","\u003cp>\\\u003cem>emphasis\u003c/em>\u003c/p>\n"],["foo\\\nbar","\u003cp>foo\u003cbr>\nbar\u003c/p>\n"],["`` \\[\\` ``","\u003cp>\u003ccode>\\[\\`\u003c/code>\u003c/p>\n"],["    \\[\\]","\u003cpre>\u003ccode>\\[\\]\n\u003c/code>\u003c/pre>\n"],["~~~\n\\[\\]\n~~~","\u003cpre>\u003ccode>\\[\\]\n\u003c/code>\u003c/pre>\n"],["\u003chttp://example.com?find=\\*>","\u003cp>\u003ca href=\"http://example.com?find=%5C*\">http://example.com?find=\\*\u003c/a>\u003c/p>\n"],["[foo](/bar\\* \"ti\\*tle\")","\u003cp>\u003ca href=\"/bar*\" title=\"ti*tle\">foo\u003c/a>\u003c/p>\n"],["[foo]\n\n[foo]: /bar\\* \"ti\\*tle\"","\u003cp>\u003ca href=\"/bar*\" title=\"ti*tle\">foo\u003c/a>\u003c/p>\n"],["``` foo\\+bar\nfoo\n```","\u003cpre>\u003ccode class=\"language-foo+bar\">foo\n\u003c/code>\u003c/pre>\n"],["&nbsp; &amp; &copy; &AElig; &frac34;","\u003cp>\u00a0 &amp; \u00a9 \u00c6 \u00be\u003c/p>\n"],["&#35; &#1234; &#992; &#0;","\u003cp># \u04d2 \u03e0 \ufffd\u003c/p>\n"],["&#X22; &#XD06; &#xcab;","\u003cp>&quot; \u0d06 \u0cab\u003c/p>\n"],["&nbsp &x; &#; &#x;\n&#87654321;\n&#abcdef0;\n&ThisIsNotDefined; &hi?;","\u003cp>&amp;nbsp &amp;x; &amp;#; &amp;#x;\n&amp;#87654321;\n&amp;#abcdef0;\n&amp;ThisIsNotDefined; &amp;hi?;\u003c/p>\n"],["&copy","\u003cp>&amp;copy\u003c/p>\n"],["&MadeUpEntity;","\u003cp>&amp;MadeUpEntity;\u003c/p>\n"],["[foo](/f&ouml;&ouml; \"f&ouml;&ouml;\")","\u003cp>\u003ca href=\"/f%C3%B6%C3%B6\" title=\"f\u00f6\u00f6\">foo\u003c/a>\u003c/p>\n"],["[foo]\n\n[foo]: /f&ouml;&ouml; \"f&ouml;&ouml;\"","\u003cp>\u003ca href=\"/f%C3%B6%C3%B6\" title=\"f\u00f6\u00f6\">foo\u003c/a>\u003c/p>\n"],["``` f&ouml;&ouml;\nfoo\n```","\u003cpre>\u003ccode class=\"language-f\u00f6\u00f6\">foo\n\u003c/code>\u003c/pre>\n"],["`f&ouml;&ouml;`","\u003cp>\u003ccode>f&amp;ouml;&amp;ouml;\u003c/code>\u003c/p>\n"],["    f&ouml;f&ouml;","\u003cpre>\u003ccode>f&amp;ouml;f&amp;ouml;\n\u003c/code>\u003c/pre>\n"],["&#42;foo&#42;\n*foo*","\u003cp>*foo*\n\u003cem>foo\u003c/em>\u003c/p>\n"],["&#42; foo\n\n* foo","\u003cp>* foo\u003c/p>\n\u003cul>\n\u003cli>foo\u003c/li>\n\u003c/ul>\n"],["foo&#10;&#10;bar","\u003cp>foo\n\nbar\u003c/p>\n"],["&#9; foo","\u003cp>\t foo\u003c/p>\n"],["&quot;hi&quot; &lt;b&gt; &apos;","\u003cp>&quot;hi&quot; &lt;b&gt; '\u003c/p>\n"],["`foo`","\u003cp>\u003ccode>foo\u003c/code>\u003c/p>\n"],["`` foo ` bar ``","\u003cp>\u003ccode>foo ` bar\u003c/code>\u003c/p>\n"],["` `` `","\u003cp>\u003ccode>``\u003c/code>\u003c/p>\n"],["`  ``  `","\u003cp>\u003ccode> `` \u003c/code>\u003c/p>\n"],["` a`","\u003cp>\u003ccode> a\u003c/code>\u003c/p>\n"],["` b `","\u003cp>\u003ccode>b\u003c/code>\u003c/p>\n"],["` `\n`  `","\u003cp>\u003ccode> \u003c/code>\n\u003ccode>  \u003c/code>\u003c/p>\n"],["``\nfoo\nbar  \nbaz\n``","\u003cp>\u003ccode>foo bar   baz\u003c/code>\u003c/p>\n"],["``\nfoo \n``","\u003cp>\u003ccode>foo \u003c/code>\u003c/p>\n"],["`foo   bar \nbaz`","\u003cp>\u003ccode>foo   bar  baz\u003c/code>\u003c/p>\n"],["`foo\\`bar`","\u003cp>\u003ccode>foo\\\u003c/code>bar`\u003c/p>\n"],["``foo`bar``","\u003cp>\u003ccode>foo`bar\u003c/code>\u003c/p>\n"],["` foo `` bar `","\u003cp>\u003ccode>foo `` bar\u003c/code>\u003c/p>\n"],["*foo`*`","\u003cp>*foo\u003ccode>*\u003c/code>\u003c/p>\n"],["[not a `link](/foo`)","\u003cp>[not a \u003ccode>link](/foo\u003c/code>)\u003c/p>\n"],["`\u003ca href=\"`\">`","\u003cp>\u003ccode>&lt;a href=&quot;\u003c/code>&quot;&gt;`\u003c/p>\n"],["`\u003chttp://foo.bar.`baz>`","\u003cp>\u003ccode>&lt;http://foo.bar.\u003c/code>baz&gt;`\u003c/p>\n"],["```foo``","\u003cp>```foo``\u003c/p>\n"],["`foo","\u003cp>`foo\u003c/p>\n"],["`foo``bar``","\u003cp>`foo\u003ccode>bar\u003c/code>\u003c/p>\n"],["*foo bar*","\u003cp>\u003cem>foo bar\u003c/em>\u003c/p>\n"],["a * foo bar*","\u003cp>a * foo bar*\u003c/p>\n"],["a*\"foo\"*","\u003cp>a*&quot;foo&quot;*\u003c/p>\n"],["* a *","\u003cul>\n\u003cli>a *\u003c/li>\n\u003c/ul>\n"],["foo*bar*","\u003cp>foo\u003cem>bar\u003c/em>\u003c/p>\n"],["5*6*78","\u003cp>5\u003cem>6\u003c/em>78\u003c/p>\n"],["_foo bar_","\u003cp>\u003cem>foo bar\u003c/em>\u003c/p>\n"],["_ foo bar_","\u003cp>_ foo bar_\u003c/p>\n"],["a_\"foo\"_","\u003cp>a_&quot;foo&quot;_\u003c/p>\n"],["foo_bar_","\u003cp>foo_bar_\u003c/p>\n"],["5_6_78","\u003cp>5_6_78\u003c/p>\n"],["\u043f\u0440\u0438\u0441\u0442\u0430\u043d\u044f\u043c_\u0441\u0442\u0440\u0435\u043c\u044f\u0442\u0441\u044f_","\u003cp>\u043f\u0440\u0438\u0441\u0442\u0430\u043d\u044f\u043c_\u0441\u0442\u0440\u0435\u043c\u044f\u0442\u0441\u044f_\u003c/p>\n"],["aa_\"bb\"_cc","\u003cp>aa_&quot;bb&quot;_cc\u003c/p>\n"],["foo-_(bar)_","\u003cp>foo-\u003cem>(bar)\u003c/em>\u003c/p>\n"],["_foo*","\u003cp>_foo*\u003c/p>\n"],["*foo bar *","\u003cp>*foo bar *\u003c/p>\n"],["*foo bar\n*","\u003cp>*foo bar\n*\u003c/p>\n"],["*(*foo)","\u003cp>*(*foo)\u003c/p>\n"],["*(*foo*)*","\u003cp>\u003cem>(\u003cem>foo\u003c/em>)\u003c/em>\u003c/p>\n"],["*foo*bar","\u003cp>\u003cem>foo\u003c/em>bar\u003c/p>\n"],["_foo bar _","\u003cp>_foo bar _\u003c/p>\n"],["_(_foo)","\u003cp>_(_foo)\u003c/p>\n"],["_(_foo_)_","\u003cp>\u003cem>(\u003cem>foo\u003c/em>)\u003c/em>\u003c/p>\n"],["_foo_bar","\u003cp>_foo_bar\u003c/p>\n"],["_\u043f\u0440\u0438\u0441\u0442\u0430\u043d\u044f\u043c_\u0441\u0442\u0440\u0435\u043c\u044f\u0442\u0441\u044f","\u003cp>_\u043f\u0440\u0438\u0441\u0442\u0430\u043d\u044f\u043c_\u0441\u0442\u0440\u0435\u043c\u044f\u0442\u0441\u044f\u003c/p>\n"],["_foo_bar_baz_","\u003cp>\u003cem>foo_bar_baz\u003c/em>\u003c/p>\n"],["_(bar)_.","\u003cp>\u003cem>(bar)\u003c/em>.\u003c/p>\n"],["**foo bar**","\u003cp>\u003cstrong>foo bar\u003c/strong>\u003c/p>\n"],["** foo bar**","\u003cp>** foo bar**\u003c/p>\n"],["a**\"foo\"**","\u003cp>a**&quot;foo&quot;**\u003c/p>\n"],["foo**bar**","\u003cp>foo\u003cstrong>bar\u003c/strong>\u003c/p>\n"],["__foo bar__","\u003cp>\u003cstrong>foo bar\u003c/strong>\u003c/p>\n"],["__ foo bar__","\u003cp>__ foo bar__\u003c/p>\n"],["__\nfoo bar__","\u003cp>__\nfoo bar__\u003c/p>\n"],["a__\"foo\"__","\u003cp>a__&quot;foo&quot;__\u003c/p>\n"],["foo__bar__","\u003cp>foo__bar__\u003c/p>\n"],["5__6__78","\u003cp>5__6__78\u003c/p>\n"],["__foo, __bar__, baz__","\u003cp>\u003cstrong>foo, \u003cstrong>bar\u003c/strong>, baz\u003c/strong>\u003c/p>\n"],["foo-__(bar)__","\u003cp>foo-\u003cstrong>(bar)\u003c/strong>\u003c/p>\n"],["**foo bar **","\u003cp>**foo bar **\u003c/p>\n"],["**(**foo)","\u003cp>**(**foo)\u003c/p>\n"],["*(**foo**)*","\u003cp>\u003cem>(\u003cstrong>foo\u003c/strong>)\u003c/em>\u003c/p>\n"],["**Gomphocarpus (*Gomphocarpus physocarpus*, syn.\n*Asclepias physocarpa*)**","\u003cp>\u003cstrong>Gomphocarpus (\u003cem>Gomphocarpus physocarpus\u003c/em>, syn.\n\u003cem>Asclepias physocarpa\u003c/em>)\u003c/strong>\u003c/p>\n"],["**foo \"*bar*\" foo**","\u003cp>\u003cstrong>foo &quot;\u003cem>bar\u003c/em>&quot; foo\u003c/strong>\u003c/p>\n"],["**foo**bar","\u003cp>\u003cstrong>foo\u003c/strong>bar\u003c/p>\n"],["__foo bar __","\u003cp>__foo bar __\u003c/p>\n"],["__(__foo)","\u003cp>__(__foo)\u003c/p>\n"],["_(__foo__)_","\u003cp>\u003cem>(\u003cstrong>foo\u003c/strong>)\u003c/em>\u003c/p>\n"],["__foo__bar","\u003cp>__foo__bar\u003c/p>\n"],["__foo__bar__baz__","\u003cp>\u003cstrong>foo__bar__baz\u003c/strong>\u003c/p>\n"],["__(bar)__.","\u003cp>\u003cstrong>(bar)\u003c/strong>.\u003c/p>\n"],["*foo [bar](/url)*","\u003cp>\u003cem>foo \u003ca href=\"/url\">bar\u003c/a>\u003c/em>\u003c/p>\n"],["*foo\nbar*","\u003cp>\u003cem>foo\nbar\u003c/em>\u003c/p>\n"],["_foo __bar__ baz_","\u003cp>\u003cem>foo \u003cstrong>bar\u003c/strong> baz\u003c/em>\u003c/p>\n"],["_foo _bar_ baz_","\u003cp>\u003cem>foo \u003cem>bar\u003c/em> baz\u003c/em>\u003c/p>\n"],["__foo_ bar_","\u003cp>\u003cem>\u003cem>foo\u003c/em> bar\u003c/em>\u003c/p>\n"],["*foo *bar**","\u003cp>\u003cem>foo \u003cem>bar\u003c/em>\u003c/em>\u003c/p>\n"],["*foo **bar** baz*","\u003cp>\u003cem>foo \u003cstrong>bar\u003c/strong> baz\u003c/em>\u003c/p>\n"],["*foo**bar**baz*","\u003cp>\u003cem>foo\u003cstrong>bar\u003c/strong>baz\u003c/em>\u003c/p>\n"],["*foo**bar*","\u003cp>\u003cem>foo**bar\u003c/em>\u003c/p>\n"],["***foo** bar*","\u003cp>\u003cem>\u003cstrong>foo\u003c/strong> bar\u003c/em>\u003c/p>\n"],["*foo **bar***","\u003cp>\u003cem>foo \u003cstrong>bar\u003c/strong>\u003c/em>\u003c/p>\n"],["*foo**bar***","\u003cp>\u003cem>foo\u003cstrong>bar\u003c/strong>\u003c/em>\u003c/p>\n"],["foo***bar***baz","\u003cp>foo\u003cem>\u003cstrong>bar\u003c/strong>\u003c/em>baz\u003c/p>\n"],["foo******bar*********baz","\u003cp>foo\u003cstrong>\u003cstrong>\u003cstrong>bar\u003c/strong>\u003c/strong>\u003c/strong>***baz\u003c/p>\n"],["*foo **bar *baz* bim** bop*","\u003cp>\u003cem>foo \u003cstrong>bar \u003cem>baz\u003c/em> bim\u003c/strong> bop\u003c/em>\u003c/p>\n"],["*foo [*bar*](/url)*","\u003cp>\u003cem>foo \u003ca href=\"/url\">\u003cem>bar\u003c/em>\u003c/a>\u003c/em>\u003c/p>\n"],["** is not an empty emphasis","\u003cp>** is not an empty emphasis\u003c/p>\n"],["**** is not an empty strong emphasis","\u003cp>**** is not an empty strong emphasis\u003c/p>\n"],["**foo [bar](/url)**","\u003cp>\u003cstrong>foo \u003ca href=\"/url\">bar\u003c/a>\u003c/strong>\u003c/p>\n"],["**foo\nbar**","\u003cp>\u003cstrong>foo\nbar\u003c/strong>\u003c/p>\n"],["__foo _bar_ baz__","\u003cp>\u003cstrong>foo \u003cem>bar\u003c/em> baz\u003c/strong>\u003c/p>\n"],["__foo __bar__ baz__","\u003cp>\u003cstrong>foo \u003cstrong>bar\u003c/strong> baz\u003c/strong>\u003c/p>\n"],["____foo__ bar__","\u003cp>\u003cstrong>\u003cstrong>foo\u003c/strong> bar\u003c/strong>\u003c/p>\n"],["**foo **bar****","\u003cp>\u003cstrong>foo \u003cstrong>bar\u003c/strong>\u003c/strong>\u003c/p>\n"],["**foo *bar* baz**","\u003cp>\u003cstrong>foo \u003cem>bar\u003c/em> baz\u003c/strong>\u003c/p>\n"],["**foo*bar*baz**","\u003cp>\u003cstrong>foo\u003cem>bar\u003c/em>baz\u003c/strong>\u003c/p>\n"],["***foo* bar**","\u003cp>\u003cstrong>\u003cem>foo\u003c/em> bar\u003c/strong>\u003c/p>\n"],["**foo *bar***","\u003cp>\u003cstrong>foo \u003cem>bar\u003c/em>\u003c/strong>\u003c/p>\n"],["**foo *bar **baz**\nbim* bop**","\u003cp>\u003cstrong>foo \u003cem>bar \u003cstrong>baz\u003c/strong>\nbim\u003c/em> bop\u003c/strong>\u003c/p>\n"],["**foo [*bar*](/url)**","\u003cp>\u003cstrong>foo \u003ca href=\"/url\">\u003cem>bar\u003c/em>\u003c/a>\u003c/strong>\u003c/p>\n"],["__ is not an empty emphasis","\u003cp>__ is not an empty emphasis\u003c/p>\n"],["____ is not an empty strong emphasis","\u003cp>____ is not an empty strong emphasis\u003c/p>\n"],["foo ***","\u003cp>foo ***\u003c/p>\n"],["foo *\\**","\u003cp>foo \u003cem>*\u003c/em>\u003c/p>\n"],["foo *_*","\u003cp>foo \u003cem>_\u003c/em>\u003c/p>\n"],["foo *****","\u003cp>foo *****\u003c/p>\n"],["foo **\\***","\u003cp>foo \u003cstrong>*\u003c/strong>\u003c/p>\n"],["foo **_**","\u003cp>foo \u003cstrong>_\u003c/strong>\u003c/p>\n"],["**foo*","\u003cp>*\u003cem>foo\u003c/em>\u003c/p>\n"],["*foo**","\u003cp>\u003cem>foo\u003c/em>*\u003c/p>\n"],["***foo**","\u003cp>*\u003cstrong>foo\u003c/strong>\u003c/p>\n"],["****foo*","\u003cp>***\u003cem>foo\u003c/em>\u003c/p>\n"],["**foo***","\u003cp>\u003cstrong>foo\u003c/strong>*\u003c/p>\n"],["*foo****","\u003cp>\u003cem>foo\u003c/em>***\u003c/p>\n"],["foo ___","\u003cp>foo ___\u003c/p>\n"],["foo _\\__","\u003cp>foo \u003cem>_\u003c/em>\u003c/p>\n"],["foo _*_","\u003cp>foo \u003cem>*\u003c/em>\u003c/p>\n"],["foo _____","\u003cp>foo _____\u003c/p>\n"],["foo __\\___","\u003cp>foo \u003cstrong>_\u003c/strong>\u003c/p>\n"],["foo __*__","\u003cp>foo \u003cstrong>*\u003c/strong>\u003c/p>\n"],["__foo_","\u003cp>_\u003cem>foo\u003c/em>\u003c/p>\n"],["_foo__","\u003cp>\u003cem>foo\u003c/em>_\u003c/p>\n"],["___foo__","\u003cp>_\u003cstrong>foo\u003c/strong>\u003c/p>\n"],["____foo_","\u003cp>___\u003cem>foo\u003c/em>\u003c/p>\n"],["__foo___","\u003cp>\u003cstrong>foo\u003c/strong>_\u003c/p>\n"],["_foo____","\u003cp>\u003cem>foo\u003c/em>___\u003c/p>\n"],["**foo**","\u003cp>\u003cstrong>foo\u003c/strong>\u003c/p>\n"],["*_foo_*","\u003cp>\u003cem>\u003cem>foo\u003c/em>\u003c/em>\u003c/p>\n"],["__foo__","\u003cp>\u003cstrong>foo\u003c/strong>\u003c/p>\n"],["_*foo*_","\u003cp>\u003cem>\u003cem>foo\u003c/em>\u003c/em>\u003c/p>\n"],["****foo****","\u003cp>\u003cstrong>\u003cstrong>foo\u003c/strong>\u003c/strong>\u003c/p>\n"],["____foo____","\u003cp>\u003cstrong>\u003cstrong>foo\u003c/strong>\u003c/strong>\u003c/p>\n"],["******foo******","\u003cp>\u003cstrong>\u003cstrong>\u003cstrong>foo\u003c/strong>\u003c/strong>\u003c/strong>\u003c/p>\n"],["***foo***","\u003cp>\u003cem>\u003cstrong>foo\u003c/strong>\u003c/em>\u003c/p>\n"],["_____foo_____","\u003cp>\u003cem>\u003cstrong>\u003cstrong>foo\u003c/strong>\u003c/strong>\u003c/em>\u003c/p>\n"],["*foo _bar* baz_","\u003cp>\u003cem>foo _bar\u003c/em> baz_\u003c/p>\n"],["*foo __bar *baz bim__ bam*","\u003cp>\u003cem>foo \u003cstrong>bar *baz bim\u003c/strong> bam\u003c/em>\u003c/p>\n"],["**foo **bar baz**","\u003cp>**foo \u003cstrong>bar baz\u003c/strong>\u003c/p>\n"],["*foo *bar baz*","\u003cp>*foo \u003cem>bar baz\u003c/em>\u003c/p>\n"],["*[bar*](/url)","\u003cp>*\u003ca href=\"/url\">bar*\u003c/a>\u003c/p>\n"],["_foo [bar_](/url)","\u003cp>_foo \u003ca href=\"/url\">bar_\u003c/a>\u003c/p>\n"],["*a `*`*","\u003cp>\u003cem>a \u003ccode>*\u003c/code>\u003c/em>\u003c/p>\n"],["_a `_`_","\u003cp>\u003cem>a \u003ccode>_\u003c/code>\u003c/em>\u003c/p>\n"],["**a\u003chttp://foo.bar/?q=**>","\u003cp>**a\u003ca href=\"http://foo.bar/?q=**\">http://foo.bar/?q=**\u003c/a>\u003c/p>\n"],["__a\u003chttp://foo.bar/?q=__>","\u003cp>__a\u003ca href=\"http://foo.bar/?q=__\">http://foo.bar/?q=__\u003c/a>\u003c/p>\n"],["[link](/uri \"title\")","\u003cp>\u003ca href=\"/uri\" title=\"title\">link\u003c/a>\u003c/p>\n"],["[link](/uri)","\u003cp>\u003ca href=\"/uri\">link\u003c/a>\u003c/p>\n"],["[link]()","\u003cp>\u003ca href=\"\">link\u003c/a>\u003c/p>\n"],["[link](\u003c>)","\u003cp>\u003ca href=\"\">link\u003c/a>\u003c/p>\n"],["[link](/my uri)","\u003cp>[link](/my uri)\u003c/p>\n"],["[link](\u003c/my uri>)","\u003cp>\u003ca href=\"/my%20uri\">link\u003c/a>\u003c/p>\n"],["[link](foo\nbar)","\u003cp>[link](foo\nbar)\u003c/p>\n"],["[link](\u003cfoo\nbar>)","\u003cp>[link](&lt;foo\nbar&gt;)\u003c/p>\n"],["[a](\u003cb)c>)","\u003cp>\u003ca href=\"b)c\">a\u003c/a>\u003c/p>\n"],["[link](\u003cfoo\\>)","\u003cp>[link](&lt;foo&gt;)\u003c/p>\n"],["[a](\u003cb)c\n[a](\u003cb)c>\n[a](\u003cb>c)","\u003cp>[a](&lt;b)c\n[a](&lt;b)c&gt;\n[a](&lt;b&gt;c)\u003c/p>\n"],["[link](\\(foo\\))","\u003cp>\u003ca href=\"(foo)\">link\u003c/a>\u003c/p>\n"],["[link](foo(and(bar)))","\u003cp>\u003ca href=\"foo(and(bar))\">link\u003c/a>\u003c/p>\n"],["[link](foo\\(and\\(bar\\))","\u003cp>\u003ca href=\"foo(and(bar)\">link\u003c/a>\u003c/p>\n"],["[link](\u003cfoo(and(bar)>)","\u003cp>\u003ca href=\"foo(and(bar)\">link\u003c/a>\u003c/p>\n"],["[link](foo\\)\\:)","\u003cp>\u003ca href=\"foo):\">link\u003c/a>\u003c/p>\n"],["[link](#fragment)\n\n[link](http://example.com#fragment)\n\n[link](http://example.com?foo=3#frag)","\u003cp>\u003ca href=\"#fragment\">link\u003c/a>\u003c/p>\n\u003cp>\u003ca href=\"http://example.com#fragment\">link\u003c/a>\u003c/p>\n\u003cp>\u003ca href=\"http://example.com?foo=3#frag\">link\u003c/a>\u003c/p>\n"],["[link](foo\\bar)","\u003cp>\u003ca href=\"foo%5Cbar\">link\u003c/a>\u003c/p>\n"],["[link](foo%20b&auml;)","\u003cp>\u003ca href=\"foo%20b%C3%A4\">link\u003c/a>\u003c/p>\n"],["[link](\"title\")","\u003cp>\u003ca href=\"%22title%22\">link\u003c/a>\u003c/p>\n"],["[link](/url \"title\")\n[link](/url 'title')\n[link](/url (title))","\u003cp>\u003ca href=\"/url\" title=\"title\">link\u003c/a>\n\u003ca href=\"/url\" title=\"title\">link\u003c/a>\n\u003ca href=\"/url\" title=\"title\">link\u003c/a>\u003c/p>\n"],["[link](/url \"title \\\"&quot;\")","\u003cp>\u003ca href=\"/url\" title=\"title &quot;&quot;\">link\u003c/a>\u003c/p>\n"],["[link](/url \"title \"and\" title\")","\u003cp>[link](/url &quot;title &quot;and&quot; title&quot;)\u003c/p>\n"],["[link](/url 'title \"and\" title')","\u003cp>\u003ca href=\"/url\" title=\"title &quot;and&quot; title\">link\u003c/a>\u003c/p>\n"],["[link](   /uri\n  \"title\"  )","\u003cp>\u003ca href=\"/uri\" title=\"title\">link\u003c/a>\u003c/p>\n"],["[link] (/uri)","\u003cp>[link] (/uri)\u003c/p>\n"],["[link [foo [bar]]](/uri)","\u003cp>\u003ca href=\"/uri\">link [foo [bar]]\u003c/a>\u003c/p>\n"],["[link] bar](/uri)","\u003cp>[link] bar](/uri)\u003c/p>\n"],["[link [bar](/uri)","\u003cp>[link \u003ca href=\"/uri\">bar\u003c/a>\u003c/p>\n"],["[link \\[bar](/uri)","\u003cp>\u003ca href=\"/uri\">link [bar\u003c/a>\u003c/p>\n"],["[link *foo **bar** `#`*](/uri)","\u003cp>\u003ca href=\"/uri\">link \u003cem>foo \u003cstrong>bar\u003c/strong> \u003ccode>#\u003c/code>\u003c/em>\u003c/a>\u003c/p>\n"],["[![moon](moon.jpg)](/uri)","\u003cp>\u003ca href=\"/uri\">\u003cimg src=\"moon.jpg\" alt=\"moon\">\u003c/a>\u003c/p>\n"],["[foo [bar](/uri)](/uri)","\u003cp>[foo \u003ca href=\"/uri\">bar\u003c/a>](/uri)\u003c/p>\n"],["[foo *[bar [baz](/uri)](/uri)*](/uri)","\u003cp>[foo \u003cem>[bar \u003ca href=\"/uri\">baz\u003c/a>](/uri)\u003c/em>](/uri)\u003c/p>\n"],["![[[foo](uri1)](uri2)](uri3)","\u003cp>\u003cimg src=\"uri3\" alt=\"[foo](uri2)\">\u003c/p>\n"],["*[foo*](/uri)","\u003cp>*\u003ca href=\"/uri\">foo*\u003c/a>\u003c/p>\n"],["[foo *bar](baz*)","\u003cp>\u003ca href=\"baz*\">foo *bar\u003c/a>\u003c/p>\n"],["*foo [bar* baz]","\u003cp>\u003cem>foo [bar\u003c/em> baz]\u003c/p>\n"],["[foo`](/uri)`","\u003cp>[foo\u003ccode>](/uri)\u003c/code>\u003c/p>\n"],["[foo][bar]\n\n[bar]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\u003c/p>\n"],["[link [foo [bar]]][ref]\n\n[ref]: /uri","\u003cp>\u003ca href=\"/uri\">link [foo [bar]]\u003c/a>\u003c/p>\n"],["[link \\[bar][ref]\n\n[ref]: /uri","\u003cp>\u003ca href=\"/uri\">link [bar\u003c/a>\u003c/p>\n"],["[link *foo **bar** `#`*][ref]\n\n[ref]: /uri","\u003cp>\u003ca href=\"/uri\">link \u003cem>foo \u003cstrong>bar\u003c/strong> \u003ccode>#\u003c/code>\u003c/em>\u003c/a>\u003c/p>\n"],["[![moon](moon.jpg)][ref]\n\n[ref]: /uri","\u003cp>\u003ca href=\"/uri\">\u003cimg src=\"moon.jpg\" alt=\"moon\">\u003c/a>\u003c/p>\n"],["[foo [bar](/uri)][ref]\n\n[ref]: /uri","\u003cp>[foo \u003ca href=\"/uri\">bar\u003c/a>]\u003ca href=\"/uri\">ref\u003c/a>\u003c/p>\n"],["[foo *bar [baz][ref]*][ref]\n\n[ref]: /uri","\u003cp>[foo \u003cem>bar \u003ca href=\"/uri\">baz\u003c/a>\u003c/em>]\u003ca href=\"/uri\">ref\u003c/a>\u003c/p>\n"],["*[foo*][ref]\n\n[ref]: /uri","\u003cp>*\u003ca href=\"/uri\">foo*\u003c/a>\u003c/p>\n"],["[foo *bar][ref]\n\n[ref]: /uri","\u003cp>\u003ca href=\"/uri\">foo *bar\u003c/a>\u003c/p>\n"],["[foo`][ref]`\n\n[ref]: /uri","\u003cp>[foo\u003ccode>][ref]\u003c/code>\u003c/p>\n"],["[foo][BaR]\n\n[bar]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\u003c/p>\n"],["[\u0422\u043e\u043b\u043f\u043e\u0439][\u0422\u043e\u043b\u043f\u043e\u0439] is a Russian word.\n\n[\u0422\u041e\u041b\u041f\u041e\u0419]: /url","\u003cp>\u003ca href=\"/url\">\u0422\u043e\u043b\u043f\u043e\u0439\u003c/a> is a Russian word.\u003c/p>\n"],["[Foo\n  bar]: /url\n\n[Baz][Foo bar]","\u003cp>\u003ca href=\"/url\">Baz\u003c/a>\u003c/p>\n"],["[foo] [bar]\n\n[bar]: /url \"title\"","\u003cp>[foo] \u003ca href=\"/url\" title=\"title\">bar\u003c/a>\u003c/p>\n"],["[foo]\n[bar]\n\n[bar]: /url \"title\"","\u003cp>[foo]\n\u003ca href=\"/url\" title=\"title\">bar\u003c/a>\u003c/p>\n"],["[foo]: /url1\n\n[foo]: /url2\n\n[bar][foo]","\u003cp>\u003ca href=\"/url1\">bar\u003c/a>\u003c/p>\n"],["[bar][foo\\!]\n\n[foo!]: /url","\u003cp>[bar][foo!]\u003c/p>\n"],["[foo][ref[]\n\n[ref[]: /uri","\u003cp>[foo][ref[]\u003c/p>\n\u003cp>[ref[]: /uri\u003c/p>\n"],["[foo][ref[bar]]\n\n[ref[bar]]: /uri","\u003cp>[foo][ref[bar]]\u003c/p>\n\u003cp>[ref[bar]]: /uri\u003c/p>\n"],["[[[foo]]]\n\n[[[foo]]]: /url","\u003cp>[[[foo]]]\u003c/p>\n\u003cp>[[[foo]]]: /url\u003c/p>\n"],["[foo][ref\\[]\n\n[ref\\[]: /uri","\u003cp>\u003ca href=\"/uri\">foo\u003c/a>\u003c/p>\n"],["[bar\\\\]: /uri\n\n[bar\\\\]","\u003cp>\u003ca href=\"/uri\">bar\\\u003c/a>\u003c/p>\n"],["[]\n\n[]: /uri","\u003cp>[]\u003c/p>\n\u003cp>[]: /uri\u003c/p>\n"],["[\n ]\n\n[\n ]: /uri","\u003cp>[\n]\u003c/p>\n\u003cp>[\n]: /uri\u003c/p>\n"],["[foo][]\n\n[foo]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\u003c/p>\n"],["[*foo* bar][]\n\n[*foo* bar]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">\u003cem>foo\u003c/em> bar\u003c/a>\u003c/p>\n"],["[Foo][]\n\n[foo]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">Foo\u003c/a>\u003c/p>\n"],["[foo] \n[]\n\n[foo]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\n[]\u003c/p>\n"],["[foo]\n\n[foo]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\u003c/p>\n"],["[*foo* bar]\n\n[*foo* bar]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">\u003cem>foo\u003c/em> bar\u003c/a>\u003c/p>\n"],["[[*foo* bar]]\n\n[*foo* bar]: /url \"title\"","\u003cp>[\u003ca href=\"/url\" title=\"title\">\u003cem>foo\u003c/em> bar\u003c/a>]\u003c/p>\n"],["[[bar [foo]\n\n[foo]: /url","\u003cp>[[bar \u003ca href=\"/url\">foo\u003c/a>\u003c/p>\n"],["[Foo]\n\n[foo]: /url \"title\"","\u003cp>\u003ca href=\"/url\" title=\"title\">Foo\u003c/a>\u003c/p>\n"],["[foo] bar\n\n[foo]: /url","\u003cp>\u003ca href=\"/url\">foo\u003c/a> bar\u003c/p>\n"],["\\[foo]\n\n[foo]: /url \"title\"","\u003cp>[foo]\u003c/p>\n"],["[foo*]: /url\n\n*[foo*]","\u003cp>*\u003ca href=\"/url\">foo*\u003c/a>\u003c/p>\n"],["[foo][bar]\n\n[foo]: /url1\n[bar]: /url2","\u003cp>\u003ca href=\"/url2\">foo\u003c/a>\u003c/p>\n"],["[foo][]\n\n[foo]: /url1","\u003cp>\u003ca href=\"/url1\">foo\u003c/a>\u003c/p>\n"],["[foo]()\n\n[foo]: /url1","\u003cp>\u003ca href=\"\">foo\u003c/a>\u003c/p>\n"],["[foo](not a link)\n\n[foo]: /url1","\u003cp>\u003ca href=\"/url1\">foo\u003c/a>(not a link)\u003c/p>\n"],["[foo][bar][baz]\n\n[baz]: /url","\u003cp>[foo]\u003ca href=\"/url\">bar\u003c/a>\u003c/p>\n"],["[foo][bar][baz]\n\n[baz]: /url1\n[bar]: /url2","\u003cp>\u003ca href=\"/url2\">foo\u003c/a>\u003ca href=\"/url1\">baz\u003c/a>\u003c/p>\n"],["[foo][bar][baz]\n\n[baz]: /url1\n[foo]: /url2","\u003cp>[foo]\u003ca href=\"/url1\">bar\u003c/a>\u003c/p>\n"],["[a]: /url \"title\"\n\n[a]","\u003cp>\u003ca href=\"/url\" title=\"title\">a\u003c/a>\u003c/p>\n"],["[a]: \u003c>\n\n[a]","\u003cp>\u003ca href=\"\">a\u003c/a>\u003c/p>\n"],["[a]:\n/url\n\n[a]","\u003cp>\u003ca href=\"/url\">a\u003c/a>\u003c/p>\n"],["[a]: /url 'title\n\nwith blank line'\n\n[a]","\u003cp>[a]: /url 'title\u003c/p>\n\u003cp>with blank line'\u003c/p>\n\u003cp>[a]\u003c/p>\n"],["[a]:\n\n[a]","\u003cp>[a]:\u003c/p>\n\u003cp>[a]\u003c/p>\n"],["[foo]: /url\\bar\\*baz \"foo\\\"bar\\baz\"\n\n[foo]","\u003cp>\u003ca href=\"/url%5Cbar*baz\" title=\"foo&quot;bar\\baz\">foo\u003c/a>\u003c/p>\n"],["[foo]\n\n[foo]: url","\u003cp>\u003ca href=\"url\">foo\u003c/a>\u003c/p>\n"],["[foo]\n\n[foo]: first\n[foo]: second","\u003cp>\u003ca href=\"first\">foo\u003c/a>\u003c/p>\n"],["[FOO]: /url\n\n[Foo]","\u003cp>\u003ca href=\"/url\">Foo\u003c/a>\u003c/p>\n"],["[\u0391\u0393\u039f]: /\u03c6\u03bf\u03c5\n\n[\u03b1\u03b3\u03bf]","\u003cp>\u003ca href=\"/%CF%86%CE%BF%CF%85\">\u03b1\u03b3\u03bf\u003c/a>\u003c/p>\n"],["[foo]: /url",""],["[\nfoo\n]: /url\nbar","\u003cp>bar\u003c/p>\n"],["[foo]: /url \"title\" ok","\u003cp>[foo]: /url &quot;title&quot; ok\u003c/p>\n"],["[foo]: /url\n\"title\" ok","\u003cp>&quot;title&quot; ok\u003c/p>\n"],["    [foo]: /url \"title\"\n\n[foo]","\u003cpre>\u003ccode>[foo]: /url &quot;title&quot;\n\u003c/code>\u003c/pre>\n\u003cp>[foo]\u003c/p>\n"],["```\n[foo]: /url\n```\n\n[foo]","\u003cpre>\u003ccode>[foo]: /url\n\u003c/code>\u003c/pre>\n\u003cp>[foo]\u003c/p>\n"],["Foo\n[bar]: /baz\n\n[bar]","\u003cp>Foo\n[bar]: /baz\u003c/p>\n\u003cp>[bar]\u003c/p>\n"],["# [Foo]\n[foo]: /url\n> bar","\u003ch1>\u003ca href=\"/url\">Foo\u003c/a>\u003c/h1>\n\u003cblockquote>\n\u003cp>bar\u003c/p>\n\u003c/blockquote>\n"],["[foo]: /url\nbar\n===\n[foo]","\u003ch1>bar\u003c/h1>\n\u003cp>\u003ca href=\"/url\">foo\u003c/a>\u003c/p>\n"],["[foo]: /url\n===\n[foo]","\u003cp>===\n\u003ca href=\"/url\">foo\u003c/a>\u003c/p>\n"],["[foo]: /foo-url \"foo\"\n[bar]: /bar-url\n  \"bar\"\n[baz]: /baz-url\n\n[foo],\n[bar],\n[baz]","\u003cp>\u003ca href=\"/foo-url\" title=\"foo\">foo\u003c/a>,\n\u003ca href=\"/bar-url\" title=\"bar\">bar\u003c/a>,\n\u003ca href=\"/baz-url\">baz\u003c/a>\u003c/p>\n"],["[foo]\n\n> [foo]: /url","\u003cp>\u003ca href=\"/url\">foo\u003c/a>\u003c/p>\n\u003cblockquote>\n\u003c/blockquote>\n"],["![foo](/url \"title\")","\u003cp>\u003cimg src=\"/url\" alt=\"foo\" title=\"title\">\u003c/p>\n"],["![foo *bar*]\n\n[foo *bar*]: train.jpg \"train & tracks\"","\u003cp>\u003cimg src=\"train.jpg\" alt=\"foo bar\" title=\"train &amp; tracks\">\u003c/p>\n"],["![foo ![bar](/url)](/url2)","\u003cp>\u003cimg src=\"/url2\" alt=\"foo bar\">\u003c/p>\n"],["![foo [bar](/url)](/url2)","\u003cp>\u003cimg src=\"/url2\" alt=\"foo bar\">\u003c/p>\n"],["![foo *bar*][]\n\n[foo *bar*]: train.jpg \"train & tracks\"","\u003cp>\u003cimg src=\"train.jpg\" alt=\"foo bar\" title=\"train &amp; tracks\">\u003c/p>\n"],["![foo *bar*][foobar]\n\n[FOOBAR]: train.jpg \"train & tracks\"","\u003cp>\u003cimg src=\"train.jpg\" alt=\"foo bar\" title=\"train &amp; tracks\">\u003c/p>\n"],["![foo](train.jpg)","\u003cp>\u003cimg src=\"train.jpg\" alt=\"foo\">\u003c/p>\n"],["My ![foo bar](/path/to/train.jpg  \"title\"   )","\u003cp>My \u003cimg src=\"/path/to/train.jpg\" alt=\"foo bar\" title=\"title\">\u003c/p>\n"],["![foo](\u003curl>)","\u003cp>\u003cimg src=\"url\" alt=\"foo\">\u003c/p>\n"],["![](/url)","\u003cp>\u003cimg src=\"/url\" alt=\"\">\u003c/p>\n"],["![foo][bar]\n\n[bar]: /url","\u003cp>\u003cimg src=\"/url\" alt=\"foo\">\u003c/p>\n"],["![foo][bar]\n\n[BAR]: /url","\u003cp>\u003cimg src=\"/url\" alt=\"foo\">\u003c/p>\n"],["![foo][]\n\n[foo]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"foo\" title=\"title\">\u003c/p>\n"],["![*foo* bar][]\n\n[*foo* bar]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"foo bar\" title=\"title\">\u003c/p>\n"],["![Foo][]\n\n[foo]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"Foo\" title=\"title\">\u003c/p>\n"],["![foo] \n[]\n\n[foo]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"foo\" title=\"title\">\n[]\u003c/p>\n"],["![foo]\n\n[foo]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"foo\" title=\"title\">\u003c/p>\n"],["![*foo* bar]\n\n[*foo* bar]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"foo bar\" title=\"title\">\u003c/p>\n"],["![[foo]]\n\n[[foo]]: /url \"title\"","\u003cp>![[foo]]\u003c/p>\n\u003cp>[[foo]]: /url &quot;title&quot;\u003c/p>\n"],["![Foo]\n\n[foo]: /url \"title\"","\u003cp>\u003cimg src=\"/url\" alt=\"Foo\" title=\"title\">\u003c/p>\n"],["!\\[foo]\n\n[foo]: /url \"title\"","\u003cp>![foo]\u003c/p>\n"],["\\![foo]\n\n[foo]: /url \"title\"","\u003cp>!\u003ca href=\"/url\" title=\"title\">foo\u003c/a>\u003c/p>\n"],["\u003chttp://foo.bar.baz>","\u003cp>\u003ca href=\"http://foo.bar.baz\">http://foo.bar.baz\u003c/a>\u003c/p>\n"],["\u003chttp://foo.bar.baz/test?q=hello&id=22&boolean>","\u003cp>\u003ca href=\"http://foo.bar.baz/test?q=hello&amp;id=22&amp;boolean\">http://foo.bar.baz/test?q=hello&amp;id=22&amp;boolean\u003c/a>\u003c/p>\n"],["\u003circ://foo.bar:2233/baz>","\u003cp>\u003ca href=\"irc://foo.bar:2233/baz\">irc://foo.bar:2233/baz\u003c/a>\u003c/p>\n"],["\u003cMAILTO:FOO@BAR.BAZ>","\u003cp>\u003ca href=\"MAILTO:FOO@BAR.BAZ\">MAILTO:FOO@BAR.BAZ\u003c/a>\u003c/p>\n"],["\u003ca+b+c:d>","\u003cp>\u003ca href=\"a+b+c:d\">a+b+c:d\u003c/a>\u003c/p>\n"],["\u003cmade-up-scheme://foo,bar>","\u003cp>\u003ca href=\"made-up-scheme://foo,bar\">made-up-scheme://foo,bar\u003c/a>\u003c/p>\n"],["\u003chttp://../>","\u003cp>\u003ca href=\"http://../\">http://../\u003c/a>\u003c/p>\n"],["\u003clocalhost:5001/foo>","\u003cp>\u003ca href=\"localhost:5001/foo\">localhost:5001/foo\u003c/a>\u003c/p>\n"],["\u003chttp://foo.bar/baz bim>","\u003cp>&lt;http://foo.bar/baz bim&gt;\u003c/p>\n"],["\u003chttp://example.com/\\[\\>","\u003cp>\u003ca href=\"http://example.com/%5C%5B%5C\">http://example.com/\\[\\\u003c/a>\u003c/p>\n"],["\u003cfoo@bar.example.com>","\u003cp>\u003ca href=\"mailto:foo@bar.example.com\">foo@bar.example.com\u003c/a>\u003c/p>\n"],["\u003cfoo+special@Bar.baz-bar0.com>","\u003cp>\u003ca href=\"mailto:foo+special@Bar.baz-bar0.com\">foo+special@Bar.baz-bar0.com\u003c/a>\u003c/p>\n"],["\u003cfoo\\+@bar.example.com>","\u003cp>&lt;foo+@bar.example.com&gt;\u003c/p>\n"],["\u003c>","\u003cp>&lt;&gt;\u003c/p>\n"],["\u003c http://foo.bar >","\u003cp>&lt; http://foo.bar &gt;\u003c/p>\n"],["\u003cm:abc>","\u003cp>&lt;m:abc&gt;\u003c/p>\n"],["\u003cfoo.bar.baz>","\u003cp>&lt;foo.bar.baz&gt;\u003c/p>\n"],["http://example.com","\u003cp>http://example.com\u003c/p>\n"],["foo@bar.example.com","\u003cp>foo@bar.example.com\u003c/p>\n"],["foo  \nbaz","\u003cp>foo\u003cbr>\nbaz\u003c/p>\n"],["foo\\\nbaz","\u003cp>foo\u003cbr>\nbaz\u003c/p>\n"],["foo       \nbaz","\u003cp>foo\u003cbr>\nbaz\u003c/p>\n"],["foo  \n     bar","\u003cp>foo\u003cbr>\nbar\u003c/p>\n"],["foo\\\n     bar","\u003cp>foo\u003cbr>\nbar\u003c/p>\n"],["*foo  \nbar*","\u003cp>\u003cem>foo\u003cbr>\nbar\u003c/em>\u003c/p>\n"],["*foo\\\nbar*","\u003cp>\u003cem>foo\u003cbr>\nbar\u003c/em>\u003c/p>\n"],["`code  \nspan`","\u003cp>\u003ccode>code   span\u003c/code>\u003c/p>\n"],["`code\\\nspan`","\u003cp>\u003ccode>code\\ span\u003c/code>\u003c/p>\n"],["foo\\","\u003cp>foo\\\u003c/p>\n"],["foo  ","\u003cp>foo\u003c/p>\n"],["### foo\\","\u003ch3>foo\\\u003c/h3>\n"],["### foo  ","\u003ch3>foo\u003c/h3>\n"],["foo\nbaz","\u003cp>foo\nbaz\u003c/p>\n"],["foo \n baz","\u003cp>foo\nbaz\u003c/p>\n"],["hello $.;'there","\u003cp>hello $.;'there\u003c/p>\n"],["Foo \u03c7\u03c1\u1fc6\u03bd","\u003cp>Foo \u03c7\u03c1\u1fc6\u03bd\u003c/p>\n"],["Multiple     spaces","\u003cp>Multiple     spaces\u003c/p>\n"],["[x](javascript:alert(1))","\u003cp>[x](javascript:alert(1))\u003c/p>\n"],["[x](JavaScript:alert(1))","\u003cp>[x](JavaScript:alert(1))\u003c/p>\n"],["[x](  javascript:alert(1))","\u003cp>[x](  javascript:alert(1))\u003c/p>\n"],["[x](java&#115;cript:alert(1))","\u003cp>[x](javascript:alert(1))\u003c/p>\n"],["[x](&#106;avascript:alert(1))","\u003cp>[x](javascript:alert(1))\u003c/p>\n"],["[x](vbscript:msgbox(1))","\u003cp>[x](vbscript:msgbox(1))\u003c/p>\n"],["[x](data:text/html,\u003cscript>alert(1)\u003c/script>)","\u003cp>[x](data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;)\u003c/p>\n"],["![x](data:image/png;base64,AAAA)","\u003cp>\u003cimg src=\"data:image/png;base64,AAAA\" alt=\"x\">\u003c/p>\n"],["![x](data:text/html;base64,AAAA)","\u003cp>![x](data:text/html;base64,AAAA)\u003c/p>\n"],["[x](file:///etc/passwd)","\u003cp>[x](file:///etc/passwd)\u003c/p>\n"],["\u003cjavascript:alert(1)>","\u003cp>&lt;javascript:alert(1)&gt;\u003c/p>\n"],["[x]\n\n[x]: javascript:alert(1)","\u003cp>[x]\u003c/p>\n\u003cp>[x]: javascript:alert(1)\u003c/p>\n"],["[x](java\tscript:alert(1))","\u003cp>[x](java\tscript:alert(1))\u003c/p>\n"],["[x](\u003cjava\tscript:alert(1)>)","\u003cp>\u003ca href=\"java%09script:alert(1)\">x\u003c/a>\u003c/p>\n"],["[x](https://example.org/a b)","\u003cp>[x](https://example.org/a b)\u003c/p>\n"],["[x](https://example.org/\u00e9\u4e2d)","\u003cp>\u003ca href=\"https://example.org/%C3%A9%E4%B8%AD\">x\u003c/a>\u003c/p>\n"],["[x](https://example.org/a%20b%zz)","\u003cp>\u003ca href=\"https://example.org/a%20b%25zz\">x\u003c/a>\u003c/p>\n"],["[x](mailto:a@b.co)","\u003cp>\u003ca href=\"mailto:a@b.co\">x\u003c/a>\u003c/p>\n"],["[x](tel:+911234567890)","\u003cp>\u003ca href=\"tel:+911234567890\">x\u003c/a>\u003c/p>\n"],["[x](#top)","\u003cp>\u003ca href=\"#top\">x\u003c/a>\u003c/p>\n"],["[x](?q=1)","\u003cp>\u003ca href=\"?q=1\">x\u003c/a>\u003c/p>\n"],["[x](./a/../b)","\u003cp>\u003ca href=\"./a/../b\">x\u003c/a>\u003c/p>\n"],["| a | b |\n|---|---|\n| 1 | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["a | b\n--|--\n1 | 2","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|:--|--:|\n| 1 | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth style=\"text-align:left\">a\u003c/th>\n\u003cth style=\"text-align:right\">b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd style=\"text-align:left\">1\u003c/td>\n\u003ctd style=\"text-align:right\">2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b | c |\n|:-:|---|--:|\n| 1 | 2 | 3 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth style=\"text-align:center\">a\u003c/th>\n\u003cth>b\u003c/th>\n\u003cth style=\"text-align:right\">c\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd style=\"text-align:center\">1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003ctd style=\"text-align:right\">3\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a |\n|---|\n| 1 |\n| 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003c/tr>\n\u003ctr>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003c/table>\n"],["| a | b |\n|---|---|\n| 1 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|\n| 1 | 2 | 3 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|\n| x \\| y | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>x | y\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| *a* | `b` |\n|---|---|\n| **1** | [l](/u) |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>\u003cem>a\u003c/em>\u003c/th>\n\u003cth>\u003ccode>b\u003c/code>\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>\u003cstrong>1\u003c/strong>\u003c/td>\n\u003ctd>\u003ca href=\"/u\">l\u003c/a>\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["text before\n| a | b |\n|---|---|\n| 1 | 2 |","\u003cp>text before\u003c/p>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\ntext after","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003ctr>\n\u003ctd>text after\u003c/td>\n\u003ctd>\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\n\ntext after","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003cp>text after\u003c/p>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\n# heading","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003ch1>heading\u003c/h1>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\n> quote","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003cblockquote>\n\u003cp>quote\u003c/p>\n\u003c/blockquote>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\n- item","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003cul>\n\u003cli>item\u003c/li>\n\u003c/ul>\n"],["| a | b |\n|---|---|\n| 1 | 2 |\n```\ncode\n```","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003cpre>\u003ccode>code\n\u003c/code>\u003c/pre>\n"],["| a | b\n|---|---\n| 1 | 2","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["|a|b|\n|-|-|\n|1|2|","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n| - | - |\n| 1 | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|--|--|\n||2|","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|--|\n| 1 | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["| a | b |\n|---|---|\n\n| c | d |\n|---|---|\n| 3 | 4 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003c/table>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>c\u003c/th>\n\u003cth>d\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>3\u003c/td>\n\u003ctd>4\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["> | a | b |\n> |---|---|\n> | 1 | 2 |","\u003cblockquote>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003c/blockquote>\n"],["- | a | b |\n  |---|---|\n  | 1 | 2 |","\u003cul>\n\u003cli>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>1\u003c/td>\n\u003ctd>2\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n\u003c/li>\n\u003c/ul>\n"],["| a | b |\n|---|---|\n    | 1 | 2 |","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003c/table>\n\u003cpre>\u003ccode>| 1 | 2 |\n\u003c/code>\u003c/pre>\n"],["Not a table | a\n---","\u003ch2>Not a table | a\u003c/h2>\n"],["a | b\n---|---","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003c/table>\n"],["| a | b |\n|--- ---|---|\n| 1 | 2 |","\u003cp>| a | b |\n|--- ---|---|\n| 1 | 2 |\u003c/p>\n"],["| a | b |\n|---|---|---|\n| 1 | 2 |","\u003cp>| a | b |\n|---|---|---|\n| 1 | 2 |\u003c/p>\n"],["| a |\n| b |\n|---|","\u003cp>| a |\u003c/p>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003c/table>\n"],["one\ntwo | a\n---|---\nx | y","\u003cp>one\u003c/p>\n\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>two\u003c/th>\n\u003cth>a\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>x\u003c/td>\n\u003ctd>y\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["|a|b|\n|---|---|\n|&amp;|&#65;|","\u003ctable>\n\u003cthead>\n\u003ctr>\n\u003cth>a\u003c/th>\n\u003cth>b\u003c/th>\n\u003c/tr>\n\u003c/thead>\n\u003ctbody>\n\u003ctr>\n\u003ctd>&amp;\u003c/td>\n\u003ctd>A\u003c/td>\n\u003c/tr>\n\u003c/tbody>\n\u003c/table>\n"],["~~foo~~","\u003cp>\u003cdel>foo\u003c/del>\u003c/p>\n"],["~~foo~~bar","\u003cp>\u003cdel>foo\u003c/del>bar\u003c/p>\n"],["a ~~b~~ c","\u003cp>a \u003cdel>b\u003c/del> c\u003c/p>\n"],["~~a *b* c~~","\u003cp>\u003cdel>a \u003cem>b\u003c/em> c\u003c/del>\u003c/p>\n"],["~~ a ~~","\u003cp>~~ a ~~\u003c/p>\n"],["~~a~~~","\u003cp>\u003cdel>a\u003c/del>~\u003c/p>\n"],["*~~a~~*","\u003cp>\u003cem>\u003cdel>a\u003c/del>\u003c/em>\u003c/p>\n"],["~~*a*~~","\u003cp>\u003cdel>\u003cem>a\u003c/em>\u003c/del>\u003c/p>\n"],["~~a~~ ~~b~~","\u003cp>\u003cdel>a\u003c/del> \u003cdel>b\u003c/del>\u003c/p>\n"],["~~a ~~b~~ c~~","\u003cp>\u003cdel>a \u003cdel>b\u003c/del> c\u003c/del>\u003c/p>\n"],["text ~~with `code` inside~~","\u003cp>text \u003cdel>with \u003ccode>code\u003c/code> inside\u003c/del>\u003c/p>\n"],["~~[link](/x)~~","\u003cp>\u003cdel>\u003ca href=\"/x\">link\u003c/a>\u003c/del>\u003c/p>\n"],["~~a\nb~~","\u003cp>\u003cdel>a\nb\u003c/del>\u003c/p>\n"],["# Title\n\nSome *intro* text with a [link](https://example.org) and `code`.\n\n## List\n\n- one\n- two\n  - nested\n- three\n\n1. first\n2. second\n\n> quote\n> continues\n\n```js\nconst a = 1;\n```\n\n---\n\nEnd.","\u003ch1>Title\u003c/h1>\n\u003cp>Some \u003cem>intro\u003c/em> text with a \u003ca href=\"https://example.org\">link\u003c/a> and \u003ccode>code\u003c/code>.\u003c/p>\n\u003ch2>List\u003c/h2>\n\u003cul>\n\u003cli>one\u003c/li>\n\u003cli>two\n\u003cul>\n\u003cli>nested\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003cli>three\u003c/li>\n\u003c/ul>\n\u003col>\n\u003cli>first\u003c/li>\n\u003cli>second\u003c/li>\n\u003c/ol>\n\u003cblockquote>\n\u003cp>quote\ncontinues\u003c/p>\n\u003c/blockquote>\n\u003cpre>\u003ccode class=\"language-js\">const a = 1;\n\u003c/code>\u003c/pre>\n\u003chr>\n\u003cp>End.\u003c/p>\n"],["Setext\n======\n\nParagraph one\nstill one.\n\n    indented code\n\n* a\n\n* b\n\n[ref]: /somewhere \"Title\"\n\nSee [ref] and ![img](a.png \"t\").","\u003ch1>Setext\u003c/h1>\n\u003cp>Paragraph one\nstill one.\u003c/p>\n\u003cpre>\u003ccode>indented code\n\u003c/code>\u003c/pre>\n\u003cul>\n\u003cli>\n\u003cp>a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>b\u003c/p>\n\u003c/li>\n\u003c/ul>\n\u003cp>See \u003ca href=\"/somewhere\" title=\"Title\">ref\u003c/a> and \u003cimg src=\"a.png\" alt=\"img\" title=\"t\">.\u003c/p>\n"],["1. one\n\n   para\n\n2. two\n   - nested a\n   - nested b\n\n   > quote in item\n\n3. three","\u003col>\n\u003cli>\n\u003cp>one\u003c/p>\n\u003cp>para\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>two\u003c/p>\n\u003cul>\n\u003cli>nested a\u003c/li>\n\u003cli>nested b\u003c/li>\n\u003c/ul>\n\u003cblockquote>\n\u003cp>quote in item\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003cli>\n\u003cp>three\u003c/p>\n\u003c/li>\n\u003c/ol>\n"],["- [ ] a\n- [x] b\n- [X] c","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> a\u003c/li>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> b\u003c/li>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> c\u003c/li>\n\u003c/ul>\n"],["- [ ] one","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> one\u003c/li>\n\u003c/ul>\n"],["* [x] done","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> done\u003c/li>\n\u003c/ul>\n"],["+ [ ] plus","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> plus\u003c/li>\n\u003c/ul>\n"],["1. [ ] first\n2. [x] second","\u003col>\n\u003cli>\u003cinput type=\"checkbox\" disabled> first\u003c/li>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> second\u003c/li>\n\u003c/ol>\n"],["- [ ] a\n  - [x] nested\n  - [ ] nested two","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> a\n\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> nested\u003c/li>\n\u003cli>\u003cinput type=\"checkbox\" disabled> nested two\u003c/li>\n\u003c/ul>\n\u003c/li>\n\u003c/ul>\n"],["- [ ] *emphasis*","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> \u003cem>emphasis\u003c/em>\u003c/li>\n\u003c/ul>\n"],["- [x] [link](/u)","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled checked> \u003ca href=\"/u\">link\u003c/a>\u003c/li>\n\u003c/ul>\n"],["- [ ] `code`","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> \u003ccode>code\u003c/code>\u003c/li>\n\u003c/ul>\n"],["- a\n- [ ] b\n- c","\u003cul>\n\u003cli>a\u003c/li>\n\u003cli>\u003cinput type=\"checkbox\" disabled> b\u003c/li>\n\u003cli>c\u003c/li>\n\u003c/ul>\n"],["- [ ] a\n\n- [x] b","\u003cul>\n\u003cli>\n\u003cp>\u003cinput type=\"checkbox\" disabled> a\u003c/p>\n\u003c/li>\n\u003cli>\n\u003cp>\u003cinput type=\"checkbox\" disabled checked> b\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["- [ ]\n- [x]","\u003cul>\n\u003cli>[ ]\u003c/li>\n\u003cli>[x]\u003c/li>\n\u003c/ul>\n"],["- [x]no space","\u003cul>\n\u003cli>[x]no space\u003c/li>\n\u003c/ul>\n"],["- [] not a task","\u003cul>\n\u003cli>[] not a task\u003c/li>\n\u003c/ul>\n"],["- [ x] not a task","\u003cul>\n\u003cli>[ x] not a task\u003c/li>\n\u003c/ul>\n"],["- [xx] not a task","\u003cul>\n\u003cli>[xx] not a task\u003c/li>\n\u003c/ul>\n"],["-   [ ] wide","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> wide\u003c/li>\n\u003c/ul>\n"],["- [ ] a\n  continued line","\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> a\ncontinued line\u003c/li>\n\u003c/ul>\n"],["- [ ] a\n\n  second paragraph","\u003cul>\n\u003cli>\n\u003cp>\u003cinput type=\"checkbox\" disabled> a\u003c/p>\n\u003cp>second paragraph\u003c/p>\n\u003c/li>\n\u003c/ul>\n"],["> - [ ] in quote","\u003cblockquote>\n\u003cul>\n\u003cli>\u003cinput type=\"checkbox\" disabled> in quote\u003c/li>\n\u003c/ul>\n\u003c/blockquote>\n"],["- > [ ] not first","\u003cul>\n\u003cli>\n\u003cblockquote>\n\u003cp>[ ] not first\u003c/p>\n\u003c/blockquote>\n\u003c/li>\n\u003c/ul>\n"],["x ~~~a~~b~~","\u003cp>x ~\u003cdel>a\u003c/del>b~~\u003c/p>\n"],["x ~~~a~~ b~~","\u003cp>x ~\u003cdel>a\u003c/del> b~~\u003c/p>\n"],["x ~~~a~~~ b~~","\u003cp>x ~\u003cdel>a\u003c/del>~ b~~\u003c/p>\n"],["x ~~a~~~ b~~ c","\u003cp>x \u003cdel>a\u003c/del>~ b~~ c\u003c/p>\n"],["x ~~~a~~b~~c~~","\u003cp>x ~\u003cdel>a\u003c/del>b\u003cdel>c\u003c/del>\u003c/p>\n"],["x ~~~~a~~~~b~~","\u003cp>x \u003cdel>\u003cdel>a\u003c/del>\u003c/del>b~~\u003c/p>\n"],["~one~ and ~~two~~","\u003cp>~one~ and \u003cdel>two\u003c/del>\u003c/p>\n"],["~~one~~ and ~two~","\u003cp>\u003cdel>one\u003c/del> and ~two~\u003c/p>\n"],["costs ~5 to ~10","\u003cp>costs ~5 to ~10\u003c/p>\n"],["a ~~b~ c","\u003cp>a ~~b~ c\u003c/p>\n"],["[x](\u003c javascript:x>)","\u003cp>[x](&lt; javascript:x&gt;)\u003c/p>\n"],["[x](\u003cjava\tscript:x>)","\u003cp>\u003ca href=\"java%09script:x\">x\u003c/a>\u003c/p>\n"],["[x](&#106;avascript:x)","\u003cp>[x](javascript:x)\u003c/p>\n"]];
  var preview = document.getElementById("preview");
  var sourceBox = document.getElementById("source");

  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  window.__copied = [];
  window.copyText = function (text) { window.__copied.push(text); };

  function say(text) { set("input", text); }
  function html() { return sourceBox.textContent; }
  function tiles() { return txt("sWords") + "|" + txt("sHeadings") + "|" + txt("sLinks"); }
  function all(sel) { return Array.prototype.slice.call(preview.querySelectorAll(sel)); }

  /* Two documents are the same if they have the same elements, attributes and
     text once the things the preview is allowed to change are set aside: it
     adds target and rel to a link, sets a table alignment through a property
     rather than a style attribute, and leaves out the line breaks the HTML
     view puts between block tags. Everything else must be identical. */
  var STRUCT = { UL: 1, OL: 1, TABLE: 1, THEAD: 1, TBODY: 1, TR: 1, BLOCKQUOTE: 1 };
  function canon(el) {
    var out = "", pending = "";
    function flush() { if (pending !== "") { out += "[" + pending + "]"; pending = ""; } }
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) {
        if (STRUCT[el.tagName] && c.data.trim() === "") { continue; }
        pending += c.data;
      } else if (c.nodeType === 1) {
        flush();
        var tag = c.tagName.toLowerCase();
        var attrs = [];
        for (var i = 0; i < c.attributes.length; i++) {
          var a = c.attributes[i];
          if (tag === "a" && (a.name === "target" || a.name === "rel")) { continue; }
          if (tag === "input" && a.name === "checked") { continue; }
          /* a language name is put in a class only if it is short and plain; the HTML view keeps whatever was typed */
          if (tag === "code" && a.name === "class" && !/^language-[A-Za-z0-9_+#.\-]{1,40}$/.test(a.value)) { continue; }
          if ((tag === "td" || tag === "th") && a.name === "style") {
            var m = /text-align:\s*(left|right|center)/.exec(a.value);
            attrs.push("align=" + (m ? m[1] : "?" + a.value));
            continue;
          }
          attrs.push(a.name + "=" + a.value);
        }
        if (tag === "input") { attrs.push("checked=" + c.checked); }
        attrs.sort();
        out += "<" + tag + (attrs.length ? " " + attrs.join(" ") : "") + ">" + canon(c) + "<\/" + tag + ">";
      }
    }
    flush();
    return out;
  }
  function parse(markup) {
    return new DOMParser().parseFromString("<!DOCTYPE html><body>" + markup, "text/html").body;
  }

  /* whitespace-only text directly inside a container that holds only blocks */
  function strayWhitespace() {
    var n = 0;
    all("ul, ol, table, thead, tbody, tr, blockquote").forEach(function (el) {
      for (var c = el.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3 && c.data.trim() === "") { n++; }
      }
    });
    return n;
  }

  /* Everything in the preview must be one of the elements and attributes the
     page makes on purpose. This is the check that matters most for a page
     that shows a stranger's text. */
  var ALLOWED = { p: 1, h1: 1, h2: 1, h3: 1, h4: 1, h5: 1, h6: 1, em: 1, strong: 1, del: 1, code: 1, pre: 1,
                  ul: 1, ol: 1, li: 1, blockquote: 1, table: 1, thead: 1, tbody: 1, tr: 1, th: 1, td: 1,
                  a: 1, input: 1, img: 1, br: 1, hr: 1, span: 1 };
  function audit() {
    var bad = [];
    all("*").forEach(function (el) {
      var tag = el.tagName.toLowerCase();
      if (!ALLOWED[tag]) { bad.push("element " + tag); }
      for (var i = 0; i < el.attributes.length; i++) {
        var at = el.attributes[i];
        if (/^on/i.test(at.name)) { bad.push(tag + " has " + at.name); }
        if (at.name === "style" && tag !== "td" && tag !== "th") { bad.push(tag + " has a style"); }
        if ((at.name === "href" || at.name === "src") &&
            !/^(https?:|mailto:|tel:|data:image\/(png|gif|jpeg|webp);)/i.test(at.value)) {
          bad.push(tag + " " + at.name + "=" + at.value.slice(0, 40));
        }
      }
    });
    return bad.join("; ");
  }

  var sample = val("input");

  /* ================= the page as it opens ================= */
  shown("the preview is shown", "preview");
  gone("the HTML view is hidden", "source");
  eq("the sample is rendered", (preview.querySelector("h1") || {}).textContent, "Markdown preview");
  eq("with three second-level headings", all("h2").length, 3);
  eq("bold, italic, struck and code are each their own element",
     ["strong", "em", "del", "code"].map(function (t) { return preview.querySelector("p " + t).textContent; }).join(","),
     "Bold,italic,struck,code");
  eq("five list items in all, two of them nested", all("li").length, 5);
  eq("two task boxes, and only the ticked one is ticked",
     all("input").map(function (b) { return b.type + ":" + b.checked + ":" + b.disabled; }).join(","),
     "checkbox:true:true,checkbox:false:true");
  eq("the nested list is a numbered one inside an item", all("li > ol > li").length, 2);
  eq("the quote is a block quote", (preview.querySelector("blockquote") || {}).textContent.trim(), "A quote can span\nseveral lines.");
  eq("the table has its three headings", all("th").map(function (c) { return c.textContent; }).join(","), "Tool,Free,Uploads");
  eq("and their alignments, left, centre and right",
     all("th").map(function (c) { return c.style.textAlign; }).join(","), "left,center,right");
  eq("the cells carry the same alignment", all("tbody td").map(function (c) { return c.style.textAlign; }).join(","), "left,center,right");
  eq("and it shows: the first column is really left-aligned on screen", getComputedStyle(all("tbody td")[0]).textAlign, "left");
  eq("and the last is really right-aligned", getComputedStyle(all("tbody td")[2]).textAlign, "right");
  eq("the code block keeps its text and its language",
     all("pre code").map(function (c) { return c.className + "|" + c.textContent; }).join(),
     "language-js|const total = items.reduce((sum, item) => sum + item.price, 0);\n");
  var about = preview.querySelector("a");
  eq("the one link goes where it says", about.getAttribute("href"), "https://108toolbox.in/about.html");
  eq("opens in a new tab", about.getAttribute("target"), "_blank");
  eq("without giving the new page this one", about.getAttribute("rel"), "noopener noreferrer nofollow");
  eq("the picture is not fetched: there is no img element", all("img").length, 0);
  eq("a labelled box stands in for it", all(".md-image").map(function (s) { return s.textContent; }).join(), "Image: a picture");
  has("and its tooltip says where it points", all(".md-image")[0].getAttribute("title"), "https://108toolbox.in/x.png");
  eq("words, headings and links are counted", tiles(), "54|4|1");
  eq("the message says HTML is never run and that the picture was not loaded", txt("msg"),
     "Raw HTML in the text is shown as text, never run. 1 image is not loaded in the preview; the HTML view keeps it.");
  eq("there is no stray whitespace inside lists, tables or quotes", strayWhitespace(), 0);
  eq("nothing unexpected is in the preview", audit(), "");
  ok("the HTML view already holds the conversion", html().indexOf("<h1>Markdown preview<\/h1>\n<p>Write on the left") === 0, html().slice(0, 60));
  has("with a ticked box", html(), "<li><input type=\"checkbox\" disabled checked> Write the post<\/li>");
  has("and the image kept exactly as written", html(), "<img src=\"https://108toolbox.in/x.png\" alt=\"a picture\" title=\"pictures from the web are not loaded here\">");

  /* ================= switching between the two views ================= */
  set("view", "html");
  gone("the preview goes", "preview");
  shown("and the HTML shows", "source");
  eq("the HTML view holds text and nothing else", sourceBox.childElementCount, 0);
  set("view", "preview");
  shown("back to the preview", "preview");
  gone("and the HTML goes", "source");

  /* ================= a space between two styled words ================= */
  say("- *a* **b**");
  eq("in a list item the space survives", all("li")[0].textContent, "a b");
  say("- *a*\n  **b**");
  eq("and so does a line break there", all("li")[0].textContent, "a\nb");
  say("| *a* **b** |\n|---|\n| *c* **d** |");
  eq("and in table cells", all("th, td").map(function (c) { return c.textContent; }).join("|"), "a b|c d");

  /* ================= the numbers ================= */
  say("Hello brave new world");
  eq("four words", tiles(), "4|0|0");
  say("# One\n## Two\n### Three words here");
  eq("three headings, five words", tiles(), "5|3|0");
  say("[a](http://x) and [b](/rel) and <https://e.com> and ![i](http://x/i.png)");
  eq("three links, and a picture is not a link", tiles(), "7|0|3");
  say("```\nsome code here\n```\n\nreal words");
  eq("a code block is not counted as words", tiles(), "2|0|0");
  say("use `foo bar` now");
  eq("but a code span is", tiles(), "4|0|0");
  say("| a b | c |\n|---|---|\n| d | e f |");
  eq("table cells are counted", tiles(), "6|0|0");
  say("- one two\n- three");
  eq("list items are counted", tiles(), "3|0|0");
  say("a\\\nb");
  eq("a hard break separates two words", tiles(), "2|0|0");
  say(String.fromCodePoint(0x0C28, 0x0C2E, 0x0C38, 0x0C4D, 0x0C15, 0x0C3E, 0x0C30, 0x0C02) + " " + String.fromCodePoint(0x0C2A, 0x0C4D, 0x0C30, 0x0C2A, 0x0C02, 0x0C1A, 0x0C02));
  eq("words in another script are counted by spaces", tiles(), "2|0|0");
  say("# Only a heading");
  eq("a heading's words count", tiles(), "3|1|0");

  /* ================= which links are followed ================= */
  say("[web](http://a.example/x) [secure](https://a.example/y) [mail](mailto:me@a.example) [phone](tel:+15551234) " +
      "[upper](HTTPS://A.EXAMPLE) [rel](/docs/setup.md) [proto](//evil.example/z) [anchor](#top) [empty]()");
  var links = all("a");
  eq("nine links", links.length, 9);
  eq("five of them are followed",
     links.filter(function (a) { return a.hasAttribute("href"); }).map(function (a) { return a.textContent; }).join(),
     "web,secure,mail,phone,upper");
  eq("each opens in a new tab and protects this page",
     links.filter(function (a) { return a.hasAttribute("href"); })
          .every(function (a) { return a.getAttribute("target") === "_blank" && a.getAttribute("rel") === "noopener noreferrer nofollow"; }), true);
  eq("web and mail and phone addresses are kept as written",
     links.slice(0, 4).map(function (a) { return a.getAttribute("href"); }).join(" "),
     "http://a.example/x https://a.example/y mailto:me@a.example tel:+15551234");
  eq("the other four are shown but not followed",
     links.filter(function (a) { return !a.hasAttribute("href"); }).map(function (a) { return a.textContent + ":" + a.className; }).join(),
     "rel:md-inert,proto:md-inert,anchor:md-inert,empty:md-inert");
  eq("one carries its address in a tooltip", links[5].getAttribute("title"), "Link to /docs/setup.md (not followed in this preview)");
  eq("and none of the four has a target", links.slice(5).some(function (a) { return a.hasAttribute("target"); }), false);
  has("the HTML view keeps the relative address as written", html(), "<a href=\"/docs/setup.md\">rel<\/a>");
  has("and the protocol-relative one", html(), "<a href=\"//evil.example/z\">proto<\/a>");
  eq("the counts include all nine", txt("sLinks"), "9");

  say("[a](http://x \"one \\\" two\")");
  eq("a title keeps its quote", all("a")[0].getAttribute("title"), "one \" two");
  say("[a](http://x\"onmouseover=\"y)");
  eq("a quote inside an address cannot start an attribute", all("a")[0].getAttribute("href"), "http://x%22onmouseover=%22y");
  eq("so the link has only its own attributes", Array.prototype.map.call(all("a")[0].attributes, function (x) { return x.name; }).sort().join(), "href,rel,target");
  say("[a](http://x \"t\\\" onmouseover=\\\"y\")");
  eq("a title cannot start one either", Array.prototype.map.call(all("a")[0].attributes, function (x) { return x.name; }).sort().join(), "href,rel,target,title");

  /* ================= addresses that are never links ================= */
  var REFUSED = ["[x](javascript:window.__x=1)", "[x](JaVaScRiPt:window.__x=1)", "[x](vbscript:x)", "[x](data:text/html,x)",
                 "[x](file:///etc/passwd)", "<javascript:window.__x=1>", "![x](javascript:window.__x=1)",
                 "[a][r]\n\n[r]: javascript:window.__x=1", "[x](<javascript:window.__x=1>)", "![a](data:image/svg+xml;base64,PHN2Zz4=)",
                 "![a](data:image/bmp;base64,Qk0=)",
                 /* the same address hidden behind a space, a tab or a character reference */
                 "[x](< javascript:window.__x=1>)", "[x](<" + String.fromCharCode(9) + "javascript:window.__x=1>)",
                 "[x](&#106;avascript:window.__x=1)", "[x](java&#x73;cript:window.__x=1)"];
  REFUSED.forEach(function (doc) {
    say(doc);
    eq(JSON.stringify(doc) + ": no link, no image", all("a, img, .md-image").length, 0);
    ok(JSON.stringify(doc) + ": the text is shown as typed", /javascript:|vbscript:|data:|file:/.test(preview.textContent.toLowerCase()), preview.textContent);
    eq(JSON.stringify(doc) + ": and no address in the HTML", /href=|src=/.test(html()), false);
  });

  /* a control character inside an address is written as %XX, so a browser cannot skip over it to find a scheme */
  say("[x](<java" + String.fromCharCode(9) + "script:window.__x=1>)");
  has("a tab inside an address is written out as %09", html(), "<a href=\"java%09script:window.__x=1\">");
  eq("and no tab is left in the address", /href="[^"]*\t/.test(html()), false);
  eq("and the preview does not follow it", all("a").length + ":" + all("a[href]").length, "1:0");
  say("[x](<java script:window.__x=1>)");
  has("a space inside an address is written out as %20", html(), "<a href=\"java%20script:window.__x=1\">");

  /* strikethrough takes two tildes on each side, so ~5 to ~10 is left alone */
  say("~one~ and ~~two~~");
  eq("one tilde is text and two make strikethrough", html(), "<p>~one~ and <del>two<\/del><\/p>\n");
  eq("in the preview too", all("del").length + ":" + preview.textContent.trim(), "1:~one~ and two");
  say("costs ~5 to ~10");
  eq("so an approximate price is not struck through", all("del").length, 0);

  /* named character references are the HTML 4 set; anything else stays as typed, and numbers work for any character */
  say("&copy; &#169; &#xA9; &mdash; &colon; &notaname;");
  eq("known references become their characters and unknown ones stay", preview.textContent.trim(),
     String.fromCharCode(169, 32, 169, 32, 169, 32, 0x2014) + " &colon; &notaname;");
  has("and the HTML view escapes the ampersand of an unknown one", html(), "&amp;colon; &amp;notaname;");

  /* ================= pictures ================= */
  say("![alt text](https://a.example/p.png \"the title\")");
  eq("a web picture is not fetched", all("img").length, 0);
  eq("its box says what it is", all(".md-image")[0].textContent, "Image: alt text");
  has("and where it points", all(".md-image")[0].getAttribute("title"), "https://a.example/p.png");
  has("the HTML keeps it", html(), "<img src=\"https://a.example/p.png\" alt=\"alt text\" title=\"the title\">");
  eq("one picture is counted in the message", txt("msg"), "Raw HTML in the text is shown as text, never run. 1 image is not loaded in the preview; the HTML view keeps it.");
  say("![](https://a.example/p.png) ![b](/local.png)");
  eq("a picture without a description says so", all(".md-image").map(function (s) { return s.textContent; }).join(), "Image: no description,Image: b");
  eq("two are counted in the message", txt("msg"), "Raw HTML in the text is shown as text, never run. 2 images are not loaded in the preview; the HTML view keeps them.");
  var PIXELS = { png: "iVBORw0KGgo=", gif: "R0lGOD==", jpeg: "/9j/4AAQ", webp: "UklGRg==" };
  Object.keys(PIXELS).forEach(function (kind) {
    var url = "data:image/" + kind + ";base64," + PIXELS[kind];
    say("![pic " + kind + "](" + url + ")");
    eq("an embedded " + kind + " is shown", all("img").length + ":" + all(".md-image").length, "1:0");
    eq("with its address", all("img")[0].getAttribute("src"), url);
    eq("and its description", all("img")[0].getAttribute("alt"), "pic " + kind);
    eq("and nothing is said about a picture not loaded", txt("msg"), "Raw HTML in the text is shown as text, never run.");
  });
  say("![a](data:image/png;base64,iVBORw0KGgo= \"t\")");
  eq("an embedded picture keeps its title", all("img")[0].getAttribute("title"), "t");

  /* ================= lists, code and tables in the preview ================= */
  say("3. a\n4. b");
  eq("a numbered list keeps its first number", all("ol")[0].getAttribute("start"), "3");
  say("1. a");
  eq("one that starts at 1 says nothing", all("ol")[0].hasAttribute("start"), false);
  say("0. a");
  eq("zero is kept", all("ol")[0].getAttribute("start"), "0");
  say("999999999. a");
  eq("nine digits are kept", all("ol")[0].getAttribute("start"), "999999999");
  say("1234567890. a");
  eq("ten digits are not a list", all("ol").length + ":" + all("p").length, "0:1");
  say("```C++\nx\n```");
  eq("a language with a plus is kept", all("code")[0].className, "language-C++");
  say("```" + new Array(42).join("a") + "\nx\n```");
  eq("a language name of forty-one letters is not put in a class", all("code")[0].className, "");
  has("but the HTML view keeps it", html(), "language-" + new Array(42).join("a"));
  say("```\"><script>window.__x=1<\/script>\nx\n```");
  eq("a language full of markup is not put in a class", all("code")[0].className, "");
  eq("and nothing is made of it", audit(), "");
  say("| l | c | r | n |\n|:--|:-:|--:|---|\n| 1 | 2 | 3 | 4 |");
  eq("all four alignments", all("th").map(function (c) { return c.style.textAlign || "none"; }).join(), "left,center,right,none");
  say("- [ ] open\n- [x] done");
  eq("task boxes are real, disabled check boxes",
     all("input").map(function (b) { return b.type + ":" + b.checked + ":" + b.disabled; }).join(), "checkbox:false:true,checkbox:true:true");
  say("- <input type=checkbox checked> raw");
  eq("a check box typed as HTML is only text", all("input").length, 0);
  say("Line one  \nline two\n\n---\n\nend");
  eq("a hard break and a rule", all("br").length + ":" + all("hr").length, "1:1");

  /* ================= the hand-written documents, compared two ways ================= */
  /* Each is a hand-written document checked against two widely used
     implementations of the standard. The HTML view must give the locked
     result exactly, and the preview must be the same document. */
  var compared = 0, skipped = 0, differences = [];
  GOLDENS.forEach(function (pair) {
    say(pair[0]);
    eq(JSON.stringify(pair[0]).slice(0, 72), html(), pair[1]);
    var safe = pair[1].indexOf("<img") < 0 && !/href="(?!https?:|mailto:|tel:)/i.test(pair[1]);
    if (!safe) { skipped++; return; }
    compared++;
    var want = canon(parse(pair[1]));
    var got = canon(preview);
    if (want !== got && differences.length < 3) { differences.push(JSON.stringify(pair[0]).slice(0, 60) + "\n            want " + want.slice(0, 120) + "\n            got  " + got.slice(0, 120)); }
    else if (want !== got) { differences.push("more"); }
  });
  eq("the preview is the same document as the HTML for every one that has no picture or unfollowed link", differences.length + " differ", "0 differ" + (differences.length ? "\n" + differences.join("\n") : ""));
  ok("and most documents were compared that way", compared > 500, compared + " compared, " + skipped + " skipped");
  eq("the ones left out were only those with a picture or an unfollowed link", compared + skipped, GOLDENS.length);

  /* ================= hostile text ================= */
  var HOSTILE = [
    "<script>window.__x=1<\/script>", "<img src=x onerror=\"window.__x=2\">", "<iframe onload=zq>", "<svg onload=\"window.__x=3\">",
    "<a href=\"javascript:window.__x=4\">click<\/a>", "[click](javascript:window.__x=5)", "<style>body{display:none}<\/style>",
    "<link rel=stylesheet href=//e.example>", "<meta http-equiv=refresh content=\"0;url=//e.example\">", "<base href=\"//e.example/\">",
    "<form action=//e.example><input name=p><\/form>", "<object data=x><\/object><embed src=x>",
    "<div style=\"position:fixed;top:0;left:0;width:9999px;height:9999px\">cover<\/div>",
    "# <script>window.__x=6<\/script>", "| <img src=x onerror=1> | b |\n|---|---|\n| c | d |", "`<script>window.__x=7<\/script>`",
    "```\n<script>window.__x=8<\/script>\n```", "&lt;script&gt;window.__x=9&lt;/script&gt;", "&#60;script&#62;window.__x=10&#60;/script&#62;",
    "[a](http://x \"\\\" onmouseover=\\\"window.__x=11\")", "[<b>x<\/b>](http://x)", "<http://x\"onmouseover=\"window.__x=12>",
    "<\!-- c --><![CDATA[x]]><?php echo 1 ?>", "> <script>window.__x=13<\/script>", "- <script>window.__x=14<\/script>",
    "*<script>window.__x=15<\/script>*", "[<img src=x onerror=1>](http://x)", "![<img src=x onerror=1>](http://x/i.png)",
    "<iframe src=\"javascript:window.__x=16\"><\/iframe>", "<body onload=\"window.__x=17\">", "<math><mi xlink:href=\"data:x\">",
    "<a href=\"data:text/html,<script>window.__x=18<\/script>\">x<\/a>",
  ];
  /* the page has a few of these of its own (its stylesheet link, its policy); what matters is that the count never moves */
  var DANGEROUS = "iframe, object, embed, form, style, link, meta, base, script";
  var before = document.querySelectorAll(DANGEROUS).length;
  HOSTILE.forEach(function (doc) {
    say(doc);
    eq(JSON.stringify(doc).slice(0, 60) + ": only the page's own elements and attributes", audit(), "");
    eq(JSON.stringify(doc).slice(0, 60) + ": no frame, script or style was made", document.querySelectorAll(DANGEROUS).length, before);
    eq(JSON.stringify(doc).slice(0, 60) + ": the HTML view is only text", sourceBox.childElementCount, 0);
  });
  eq("no hostile text ran anything", typeof window.__x, "undefined");
  say("<iframe onload=zq>");
  eq("markup typed into the box is shown as the characters typed", preview.textContent.trim(), "<iframe onload=zq>");
  eq("and made no element", document.querySelectorAll("iframe").length, 0);

  /* ================= how much it takes ================= */
  say(new Array(500001).join("a"));
  eq("exactly the limit is converted", tiles(), "1|0|0");
  var atLimit = preview.textContent.trim().length;
  eq("with all of it shown", atLimit, 500000);
  say(new Array(500002).join("a"));
  has("one over is refused, with both numbers", txt("msg"), formatNumber(500001, 0) + " characters");
  has("and the limit", txt("msg"), formatNumber(500000, 0));
  eq("with nothing shown", preview.textContent + html() + tiles(), "" + "" + DASH + "|" + DASH + "|" + DASH);
  say(new Array(100001).join("word "));
  eq("a hundred thousand words", txt("sWords"), formatNumber(100000, 0));
  var many = "";
  for (var pi = 0; pi < 3000; pi++) { many += "paragraph " + pi + "\n\n"; }
  say(many);
  eq("three thousand paragraphs", all("p").length, 3000);
  eq("and their words", txt("sWords"), formatNumber(6000, 0));
  say(new Array(151).join(">") + " deep");
  eq("quotes nested a hundred and fifty deep stop at a hundred", all("blockquote").length, 100);
  has("with the text kept", preview.textContent, "deep");
  ok("and the page did not give up", txt("msg").indexOf("could not be converted") < 0, txt("msg"));
  var deepList = "";
  for (var di = 0; di < 400; di++) { deepList += new Array(di + 1).join("  ") + "- item\n"; }
  say(deepList);
  ok("a list four hundred levels deep does not break the page", txt("msg").indexOf("could not be converted") < 0 && all("li").length > 10, txt("msg"));
  say(new Array(30001).join("[a](b"));
  ok("thirty thousand unclosed links do not hang the page", txt("msg").indexOf("could not be converted") < 0, txt("msg"));
  say(new Array(20001).join("`` `x "));
  ok("nor do twenty thousand unmatched backticks", txt("msg").indexOf("could not be converted") < 0, txt("msg"));
  say("Type");
  eq("and the page is still working", tiles(), "1|0|0");

  /* ================= the same document twice ================= */
  say(sample);
  var once = preview.innerHTML;
  say(sample);
  eq("converting the same text again gives the same preview, not more of it", preview.innerHTML, once);
  eq("with no stray whitespace in lists, tables or quotes", strayWhitespace(), 0);

  /* ================= copying, saving and clearing ================= */
  say("# Title <b> & \"more\"\n\nBody with `code` & <i>\n\n- one\n- two");
  var expected = html();
  eq("the HTML is the conversion",
     expected, "<h1>Title &lt;b&gt; &amp; &quot;more&quot;<\/h1>\n<p>Body with <code>code<\/code> &amp; &lt;i&gt;<\/p>\n<ul>\n<li>one<\/li>\n<li>two<\/li>\n<\/ul>\n");
  click("copyBtn");
  eq("copy hands over exactly that", window.__copied[window.__copied.length - 1], expected);
  click("dlBtn");
  eq("download hands over one file", window.__saved.length, 1);
  eq("named document.html", window.__saved[0].name, "document.html");
  eq("as HTML", window.__saved[0].blob.type, "text/html");
  window.__saved[0].blob.text().then(function (whole) {
    ok("the file is a whole page", whole.indexOf("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">") === 0, whole.slice(0, 80));
    has("with a viewport", whole, "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">");
    var doc = new DOMParser().parseFromString(whole, "text/html");
    eq("its title is the first heading as plain text", doc.title, "Title <b> & \"more\"");
    eq("its language is English", doc.documentElement.getAttribute("lang"), "en");
    ok("the body starts with one line break and then the document", doc.body.firstChild.nodeType === 3 && doc.body.firstChild.data === "\n", "first child: " + doc.body.firstChild.nodeName);
    doc.body.removeChild(doc.body.firstChild);
    /* the line break after <\/body> is read as part of the body, so the end may hold one more */
    eq("its body is the converted document", canon(doc.body).replace(/\n+\]$/, "\n]"), canon(parse(expected)));
    eq("and it holds no script", doc.querySelectorAll("script").length, 0);
    eq("nothing comes before the body's own text", whole.slice(whole.indexOf("<body>") + 7, whole.indexOf("<body>") + 7 + 4), "<h1>");
    ok("it ends with the closing tags", /<\/body>\n<\/html>\n$/.test(whole), whole.slice(-20));

    say("no heading here, only text");
    click("dlBtn");
    window.__saved[1].blob.text().then(function (plain) {
      eq("without a heading the title is Document", new DOMParser().parseFromString(plain, "text/html").title, "Document");
      say("> # quoted heading\n\ntext");
      click("dlBtn");
      window.__saved[2].blob.text().then(function (quoted) {
        eq("a heading inside a quote does not name the page", new DOMParser().parseFromString(quoted, "text/html").title, "Document");
        say("text\n\n## Second\n\n# Third");
        click("dlBtn");
        window.__saved[3].blob.text().then(function (later) {
          eq("the first heading wins, at any level", new DOMParser().parseFromString(later, "text/html").title, "Second");
          say("# A *styled* [heading](http://x) with `code`");
          click("dlBtn");
          window.__saved[4].blob.text().then(function (styled) {
            eq("a heading's title is its plain text", new DOMParser().parseFromString(styled, "text/html").title, "A styled heading with code");
            say("# a <\/title><script>window.__x=19<\/script>");
            click("dlBtn");
            window.__saved[5].blob.text().then(function (broken) {
            var brokenDoc = new DOMParser().parseFromString(broken, "text/html");
            eq("a heading that tries to close the title is only text inside it", brokenDoc.title, "a <\/title><script>window.__x=19<\/script>");
            eq("and the file has no script in it", brokenDoc.querySelectorAll("script").length, 0);

            /* ---- the two buttons that fill and empty the box ---- */
            click("clearBtn");
            eq("clear empties the box", val("input"), "");
            eq("and the preview and the HTML", preview.childNodes.length + ":" + html(), "0:");
            eq("and the tiles", tiles(), DASH + "|" + DASH + "|" + DASH);
            eq("and asks for something", txt("msg"), "Type or paste some Markdown on the left.");
            eq("and puts the cursor back in the box", document.activeElement.id, "input");
            var copies = window.__copied.length, saves = window.__saved.length;
            click("copyBtn");
            click("dlBtn");
            eq("with nothing to copy, nothing is copied", window.__copied.length, copies);
            eq("and nothing is saved", window.__saved.length, saves);
            say("   \n\t \n");
            eq("only whitespace counts as nothing", txt("msg"), "Type or paste some Markdown on the left.");
            click("sampleBtn");
            eq("the sample button fills the box", val("input"), sample);
            eq("and it is rendered", tiles(), "54|4|1");
            say("");
            eq("typing nothing empties everything again", preview.childNodes.length + ":" + html() + ":" + tiles(), "0::" + DASH + "|" + DASH + "|" + DASH);
            say("back");
            eq("and typing again brings it back", preview.textContent.trim(), "back");
            finish();
            });
          });
        });
      });
    });
  });
"""
# ---- END: the test for markdown-previewer ----

# ---- START: the test for image-to-pdf ----
T["image-to-pdf"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TIMES = String.fromCharCode(0xD7);
  var list = document.getElementById("list");
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(files) {
    var dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function settled() { return txt("msg") !== "" && !/^Reading/.test(txt("msg")); }
  function wait(label, test, budget) {
    return new Promise(function (resolve) { waitFor(label, test, resolve, budget); });
  }
  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* four coloured quarters and a white dot, so a picture on its side is never the same picture */
  function quarters(w, h, ramp) {
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    var x = c.getContext("2d");
    x.fillStyle = "rgb(220,30,30)"; x.fillRect(0, 0, w / 2, h / 2);
    x.fillStyle = "rgb(30,180,60)"; x.fillRect(w / 2, 0, w / 2, h / 2);
    x.fillStyle = "rgb(30,60,220)"; x.fillRect(0, h / 2, w / 2, h / 2);
    x.fillStyle = "rgb(240,220,30)"; x.fillRect(w / 2, h / 2, w / 2, h / 2);
    x.fillStyle = "#fff"; x.fillRect(w / 10, h / 10, h / 5, h / 5);
    if (ramp) {
      var d = x.getImageData(0, 0, w, h);
      for (var i = 0; i < w * h; i++) { d.data[i * 4 + 3] = Math.round(255 * (i % w) / (w - 1)); }
      x.putImageData(d, 0, 0);
    }
    return c;
  }
  function blobOf(canvas, type, quality) {
    return new Promise(function (resolve) { canvas.toBlob(resolve, type, quality); });
  }
  function bytesOf(blob) { return blob.arrayBuffer().then(function (b) { return new Uint8Array(b); }); }

  /* a JPEG with an EXIF block saying "turn me", big-endian, put right after the start marker */
  function withExif(jpeg, orientation) {
    var seg = [0x45, 0x78, 0x69, 0x66, 0, 0, 0x4D, 0x4D, 0, 0x2A, 0, 0, 0, 8, 0, 1,
               0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, orientation, 0, 0, 0, 0, 0, 0];
    var out = new Uint8Array(jpeg.length + 4 + seg.length);
    out.set([0xFF, 0xD8, 0xFF, 0xE1, (seg.length + 2) >> 8, (seg.length + 2) & 255], 0);
    out.set(seg, 6);
    out.set(jpeg.subarray(2), 6 + seg.length);
    return out;
  }

  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function same(a, b) {
    if (!a || !b || a.length !== b.length) { return false; }
    for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) { return false; } }
    return true;
  }
  function inflate(u8) {
    var s = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"));
    return new Response(s).arrayBuffer().then(function (b) { return new Uint8Array(b); });
  }
  function pixelsOf(blob) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () {
        var c = document.createElement("canvas");
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        var x = c.getContext("2d");
        x.drawImage(img, 0, 0);
        var d = x.getImageData(0, 0, c.width, c.height).data;
        var rgb = new Uint8Array(c.width * c.height * 3), alpha = new Uint8Array(c.width * c.height);
        for (var i = 0, j = 0, k = 0; i < d.length; i += 4, j += 3, k++) {
          rgb[j] = d[i]; rgb[j + 1] = d[i + 1]; rgb[j + 2] = d[i + 2]; alpha[k] = d[i + 3];
        }
        resolve({ rgb: rgb, alpha: alpha });
      };
      img.src = URL.createObjectURL(blob);
    });
  }

  /* A PDF reader written for this test, sharing nothing with the page: it
     follows the byte positions in the xref table and fails if any of them is
     off by one, and it takes every stream by its /Length and fails if that
     does not land exactly on "endstream". */
  function readPdf(bytes) {
    var text = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(text);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    if (text.substr(at, 5) !== "xref\n") { throw new Error("startxref does not point at the table"); }
    var head = /^xref\n0 (\d+)\n/.exec(text.slice(at));
    var count = Number(head[1]);
    var first = at + head[0].length;
    if (text.substr(first, 20) !== "0000000000 65535 f \n") { throw new Error("entry 0 is wrong"); }
    var objects = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(text.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]);
      var tag = n + " 0 obj\n";
      if (text.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      var start = off + tag.length;
      var si = text.indexOf("\nstream\n", start);
      var ei = text.indexOf("\nendobj\n", start);
      var obj = { dict: text.slice(start, ei), stream: null };
      if (si > -1 && si < ei) {
        obj.dict = text.slice(start, si);
        var len = Number(/\/Length (\d+)/.exec(obj.dict)[1]);
        var data = si + 8;
        obj.stream = bytes.subarray(data, data + len);
        if (text.substr(data + len, 18) !== "\nendstream\nendobj\n") { throw new Error("object " + n + " has the wrong /Length"); }
      }
      objects[n] = obj;
    }
    var trailer = text.slice(first + count * 20);
    var size = Number(/\/Size (\d+)/.exec(trailer)[1]);
    if (size !== count) { throw new Error("/Size says " + size + ", the table has " + count); }
    var root = objects[Number(/\/Root (\d+) 0 R/.exec(trailer)[1])];
    var pagesObj = objects[Number(/\/Pages (\d+) 0 R/.exec(root.dict)[1])];
    var kids = /\/Kids \[([^\]]*)\]/.exec(pagesObj.dict)[1].match(/\d+ 0 R/g).map(function (k) { return parseInt(k, 10); });
    var pages = kids.map(function (k) {
      var p = objects[k];
      var image = objects[Number(/\/Im0 (\d+) 0 R/.exec(p.dict)[1])];
      var smaskRef = /\/SMask (\d+) 0 R/.exec(image.dict);
      return {
        box: /\/MediaBox \[([^\]]*)\]/.exec(p.dict)[1],
        content: latin1(objects[Number(/\/Contents (\d+) 0 R/.exec(p.dict)[1])].stream),
        image: image,
        smask: smaskRef ? objects[Number(smaskRef[1])] : null
      };
    });
    return { text: text, objects: objects, trailer: trailer, count: /\/Count (\d+)/.exec(pagesObj.dict)[1], pages: pages,
             info: objects[Number(/\/Info (\d+) 0 R/.exec(trailer)[1])] };
  }
  function matrix(content) {
    var m = /^q (\S+) (\S+) (\S+) (\S+) (\S+) (\S+) cm \/Im0 Do Q\n$/.exec(content);
    return m ? m.slice(1).map(Number) : null;
  }
  function close(a, b) {
    if (!a || a.length !== b.length) { return false; }
    return a.every(function (v, i) { return Math.abs(v - b[i]) < 0.0011; });
  }
  /* the expected picture box: fitted inside the margins and centred */
  function fitted(pw, ph, W, H, marginMm) {
    var m = marginMm * 72 / 25.4;
    var s = Math.min((W - 2 * m) / pw, (H - 2 * m) / ph);
    return { w: pw * s, h: ph * s, x: (W - pw * s) / 2, y: (H - ph * s) / 2 };
  }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return bytesOf(saved.blob).then(function (bytes) {
      var pdf = null, error = "";
      try { pdf = readPdf(bytes); } catch (e) { error = e.message; }
      return { saved: saved, bytes: bytes, pdf: pdf, error: error };
    });
  }
  function row(i) { return list.children[i]; }
  function nameOf(i) { return row(i).querySelector(".file-list__name").firstChild.data; }
  function metaOf(i) { return row(i).querySelector(".file-list__meta").textContent; }
  function btn(i, k) { return row(i).querySelectorAll("button")[k]; }

  var F = {};
  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for pictures", txt("msg"), "Choose one or more pictures.");
  ok("download starts disabled", document.getElementById("dlBtn").disabled);
  eq("the tiles start blank", txt("sPages") + txt("sIn") + txt("sOut"), DASH + DASH + DASH);
  eq("orientation is hidden while pages follow the picture", getComputedStyle(document.getElementById("orientField")).display, "none");
  eq("the empty list takes no room", getComputedStyle(list).display, "none");
  ok("the file input takes many files", document.getElementById("file").multiple);

  /* ================= making the test pictures ================= */
  step(function () {
    return Promise.all([blobOf(quarters(160, 100), "image/jpeg", 0.9), blobOf(quarters(40, 30, true), "image/png"),
                        blobOf(quarters(30, 40), "image/png"), blobOf(quarters(8, 8), "image/png")])
      .then(function (b) {
        F.alphaBlob = b[1]; F.plainBlob = b[2];
        return Promise.all(b.map(bytesOf)).then(function (u) {
          F.jpeg = u[0]; F.turnedJpeg = withExif(u[0], 6);
          F.turned = new File([F.turnedJpeg], "a-turned.jpg", { type: "image/jpeg" });
          F.photo = new File([F.jpeg], "b-photo.jpg", { type: "image/jpeg" });
          F.alpha = new File([u[1]], "c-alpha.png", { type: "image/png" });
          F.p10 = new File([u[2]], "page 10.png", { type: "image/png" });
          F.p2 = new File([u[2]], "page 2.png", { type: "image/png" });
          F.small = u[3];
          F.hostile = new File([u[3]], "<iframe onload=zq>.png", { type: "image/png" });
          F.broken = new File([new Uint8Array([0x89, 0x50, 0x4E, 0x47, 1, 2, 3, 4, 5])], "broken.png", { type: "image/png" });
        });
      });
  });

  /* ================= seven files, one of them not a picture ================= */
  step(function () {
    feed([F.turned, F.photo, F.alpha, F.p10, F.broken, F.p2, F.hostile]);
    return wait("the seven files are read", function () { return settled() && list.children.length === 6; }, 20000);
  });
  step(function () {
    eq("six go in, in the order given", [0, 1, 2, 3, 4, 5].map(nameOf).join("|"),
       "1. a-turned.jpg|2. b-photo.jpg|3. c-alpha.png|4. page 10.png|5. page 2.png|6. <iframe onload=zq>.png");
    has("the one that is not a picture is named", txt("msg"), "Could not read broken.png");
    has("and the rest are ready", txt("msg"), "Ready: 6 pages.");
    eq("a name written as markup stays text", document.querySelectorAll("iframe").length, 0);
    eq("the turned photo is listed the way it is shown, and kept as it is", metaOf(0), "100" + TIMES + "160 px, " + formatBytes(F.turned.size) + ", kept as it is");
    eq("a PNG is listed with its size and is redrawn", metaOf(2), "40" + TIMES + "30 px, " + formatBytes(F.alpha.size));
    eq("six pages", txt("sPages"), "6");
    eq("the pictures' sizes added up", txt("sIn"), formatBytes(F.turned.size + F.photo.size + F.alpha.size + F.p10.size + F.p2.size + F.hostile.size));
    ok("download is on", !document.getElementById("dlBtn").disabled);
    eq("the first can not move up", btn(0, 0).disabled, true);
    eq("the last can not move down", btn(5, 1).disabled, true);
    eq("a middle one can move both ways", btn(2, 0).disabled + "," + btn(2, 1).disabled, "false,false");
    has("every button says what it does", btn(1, 2).getAttribute("aria-label"), "Turn b-photo.jpg a quarter clockwise");
    return download();
  });
  step(function (d) {
    ok("the PDF reads back cleanly: every object where the table says, every length right", d && d.pdf, d ? d.error : "no download");
    if (!d || !d.pdf) { return; }
    F.first = d;
    eq("named for several pictures", d.saved.name, "pictures.pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("the PDF tile is the file's size", txt("sOut"), formatBytes(d.bytes.length));
    eq("it starts as a PDF 1.4 file", latin1(d.bytes.subarray(0, 9)), "%PDF-1.4\n");
    eq("with the binary marker line", Array.prototype.slice.call(d.bytes.subarray(9, 15)).join(","), "37,226,227,207,211,10");
    eq("six pages in the page tree", d.pdf.count + "/" + d.pdf.pages.length, "6/6");
    has("it says what made it", d.pdf.info.dict, "/Producer (108toolbox.in Image to PDF)");
    var p = d.pdf.pages;
    eq("the turned photo's page is tall", p[0].box, "0 0 75 120");
    ok("and it is turned by the drawing, not by redrawing", close(matrix(p[0].content), [0, -120, 75, 0, 0, 120]), p[0].content);
    ok("its JPEG is in the PDF byte for byte, camera note and all", same(p[0].image.stream, F.turnedJpeg));
    has("as a JPEG", p[0].image.dict, "/Width 160 /Height 100 /BitsPerComponent 8 /ColorSpace /DeviceRGB /Filter /DCTDecode");
    eq("the plain photo's page is wide", p[1].box, "0 0 120 75");
    ok("and drawn straight", close(matrix(p[1].content), [120, 0, 0, 75, 0, 0]), p[1].content);
    ok("and byte for byte too", same(p[1].image.stream, F.jpeg));
    eq("the see-through PNG's page", p[2].box, "0 0 30 22.5");
    has("is stored without loss", p[2].image.dict, "/ColorSpace /DeviceRGB /Filter /FlateDecode /SMask");
    ok("with a soft mask for the see-through part", p[2].smask !== null);
    eq("an opaque PNG has no mask", p[3].smask, null);
    eq("nothing else in the file is a stream than pictures and drawings",
       Object.keys(d.pdf.objects).filter(function (k) { return d.pdf.objects[k].stream; }).length, 6 + 6 + 1);
    return Promise.all([inflate(p[2].image.stream), inflate(p[2].smask.stream), inflate(p[3].image.stream),
                        pixelsOf(F.alphaBlob), pixelsOf(F.plainBlob)]).then(function (r) {
      ok("the PNG's colours come back exactly", same(r[0], r[3].rgb), r[0].length + " vs " + r[3].rgb.length);
      ok("and its transparency exactly", same(r[1], r[3].alpha));
      ok("the opaque PNG's colours exactly", same(r[2], r[4].rgb));
      ok("the transparency really varies", r[1][0] < 20 && r[1][39] > 235, r[1][0] + " .. " + r[1][39]);
    });
  });

  /* ================= page sizes, orientation and margins ================= */
  step(function () {
    set("size", "a4");
    eq("orientation appears for paper sizes", getComputedStyle(document.getElementById("orientField")).display !== "none", true);
    return download();
  });
  step(function (d) {
    var p = d.pdf.pages;
    eq("on A4 the tall picture gets an upright page", p[0].box, "0 0 595.28 841.89");
    eq("and the wide one a sideways page", p[1].box, "0 0 841.89 595.28");
    var b = fitted(120, 75, 841.89, 595.28, 0);
    ok("the wide one fills the width and is centred", close(matrix(p[1].content), [b.w, 0, 0, b.h, b.x, b.y]), p[1].content);
    var t = fitted(75, 120, 595.28, 841.89, 0);
    ok("the turned one is fitted and still turned", close(matrix(p[0].content), [0, -t.h, t.w, 0, t.x, t.y + t.h]), p[0].content);
    set("orient", "portrait");
    return download();
  });
  step(function (d) {
    eq("portrait makes every page upright", d.pdf.pages[1].box, "0 0 595.28 841.89");
    var b = fitted(120, 75, 595.28, 841.89, 0);
    ok("and the wide picture is fitted across it", close(matrix(d.pdf.pages[1].content), [b.w, 0, 0, b.h, b.x, b.y]), d.pdf.pages[1].content);
    set("orient", "landscape");
    return download();
  });
  step(function (d) {
    eq("landscape makes every page sideways", d.pdf.pages[0].box, "0 0 841.89 595.28");
    set("orient", "auto");
    set("margin", "20");
    return download();
  });
  step(function (d) {
    var b = fitted(120, 75, 841.89, 595.28, 20);
    ok("a 20 mm margin is kept clear", close(matrix(d.pdf.pages[1].content), [b.w, 0, 0, b.h, b.x, b.y]), d.pdf.pages[1].content);
    ok("on every side", b.x >= 56.69 && b.y >= 56.69);
    set("size", "letter");
    set("margin", "10");
    return download();
  });
  step(function (d) {
    eq("US Letter, upright", d.pdf.pages[0].box, "0 0 612 792");
    eq("and sideways", d.pdf.pages[1].box, "0 0 792 612");
    var b = fitted(120, 75, 792, 612, 10);
    ok("with a 10 mm margin", close(matrix(d.pdf.pages[1].content), [b.w, 0, 0, b.h, b.x, b.y]), d.pdf.pages[1].content);
    set("size", "fit");
    return download();
  });
  step(function (d) {
    eq("back to the picture's own size, the margin goes around it", d.pdf.pages[1].box, "0 0 176.6929 131.6929");
    ok("with the picture inside it", close(matrix(d.pdf.pages[1].content), [120, 0, 0, 75, 28.3465, 28.3465]), d.pdf.pages[1].content);
    set("margin", "0");
    eq("orientation hides again", getComputedStyle(document.getElementById("orientField")).display, "none");
  });

  /* ================= changing the list ================= */
  step(function () {
    btn(0, 1).click();
    eq("down swaps the first two", nameOf(0) + "|" + nameOf(1), "1. b-photo.jpg|2. a-turned.jpg");
    btn(0, 2).click();
    eq("a quarter turn shows the picture on its side", metaOf(0), "100" + TIMES + "160 px, " + formatBytes(F.photo.size) + ", kept as it is");
    eq("and turns its little picture", row(0).querySelector("img").style.transform, "rotate(90deg)");
    return download();
  });
  step(function (d) {
    eq("a plain photo turned once gets the same page as one the camera marked", d.pdf.pages[0].box, "0 0 75 120");
    ok("and the same drawing", close(matrix(d.pdf.pages[0].content), [0, -120, 75, 0, 0, 120]), d.pdf.pages[0].content);
    ok("still without touching its bytes", same(d.pdf.pages[0].image.stream, F.jpeg));
    btn(0, 2).click();
    return download();
  });
  step(function (d) {
    ok("two turns turn it upside down", close(matrix(d.pdf.pages[0].content), [-120, 0, 0, -75, 120, 75]), d.pdf.pages[0].content);
    btn(0, 2).click();
    return download();
  });
  step(function (d) {
    ok("three turns, a quarter the other way", close(matrix(d.pdf.pages[0].content), [0, 120, -75, 0, 75, 0]), d.pdf.pages[0].content);
    btn(0, 2).click();
    return download();
  });
  step(function (d) {
    ok("four turns, back where it started", close(matrix(d.pdf.pages[0].content), [120, 0, 0, 75, 0, 0]), d.pdf.pages[0].content);
    eq("and its little picture straight again", row(0).querySelector("img").style.transform, "rotate(0deg)");
    btn(1, 0).click();
    eq("up moves it back", nameOf(0), "1. a-turned.jpg");
    btn(5, 3).click();
    eq("remove takes one out", list.children.length, 5);
    eq("and the pages follow", txt("sPages"), "5");
    click("sortBtn");
    var names = [0, 1, 2, 3, 4].map(nameOf).join("|");
    eq("sort puts names in order, with numbers in their natural order", names,
       "1. a-turned.jpg|2. b-photo.jpg|3. c-alpha.png|4. page 2.png|5. page 10.png");
  });

  /* ================= every camera turn, judged by the browser itself =================
     For each of the eight EXIF turns the stored photo is drawn with the
     PDF's own matrix (turned from PDF coordinates, y up, into canvas
     coordinates, y down) and compared, pixel by pixel, with the same file as
     the browser shows it. The browser obeys EXIF on its own, so it is the
     referee here, and the page's table of turns is never consulted. */
  function withExifLE(jpeg, orientation) {
    var seg = [0x45, 0x78, 0x69, 0x66, 0, 0, 0x49, 0x49, 0x2A, 0, 8, 0, 0, 0, 1, 0,
               0x12, 0x01, 3, 0, 1, 0, 0, 0, orientation, 0, 0, 0, 0, 0, 0, 0];
    var out = new Uint8Array(jpeg.length + 4 + seg.length);
    out.set([0xFF, 0xD8, 0xFF, 0xE1, (seg.length + 2) >> 8, (seg.length + 2) & 255], 0);
    out.set(seg, 6);
    out.set(jpeg.subarray(2), 6 + seg.length);
    return out;
  }
  function imageOf(blob) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { resolve(null); };
      img.src = URL.createObjectURL(blob);
    });
  }
  function canvasPixels(c) { return c.getContext("2d").getImageData(0, 0, c.width, c.height).data; }
  function meanDiff(a, b) {
    if (a.length !== b.length) { return 999; }
    var sum = 0;
    for (var i = 0; i < a.length; i += 4) { sum += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]); }
    return sum / (a.length / 4 * 3);
  }
  step(function () {
    click("resetBtn");
    F.turnFiles = [];
    for (var n = 1; n <= 8; n++) {
      F.turnFiles.push({ n: "EXIF " + n, bytes: withExif(F.jpeg, n) });
    }
    F.turnFiles.push({ n: "little-endian EXIF 8", bytes: withExifLE(F.jpeg, 8) });
    F.turnFiles.push({ n: "little-endian EXIF 5", bytes: withExifLE(F.jpeg, 5) });
    feed(F.turnFiles.map(function (t, i) { return new File([t.bytes], "turn" + (i + 1) + ".jpg", { type: "image/jpeg" }); }));
    return wait("ten turned photos are read", function () { return settled() && list.children.length === 10; }, 40000);
  });
  step(function () {
    ok("every one of them is kept as it is", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].every(function (i) { return /kept as it is$/.test(metaOf(i)); }));
    return download();
  });
  step(function (d) {
    var raw;
    return imageOf(new Blob([F.jpeg], { type: "image/jpeg" })).then(function (img) {
      raw = img;
      var checks = F.turnFiles.map(function (t, i) {
        return imageOf(new Blob([t.bytes], { type: "image/jpeg" })).then(function (shown) {
          var page = d.pdf.pages[i];
          var box = page.box.split(" ").map(Number);
          var m = matrix(page.content);
          var k = 4 / 3;
          var W = 160, H = 100;
          var pageH = box[3];
          var cw = Math.round(box[2] * k), ch = Math.round(box[3] * k);
          eq(t.n + ": the page is the size the browser shows the photo at", cw + TIMES + ch, shown.naturalWidth + TIMES + shown.naturalHeight);
          var drawn = document.createElement("canvas");
          drawn.width = cw; drawn.height = ch;
          var g = drawn.getContext("2d");
          g.setTransform(k * m[0] / W, -k * m[1] / W, -k * m[2] / H, k * m[3] / H, k * (m[2] + m[4]), k * (pageH - m[3] - m[5]));
          g.drawImage(raw, 0, 0);
          var seen = document.createElement("canvas");
          seen.width = cw; seen.height = ch;
          seen.getContext("2d").drawImage(shown, 0, 0);
          var diff = meanDiff(canvasPixels(drawn), canvasPixels(seen));
          ok(t.n + ": the PDF draws it exactly the way the browser shows it", diff < 3, "mean difference " + diff.toFixed(1));
          ok(t.n + ": with the file's bytes untouched", same(page.image.stream, t.bytes));
        });
      });
      return Promise.all(checks);
    });
  });

  /* ================= EXIF the way real cameras write it =================
     A general referee: whatever is in the PDF (a JPEG drawn with a matrix,
     or plain pixels) is drawn here the way a PDF reader would draw it, and
     compared with the original file as the browser shows it. */
  function exifSegment(orientation, makeAndModelFirst) {
    var entries = [];
    if (makeAndModelFirst) {
      entries.push([0x01, 0x0F, 0, 2, 0, 0, 0, 4, 0x43, 0x61, 0x6D, 0]);
      entries.push([0x01, 0x10, 0, 2, 0, 0, 0, 4, 0x4F, 0x6E, 0x65, 0]);
    }
    entries.push([0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, orientation, 0, 0]);
    var seg = [0x45, 0x78, 0x69, 0x66, 0, 0, 0x4D, 0x4D, 0, 0x2A, 0, 0, 0, 8, 0, entries.length];
    entries.forEach(function (e) { seg = seg.concat(e); });
    seg = seg.concat([0, 0, 0, 0]);
    return [0xFF, 0xE1, (seg.length + 2) >> 8, (seg.length + 2) & 255].concat(seg);
  }
  function insertAt(jpeg, pos, segment) {
    var out = new Uint8Array(jpeg.length + segment.length);
    out.set(jpeg.subarray(0, pos), 0);
    out.set(segment, pos);
    out.set(jpeg.subarray(pos), pos + segment.length);
    return out;
  }
  /* the byte just after the frame header, and a copy of a JPEG with every APP1 block taken out */
  function afterFrame(jpeg) {
    var pos = 2;
    while (pos < jpeg.length) {
      var marker = jpeg[pos + 1], len = (jpeg[pos + 2] << 8) | jpeg[pos + 3];
      if (marker === 0xC0) { return pos + 2 + len; }
      pos += 2 + len;
    }
    return -1;
  }
  function withoutApp1(jpeg) {
    var keep = [jpeg.subarray(0, 2)], pos = 2;
    while (pos < jpeg.length) {
      var marker = jpeg[pos + 1];
      if (marker === 0xDA) { keep.push(jpeg.subarray(pos)); break; }
      var len = (jpeg[pos + 2] << 8) | jpeg[pos + 3];
      if (marker !== 0xE1) { keep.push(jpeg.subarray(pos, pos + 2 + len)); }
      pos += 2 + len;
    }
    return new Blob(keep, { type: "image/jpeg" });
  }
  function drawLikeAReader(page) {
    var box = page.box.split(" ").map(Number), m = matrix(page.content), k = 4 / 3;
    var W = Number(/\/Width (\d+)/.exec(page.image.dict)[1]), H = Number(/\/Height (\d+)/.exec(page.image.dict)[1]);
    var source;
    if (/DCTDecode/.test(page.image.dict)) {
      source = imageOf(withoutApp1(page.image.stream));
    } else {
      source = Promise.all([inflate(page.image.stream), page.smask ? inflate(page.smask.stream) : null]).then(function (r) {
        var c = document.createElement("canvas"); c.width = W; c.height = H;
        var data = new ImageData(W, H);
        for (var i = 0; i < W * H; i++) {
          data.data[i * 4] = r[0][i * 3]; data.data[i * 4 + 1] = r[0][i * 3 + 1]; data.data[i * 4 + 2] = r[0][i * 3 + 2];
          data.data[i * 4 + 3] = r[1] ? r[1][i] : 255;
        }
        c.getContext("2d").putImageData(data, 0, 0);
        return c;
      });
    }
    return source.then(function (src) {
      var out = document.createElement("canvas");
      out.width = Math.round(box[2] * k); out.height = Math.round(box[3] * k);
      var g = out.getContext("2d");
      g.fillStyle = "#fff"; g.fillRect(0, 0, out.width, out.height);
      g.setTransform(k * m[0] / W, -k * m[1] / W, -k * m[2] / H, k * m[3] / H, k * (m[2] + m[4]), k * (box[3] - m[3] - m[5]));
      g.drawImage(src, 0, 0);
      return out;
    });
  }
  function asTheBrowserShows(bytes, type, width, height) {
    return imageOf(new Blob([bytes], { type: type })).then(function (img) {
      var out = document.createElement("canvas");
      out.width = width; out.height = height;
      var g = out.getContext("2d");
      g.fillStyle = "#fff"; g.fillRect(0, 0, width, height);
      g.drawImage(img, 0, 0, width, height);
      return { canvas: out, w: img.naturalWidth, h: img.naturalHeight };
    });
  }
  step(function () {
    click("resetBtn");
    F.real = [
      { n: "orientation after Make and Model", bytes: insertAt(F.jpeg, 2, exifSegment(6, true)) },
      { n: "EXIF placed after the frame header", bytes: insertAt(F.jpeg, afterFrame(F.jpeg), exifSegment(8, true)) },
      { n: "two EXIF blocks, the first saying upright", bytes: insertAt(insertAt(F.jpeg, 2, exifSegment(6, false)), 2, exifSegment(1, true)) }
    ];
    feed(F.real.map(function (t, i) { return new File([t.bytes], "real" + (i + 1) + ".jpg", { type: "image/jpeg" }); }));
    return wait("three photos with real-world EXIF are read", function () { return settled() && list.children.length === 3; }, 30000);
  });
  step(function () { return download(); });
  step(function (d) {
    return Promise.all(F.real.map(function (t, i) {
      var page = d.pdf.pages[i];
      return drawLikeAReader(page).then(function (drawn) {
        return asTheBrowserShows(t.bytes, "image/jpeg", drawn.width, drawn.height).then(function (shown) {
          eq(t.n + ": the page has the shape the browser shows", drawn.width + TIMES + drawn.height, shown.w + TIMES + shown.h);
          var diff = meanDiff(canvasPixels(drawn), canvasPixels(shown.canvas));
          ok(t.n + ": and the PDF shows what the browser shows", diff < 3, "mean difference " + diff.toFixed(1));
          ok(t.n + ": kept as it is", /kept as it is$/.test(metaOf(i)), metaOf(i));
        });
      });
    }));
  });

  /* ================= JPEGs that are kept and JPEGs that are redrawn, and odd sizes ================= */
  step(function () {
    click("resetBtn");
    var JPEGS = {"GRAY": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/wAALCAAQABgBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AMb/AIWB4B/4Z7/4RP8A4Qn/AIr7+0ftH/CR7x/q92d2/O/7n7ryMeX/AMtc7+K5jwD/AMv3/bP/ANmrra+da+tP2Cv+Z5/7cf8A24r60r//2Q==", "PROG": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wgARCAAQABgDASIAAhEBAxEB/8QAGAAAAwEBAAAAAAAAAAAAAAAAAAUHBAb/xAAXAQADAQAAAAAAAAAAAAAAAAADBQYH/9oADAMBAAIQAxAAAAHEs5CtGaqi3mQR/wD/xAAYEAACAwAAAAAAAAAAAAAAAAAEEAYVFv/aAAgBAQABBQKwAzwDgS//xAAiEQAABAUFAQAAAAAAAAAAAAABBRESAAIDBFETFjFjouH/2gAIAQMBAT8BvTqU6tahaFNmoKq5yIITcIGMxtbu8/Y//8QAIxEAAAMGBwAAAAAAAAAAAAAAAQURAAISFTFRAwQTIWOh4f/aAAgBAgEBPwE/IJOXYmf1YoE2hSrwBVRvZp5x9+N//8QAJBAAAAIIBwAAAAAAAAAAAAAAEBQAAgQREhMhJDFBYYGCwvD/2gAIAQEABj8CKEr+ZEY09R2GaL7C3cOwf//EABwQAAEDBQAAAAAAAAAAAAAAADEAESBRgaHR8P/aAAgBAQABPyHKZqLkbRdrxL//2gAMAwEAAgADAAAAELv/AP/EABgRAQEBAQEAAAAAAAAAAAAAAAERIQBB/9oACAEDAQE/EFFxJqmAsHIAC0PHl//EABsRAAIBBQAAAAAAAAAAAAAAAAERIRAxQWGB/9oACAECAQE/EDGIY5vBErmlunf/xAAZEAABBQAAAAAAAAAAAAAAAAAxIFFhgaH/2gAIAQEAAT8QwC3C18oqDP/Z", "CMYK": "/9j/7gAOQWRvYmUAZAAAAAAA/9sAQwADAgIDAgIDAwMDBAMDBAUIBQUEBAUKBwcGCAwKDAwLCgsLDQ4SEA0OEQ4LCxAWEBETFBUVFQwPFxgWFBgSFBUU/8AAFAgAEAAYBEMRAE0RAFkRAEsRAP/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/aAA4EQwBNAFkASwAAPwD9B/ttl/Yf2b7J/pvmbvPz2+vXpxjp36181f8AC1fhX/wyd/wgn/Ct/wDi6f8Aa/2r/hL/ADF/1W/dv358z/V/uPs2PK/5bbvM4r5q/wCFq/Cv/hk7/hBP+Fb/APF0/wC1/tX/AAl/mL/qt+7fvz5n+r/cfZseV/y23eZxX6p1+eH/AAVY/wCaXf8AcU/9tK9Y/wCCd/8AzUD/ALh//tzXm/gn/l9/4B/7NRXwFX2RXUUUV+X9ftVRX6Mf8Egv+as/9wn/ANva+1f+Cbv/ADUT/uHf+3VfAX/BVj/ml3/cU/8AbSiv0Yr7Vr4Cor//2Q=="};
    function fromB64(s) { var b = atob(s); var u = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) { u[i] = b.charCodeAt(i); } return u; }
    var dot = document.createElement("canvas"); dot.width = 1; dot.height = 1;
    var strip = quarters(20000, 4);
    return Promise.all([blobOf(dot, "image/png"), blobOf(strip, "image/png")]).then(function (b) {
      F.odd = [fromB64(JPEGS.GRAY), fromB64(JPEGS.PROG), fromB64(JPEGS.CMYK)];
      feed([new File([F.odd[0]], "grey.jpg", { type: "image/jpeg" }),
            new File([F.odd[1]], "progressive.jpg", { type: "image/jpeg" }),
            new File([F.odd[2]], "cmyk.jpg", { type: "image/jpeg" }),
            new File([b[0]], "dot.png", { type: "image/png" }),
            new File([b[1]], "strip.png", { type: "image/png" })]);
      return wait("five odd pictures are read", function () { return settled() && list.children.length === 5; }, 40000);
    });
  });
  step(function () {
    has("a grey JPEG is kept as it is", metaOf(0), "kept as it is");
    has("so is a progressive one", metaOf(1), "kept as it is");
    ok("a CMYK JPEG is redrawn, because PDF readers disagree about its colours", !/kept as it is/.test(metaOf(2)), metaOf(2));
    return download();
  });
  step(function (d) {
    var p = d.pdf.pages;
    has("the grey JPEG stays grey", p[0].image.dict, "/ColorSpace /DeviceGray /Filter /DCTDecode");
    has("the progressive one stays a JPEG", p[1].image.dict, "/ColorSpace /DeviceRGB /Filter /DCTDecode");
    has("the CMYK one is stored as plain colours", p[2].image.dict, "/ColorSpace /DeviceRGB /Filter /FlateDecode");
    eq("a 1-pixel picture still gets the smallest page a reader accepts", p[3].box, "0 0 3 3");
    ok("with the pixel in the middle of it", close(matrix(p[3].content), [0.75, 0, 0, 0.75, 1.125, 1.125]), p[3].content);
    eq("a picture wider than any reader accepts is shrunk to the widest page there is", p[4].box, "0 0 14400 3");
    ok("keeping its shape", close(matrix(p[4].content), [14400, 0, 0, 2.88, 0, 0.06]), p[4].content);
    return Promise.all(["grey", "progressive", "CMYK"].map(function (label, i) {
      return drawLikeAReader(p[i]).then(function (drawn) {
        return asTheBrowserShows(F.odd[i], "image/jpeg", drawn.width, drawn.height).then(function (shown) {
          var diff = meanDiff(canvasPixels(drawn), canvasPixels(shown.canvas));
          ok("the " + label + " JPEG looks in the PDF the way the browser shows it", diff < 3, "mean difference " + diff.toFixed(1));
        });
      });
    }));
  });

  /* ================= one picture, clearing, and a reset mid-way ================= */
  step(function () {
    click("resetBtn");
    eq("clear empties the list", list.children.length, 0);
    eq("and asks again", txt("msg"), "Choose one or more pictures.");
    ok("and turns download off", document.getElementById("dlBtn").disabled);
    eq("and blanks the tiles", txt("sPages") + txt("sIn") + txt("sOut"), DASH + DASH + DASH);
    var before = window.__saved.length;
    click("dlBtn");
    eq("with nothing in it, nothing is saved", window.__saved.length, before);
    feed([F.photo]);
    return wait("one photo is read", function () { return settled() && list.children.length === 1; }, 10000);
  });
  step(function () {
    return download();
  });
  step(function (d) {
    eq("one picture's PDF is named after it", d.saved.name, "b-photo.pdf");
    eq("one page", d.pdf.count, "1");
    click("resetBtn");
    feed([F.photo, F.alpha, F.p2, F.p10, F.turned]);
    click("resetBtn");
    return pause(1500);
  });
  step(function () {
    eq("a clear while pictures are still being read leaves the list empty", list.children.length, 0);
    eq("and nothing half-read turns up afterwards", txt("msg"), "Choose one or more pictures.");
    feed([F.broken]);
    return wait("a file that is not a picture is turned away", function () { return settled(); }, 10000);
  });
  step(function () {
    has("with its name", txt("msg"), "Could not read broken.png");
    ok("and no PDF", document.getElementById("dlBtn").disabled);
    click("resetBtn");

    /* ================= more than 200 ================= */
    var many = [];
    for (var i = 0; i < 201; i++) { many.push(new File([F.small], "p" + i + ".png", { type: "image/png" })); }
    feed(many);
    return wait("two hundred and one pictures are read", function () { return settled(); }, 400000);
  });
  step(function () {
    eq("two hundred go in", txt("sPages"), "200");
    has("and the visitor is told the rest were left out", txt("msg"), "Only 200 pictures fit in one PDF");
    return download();
  });
  step(function (d) {
    eq("the PDF has all two hundred pages", d && d.pdf ? d.pdf.count : "no pdf", "200");
    click("resetBtn");
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for image-to-pdf ----

# ---- START: the test for merge-pdf ----
T["merge-pdf"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var list = document.getElementById("list");
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(files) {
    var dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function settled() { return txt("msg") !== "" && !/^Reading/.test(txt("msg")); }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }

  /* A PDF written by hand for the test: numbered bodies, an exact table.
     A body is a string, or { dict, data } for a stream ("LEN" in the
     dictionary becomes the length of data). */
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  function text(s) { return { dict: "<<\/Length LEN>>", data: "BT /F1 12 Tf 10 10 Td (" + s + ") Tj ET" }; }

  /* Three pages under a middle node that hands down a paper size, a turn and
     a font; links from page 1 to page 2 and from page 3 to page 1. */
  var DOC_A = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R 6 0 R] /Count 3>>",
    "<<\/Type /Pages /Parent 2 0 R /Kids [4 0 R 5 0 R] /Count 2 /MediaBox [0 0 200 100] /Rotate 90 /Resources <<\/Font <<\/F1 9 0 R>>>>>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 7 0 R /Annots [<<\/Type /Annot /Subtype /Link /Rect [0 0 50 50] /Dest [5 0 R /Fit]>>]>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 8 0 R /Annots [<<\/Type /Annot /Subtype /Text /Rect [0 0 5 5] /P 3 0 R>>]>>",
    "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources <<\/Font <<\/F1 9 0 R>>>> /Contents 10 0 R /Annots [<<\/Type /Annot /Subtype /Link /Rect [0 0 9 9] /Dest [4 0 R /Fit]>>]>>",
    text("A1"), text("A2"),
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    text("A3")
  ]);
  /* one page whose drawing is in two streams, with its resources and size from the root */
  var DOC_B = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 50] /Resources <<\/Font <<\/F1 6 0 R>>>>>>",
    "<<\/Type /Page /Parent 2 0 R /CropBox [0.5 0.25 99.5 49.75] /Contents [4 0 R 5 0 R]>>",
    text("B1a"), text("B1b"),
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Courier#20New#2FBold>>"
  ]);
  var DOC_ENC = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R");
  var DOC_EMPTY = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [] /Count 0>>"]);

  var F = {
    a: new File([DOC_A], "a-report.pdf", { type: "application/pdf" }),
    b: new File([DOC_B], "b-letter.pdf", { type: "application/pdf" }),
    packed: new File([fromB64(TINY.objstm)], "c-packed.pdf", { type: "application/pdf" }),
    hybrid: new File([fromB64(TINY.hybrid)], "d-hybrid.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "e-damaged.pdf", { type: "application/pdf" }),
    updated: new File([fromB64(TINY.incremental)], "f-updated.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    empty: new File([DOC_EMPTY], "empty.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("just some text")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC_B], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* A reader for the joined file, sharing nothing with the page: it follows
     the table to every object and fails if any position or length is off. */
  function readOut(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    var head = /^xref\n0 (\d+)\n/.exec(t.slice(at));
    if (!head) { throw new Error("startxref does not point at the table"); }
    var count = Number(head[1]), first = at + head[0].length, objs = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(t.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]), tag = n + " 0 obj\n";
      if (t.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      var start = off + tag.length, si = t.indexOf("\nstream\n", start), ei = t.indexOf("\nendobj\n", start);
      var o = { body: t.slice(start, ei), data: null };
      if (si > -1 && si < ei) {
        o.body = t.slice(start, si);
        var len = Number(/\/Length (\d+)/.exec(o.body)[1]);
        o.data = t.substr(si + 8, len);
        if (t.substr(si + 8 + len, 18) !== "\nendstream\nendobj\n") { throw new Error("object " + n + " has the wrong /Length"); }
      }
      objs[n] = o;
    }
    var trailer = t.slice(first + count * 20);
    var root = objs[Number(/\/Root (\d+) 0 R/.exec(trailer)[1])];
    var pagesNum = Number(/\/Pages (\d+) 0 R/.exec(root.body)[1]);
    var kids = /\/Kids \[([^\]]*)\]/.exec(objs[pagesNum].body)[1].match(/\d+ 0 R/g).map(function (k) { return parseInt(k, 10); });
    var pages = kids.map(function (k) {
      var body = objs[k].body;
      var refs = [];
      var single = /\/Contents (\d+) 0 R/.exec(body), many = /\/Contents \[([^\]]*)\]/.exec(body);
      if (single) { refs = [Number(single[1])]; }
      else if (many) { refs = many[1].match(/\d+ 0 R/g).map(function (r) { return parseInt(r, 10); }); }
      var font = /\/F1 (\d+) 0 R/.exec(body);
      if (!font) {
        var res = /\/Resources (\d+) 0 R/.exec(body);
        if (res) { font = /\/F1 (\d+) 0 R/.exec(objs[Number(res[1])].body); }
      }
      return {
        num: k, body: body,
        drawn: refs.map(function (r) { var m2 = /\(([^)]*)\) Tj/.exec(objs[r].data || ""); return m2 ? m2[1] : "?"; }).join("+"),
        box: (/\/MediaBox \[([^\]]*)\]/.exec(body) || [])[1],
        turn: (/\/Rotate (\d+)/.exec(body) || [0, "0"])[1],
        parent: Number((/\/Parent (\d+) 0 R/.exec(body) || [])[1]),
        dests: (body.match(/\/Dest \[(\d+) 0 R/g) || []).map(function (d) { return parseInt(d.slice(7), 10); }),
        font: font ? Number(font[1]) : 0
      };
    });
    return { objs: objs, pages: pages, pagesNum: pagesNum, trailer: trailer, count: /\/Count (\d+)/.exec(objs[pagesNum].body)[1], text: t };
  }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return saved.blob.arrayBuffer().then(function (b) {
      var bytes = new Uint8Array(b), out = null, error = "";
      try { out = readOut(bytes); } catch (e) { error = e.message; }
      return { saved: saved, bytes: bytes, out: out, error: error };
    });
  }
  function row(i) { return list.children[i]; }
  function nameOf(i) { return row(i).querySelector(".file-list__name").firstChild.data; }
  function metaOf(i) { return row(i).querySelector(".file-list__meta").textContent; }
  function btn(i, k) { return row(i).querySelectorAll("button")[k]; }
  function drawnOrder(d) { return d.out.pages.map(function (p) { return p.drawn; }).join(","); }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for PDFs", txt("msg"), "Choose two or more PDFs.");
  ok("download starts disabled", document.getElementById("dlBtn").disabled);
  eq("the tiles start blank", txt("sPages") + txt("sIn") + txt("sOut"), DASH + DASH + DASH);
  ok("the file input takes many PDFs", document.getElementById("file").multiple && /pdf/.test(document.getElementById("file").accept));

  /* ================= ten files, five of them good ================= */
  step(function () {
    feed([F.a, F.notPdf, F.b, F.packed, F.enc, F.hybrid, F.damaged, F.empty, F.updated, F.hostile]);
    return wait("the ten files are read", function () { return settled() && list.children.length === 7; }, 30000);
  });
  step(function () {
    eq("the good ones are listed in the order given", [0, 1, 2, 3, 4, 5, 6].map(nameOf).join("|"),
       "1. a-report.pdf|2. b-letter.pdf|3. c-packed.pdf|4. d-hybrid.pdf|5. e-damaged.pdf|6. f-updated.pdf|7. <iframe onload=zq>.pdf");
    eq("a name written as markup stays text", document.querySelectorAll("iframe").length, 0);
    eq("each says how many pages it has", metaOf(0), "3 pages, " + formatBytes(F.a.size));
    eq("one page is one page", metaOf(1), "1 page, " + formatBytes(F.b.size));
    eq("a damaged file read anyway says so", metaOf(4), "1 page, " + formatBytes(F.damaged.size) + ", damaged but readable");
    eq("sound ones, packed or hybrid, do not", [0, 1, 2, 3, 5].map(function (i) { return /damaged/.test(metaOf(i)); }).join(), "false,false,false,false,false");
    has("a file that is not a PDF is named", txt("msg"), "notes.pdf (not a PDF)");
    has("a protected one is named", txt("msg"), "locked.pdf (protected with a password)");
    has("an empty one is named", txt("msg"), "empty.pdf (has no pages)");
    has("and the rest are ready", txt("msg"), "Ready: 7 PDFs, 9 pages.");
    eq("nine pages", txt("sPages"), "9");
    eq("the sizes added up", txt("sIn"), formatBytes([F.a, F.b, F.packed, F.hybrid, F.damaged, F.updated, F.hostile].reduce(function (s, f) { return s + f.size; }, 0)));
    return download();
  });
  step(function (d) {
    ok("the joined PDF reads back cleanly: every object where the table says, every length right", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    var p = d.out.pages;
    eq("named merged.pdf", d.saved.name, "merged.pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("the PDF tile is the file's size", txt("sOut"), formatBytes(d.bytes.length));
    eq("nine pages, in order", drawnOrder(d), "A1,A2,A3,B1a+B1b,T1,T1,T1,T1,B1a+B1b");
    eq("the page tree says nine", d.out.count, "9");
    eq("pages under the middle node keep the size it gave them", p[0].box + "|" + p[1].box, "0 0 200 100|0 0 200 100");
    eq("and its turn", p[0].turn + "|" + p[1].turn, "90|90");
    eq("a page with its own size keeps it, and no turn", p[2].box + "|" + p[2].turn, "0 0 300 300|0");
    eq("a page takes its size from the root too", p[3].box, "0 0 100 50");
    ok("the shared font is copied once and shared still", p[0].font > 0 && p[0].font === p[1].font && p[1].font === p[2].font,
       p[0].font + " " + p[1].font + " " + p[2].font);
    ok("a different file's font is a different object", p[3].font > 0 && p[3].font !== p[0].font);
    eq("the link on page 1 now points at the new page 2", p[0].dests.join(), String(p[1].num));
    eq("the link on page 3 points at the new page 1", p[2].dests.join(), String(p[0].num));
    eq("every page belongs to the new page tree", p.every(function (x) { return x.parent === d.out.pagesNum; }), true);
    eq("the old middle node did not come along", (d.out.text.match(/\/Type \/Pages/g) || []).length, 1);
    eq("one catalog", (d.out.text.match(/\/Type \/Catalog/g) || []).length, 1);
    eq("the updated file's turn is kept", p[7].turn, "90");
    has("a crop box with fractions is written exactly", p[3].body, "/CropBox [0.5 0.25 99.5 49.75]");
    has("a font name with a space and a slash keeps them, escaped", d.out.text, "/BaseFont /Courier#20New#2FBold");
    eq("a note pointing at the page tree's node points at nothing instead", /\/P null/.test(p[1].body), true);
    eq("the packed, hybrid and damaged files' pages are their own size", [4, 5, 6].map(function (i) { return p[i].box; }).join("|"),
       "0 0 150 80|0 0 150 80|0 0 150 80");
    has("it says what made it", d.out.text, "/Producer <313038746F6F6C626F782E696E204D6572676520504446>");
    eq("and it is not encrypted", /\/Encrypt/.test(d.out.trailer), false);
  });

  /* ================= changing the list ================= */
  step(function () {
    eq("the first can not move up", btn(0, 0).disabled, true);
    eq("the last can not move down", btn(6, 1).disabled, true);
    btn(1, 0).click();
    eq("up moves a file up", nameOf(0) + "|" + nameOf(1), "1. b-letter.pdf|2. a-report.pdf");
    return download();
  });
  step(function (d) {
    eq("and its pages come first", drawnOrder(d).split(",").slice(0, 4).join(","), "B1a+B1b,A1,A2,A3");
    eq("the links still find their pages after the move", d.out.pages[1].dests.join(), String(d.out.pages[2].num));
    btn(6, 2).click();
    eq("remove takes one out", list.children.length, 6);
    eq("and its pages", txt("sPages"), "8");
    click("sortBtn");
    eq("sort puts them in order of name", [0, 1, 2, 3, 4, 5].map(nameOf).join("|"),
       "1. a-report.pdf|2. b-letter.pdf|3. c-packed.pdf|4. d-hybrid.pdf|5. e-damaged.pdf|6. f-updated.pdf");
    return download();
  });
  step(function (d) {
    eq("and the joined file follows", drawnOrder(d), "A1,A2,A3,B1a+B1b,T1,T1,T1,T1");
  });

  /* ================= one file, clearing, a clear mid-way ================= */
  step(function () {
    click("resetBtn");
    eq("clear empties the list", list.children.length, 0);
    eq("and asks again", txt("msg"), "Choose two or more PDFs.");
    eq("and blanks the tiles", txt("sPages") + txt("sIn") + txt("sOut"), DASH + DASH + DASH);
    feed([F.a]);
    return wait("one PDF is read", function () { return settled() && list.children.length === 1; }, 10000);
  });
  step(function () {
    eq("one PDF is not enough", txt("msg"), "Add at least one more PDF to join it to.");
    ok("so there is nothing to download", document.getElementById("dlBtn").disabled);
    var before = window.__saved.length;
    click("dlBtn");
    eq("and nothing is saved", window.__saved.length, before);
    click("resetBtn");
    /* A slow disk, made here: reading a file's bytes takes 200 ms. The
       clear comes while the first file is being read, so a read already
       under way must be dropped when it finishes. */
    var realArrayBuffer = Blob.prototype.arrayBuffer;
    Blob.prototype.arrayBuffer = function () {
      var self = this;
      return pause(200).then(function () { return realArrayBuffer.call(self); });
    };
    feed([F.a, F.b, F.packed]);
    return pause(50).then(function () {
      click("resetBtn");
      return pause(1500);
    }).then(function () { Blob.prototype.arrayBuffer = realArrayBuffer; });
  });
  step(function () {
    eq("a clear while files are being read leaves the list empty", list.children.length, 0);
    eq("and nothing half-read turns up", txt("msg"), "Choose two or more PDFs.");
    feed([F.notPdf, F.enc]);
    return wait("two bad files are turned away", function () { return settled(); }, 10000);
  });
  step(function () {
    has("both are named", txt("msg"), "Left out: notes.pdf (not a PDF); locked.pdf (protected with a password).");
    eq("and nothing from before the clear has crept into the list", list.children.length, 0);
    ok("and there is no PDF", document.getElementById("dlBtn").disabled);
    click("resetBtn");
    var many = [];
    for (var i = 0; i < 101; i++) { many.push(new File([DOC_B], "doc" + i + ".pdf", { type: "application/pdf" })); }
    feed(many);
    return wait("a hundred and one PDFs are read", function () { return settled(); }, 400000);
  });
  step(function () {
    eq("a hundred go in", list.children.length, 100);
    has("and the visitor is told the rest were left out", txt("msg"), "Only 100 PDFs can be joined at once");
    return download();
  });
  step(function (d) {
    eq("the joined file has a hundred pages", d && d.out ? d.out.count : "no pdf", "100");
    click("resetBtn");

    /* ================= two more layouts, and numbers in names ================= */
    feed([new File([fromB64(TINY.xref_notype)], "part 10.pdf", { type: "application/pdf" }),
          new File([fromB64(TINY.objstm_badindex)], "part 2.pdf", { type: "application/pdf" }),
          new File([DOC_A], "part 1.pdf", { type: "application/pdf" })]);
    return wait("three more PDFs are read", function () { return settled() && list.children.length === 3; }, 20000);
  });
  step(function () {
    eq("a cross-reference stream with no type column is read as sound", /damaged/.test(metaOf(0)), false);
    eq("so is one whose packed objects are only findable by number", /damaged/.test(metaOf(1)), false);
    click("sortBtn");
    eq("sort puts part 2 before part 10", [0, 1, 2].map(nameOf).join("|"), "1. part 1.pdf|2. part 2.pdf|3. part 10.pdf");
    return download();
  });
  step(function (d) {
    eq("and every page is there, in that order", drawnOrder(d), "A1,A2,A3,T1,T1");
    click("resetBtn");
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for merge-pdf ----

# ---- START: the test for split-pdf ----
T["split-pdf"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  var planned = document.getElementById("planned");
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  function text(s) { return { dict: "<<\/Length LEN>>", data: "BT /F1 12 Tf 10 10 Td (" + s + ") Tj ET" }; }

  /* three pages; links from page 1 to page 2 and from page 3 to page 1 */
  var DOC_A = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R 6 0 R] /Count 3>>",
    "<<\/Type /Pages /Parent 2 0 R /Kids [4 0 R 5 0 R] /Count 2 /MediaBox [0 0 200 100] /Resources <<\/Font <<\/F1 9 0 R>>>>>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 7 0 R /Annots [<<\/Type /Annot /Subtype /Link /Rect [0 0 50 50] /Dest [5 0 R /Fit]>>]>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 8 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources <<\/Font <<\/F1 9 0 R>>>> /Contents 10 0 R /Annots [<<\/Type /Annot /Subtype /Link /Rect [0 0 9 9] /Dest [4 0 R /Fit]>>]>>",
    text("A1"), text("A2"),
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    text("A3")
  ]);
  /* forty pages, one stream each */
  var DOC_40 = (function () {
    var bodies = ["<<\/Type /Catalog /Pages 2 0 R>>", ""];
    var kids = [];
    for (var i = 0; i < 40; i++) {
      kids.push((3 + i * 2) + " 0 R");
      bodies.push("<<\/Type /Page /Parent 2 0 R /Contents " + (4 + i * 2) + " 0 R>>");
      bodies.push(text("P" + (i + 1)));
    }
    bodies[1] = "<<\/Type /Pages /Kids [" + kids.join(" ") + "] /Count 40 /MediaBox [0 0 100 100]>>";
    return makePdf(bodies);
  })();
  var DOC_ENC = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R");
  var F = {
    a: new File([DOC_A], "a-report.pdf", { type: "application/pdf" }),
    forty: new File([DOC_40], "forty.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("plain words")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC_A], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* a reader for the PDFs that come out, sharing nothing with the page */
  function readOut(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    var head = /^xref\n0 (\d+)\n/.exec(t.slice(at));
    if (!head) { throw new Error("startxref does not point at the table"); }
    var count = Number(head[1]), first = at + head[0].length, objs = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(t.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]), tag = n + " 0 obj\n";
      if (t.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      var start = off + tag.length, si = t.indexOf("\nstream\n", start), ei = t.indexOf("\nendobj\n", start);
      var o = { body: t.slice(start, ei), data: null };
      if (si > -1 && si < ei) {
        o.body = t.slice(start, si);
        var len = Number(/\/Length (\d+)/.exec(o.body)[1]);
        o.data = t.substr(si + 8, len);
        if (t.substr(si + 8 + len, 18) !== "\nendstream\nendobj\n") { throw new Error("object " + n + " has the wrong /Length"); }
      }
      objs[n] = o;
    }
    var trailer = t.slice(first + count * 20);
    var root = objs[Number(/\/Root (\d+) 0 R/.exec(trailer)[1])];
    var pagesNum = Number(/\/Pages (\d+) 0 R/.exec(root.body)[1]);
    var kids = /\/Kids \[([^\]]*)\]/.exec(objs[pagesNum].body)[1].match(/\d+ 0 R/g).map(function (k) { return parseInt(k, 10); });
    var pages = kids.map(function (k) {
      var body = objs[k].body;
      var c = /\/Contents (\d+) 0 R/.exec(body);
      var drawn = c ? (/\(([^)]*)\) Tj/.exec(objs[Number(c[1])].data || "") || [0, "?"])[1] : "?";
      return { num: k, body: body, drawn: drawn,
               dests: (body.match(/\/Dest \[(\d+) 0 R/g) || []).map(function (d) { return parseInt(d.slice(7), 10); }) };
    });
    return { objs: objs, pages: pages, text: t, drawn: pages.map(function (p) { return p.drawn; }).join(",") };
  }
  /* a ZIP reader: walk the local headers of a stored (uncompressed) ZIP */
  function readZip(bytes) {
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), pos = 0, files = [];
    var decoder = new TextDecoder();
    while (pos + 30 <= bytes.length && view.getUint32(pos, true) === 0x04034B50) {
      var method = view.getUint16(pos + 8, true), size = view.getUint32(pos + 18, true);
      var nameLen = view.getUint16(pos + 26, true), extra = view.getUint16(pos + 28, true);
      var name = decoder.decode(bytes.subarray(pos + 30, pos + 30 + nameLen));
      var start = pos + 30 + nameLen + extra;
      if (method !== 0) { throw new Error("an entry is compressed"); }
      files.push({ name: name, bytes: bytes.subarray(start, start + size) });
      pos = start + size;
    }
    if (view.getUint32(pos, true) !== 0x02014B50) { throw new Error("the central directory is not where it should be"); }
    return files;
  }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    return wait("the files are made", function () { return window.__saved.length === before + 1; }, 30000).then(function () {
      var saved = window.__saved[window.__saved.length - 1];
      if (!saved) { return null; }
      return saved.blob.arrayBuffer().then(function (b) {
        var bytes = new Uint8Array(b), result = { saved: saved, files: [], error: "" };
        try {
          var list = /\.zip$/.test(saved.name) ? readZip(bytes) : [{ name: saved.name, bytes: bytes }];
          result.files = list.map(function (f) { return { name: f.name, pdf: readOut(f.bytes) }; });
        } catch (e) { result.error = e.message; }
        return result;
      });
    });
  }
  function plannedText() { return Array.prototype.map.call(planned.children, function (li) { return li.textContent; }).join(" | "); }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF to split.");
  ok("the button starts disabled", document.getElementById("dlBtn").disabled);
  eq("the tiles start blank", txt("sFiles") + txt("sPages") + txt("sIn"), DASH + DASH + DASH);
  eq("ranges is the starting choice", val("mode"), "ranges");
  eq("so its box shows and the part length does not",
     (getComputedStyle(document.getElementById("rangesField")).display !== "none") + "," + getComputedStyle(document.getElementById("everyField")).display, "true,none");
  eq("one file at a time", document.getElementById("file").multiple, false);

  /* ================= ranges ================= */
  step(function () {
    feed(F.a);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    eq("the ranges box is filled in with a sensible first guess", val("ranges"), "1-2, 3-");
    eq("which plans two files", plannedText(), "a-report-pages-1-2.pdf " + DASH + " pages 1 to 2 | a-report-page-3.pdf " + DASH + " page 3");
    eq("the tiles count them", txt("sFiles") + "|" + txt("sPages") + "|" + txt("sIn"), "2|3|" + formatBytes(F.a.size));
    eq("and the message says so", txt("msg"), "a-report.pdf: 3 pages. 2 PDFs will be made, in one ZIP file.");
    return download();
  });
  step(function (d) {
    ok("the ZIP and both PDFs in it read back cleanly", d && !d.error && d.files.length === 2, d ? d.error : "nothing saved");
    if (!d || d.error) { return; }
    eq("the ZIP is named after the PDF", d.saved.name, "a-report-split.zip");
    eq("the files in it, in order", d.files.map(function (f) { return f.name; }).join("|"), "a-report-pages-1-2.pdf|a-report-page-3.pdf");
    eq("the first has pages 1 and 2", d.files[0].pdf.drawn, "A1,A2");
    eq("the second has page 3", d.files[1].pdf.drawn, "A3");
    eq("a link between two pages in the same file still works", d.files[0].pdf.pages[0].dests.join(), String(d.files[0].pdf.pages[1].num));
    eq("a link to a page now in another file goes nowhere", d.files[1].pdf.pages[0].dests.length + "," + /\/Dest \[null/.test(d.files[1].pdf.pages[0].body), "0,true");
    eq("and it did not drag that page along", (d.files[1].pdf.text.match(/\/Type \/Page\b/g) || []).length, 1);
    has("the done message counts what was made", txt("msg"), "Done: 2 PDFs in one ZIP file");
    set("ranges", "2");
    eq("one range plans one file", plannedText(), "a-report-page-2.pdf " + DASH + " page 2");
    has("and says so", txt("msg"), "One PDF will be made.");
    return download();
  });
  step(function (d) {
    eq("one file comes as a PDF, not a ZIP", d.saved.name + "|" + d.saved.blob.type, "a-report-page-2.pdf|application/pdf");
    eq("with that page", d.files[0].pdf.drawn, "A2");
    set("ranges", "1, 1");
    eq("the same range twice gets two names", plannedText(), "a-report-page-1.pdf " + DASH + " page 1 | a-report-page-1 (2).pdf " + DASH + " page 1");
    set("ranges", "1-2, 2-3");
    eq("ranges may overlap", txt("sFiles") + "|" + txt("sPages"), "2|4");
    set("ranges", "-2");
    eq("an open start runs from page 1", plannedText(), "a-report-pages-1-2.pdf " + DASH + " pages 1 to 2");
    set("ranges", "2 - 3");
    eq("spaces around a dash are fine", plannedText(), "a-report-pages-2-3.pdf " + DASH + " pages 2 to 3");
    set("ranges", "2-");
    eq("an open end runs to the last page", plannedText(), "a-report-pages-2-3.pdf " + DASH + " pages 2 to 3");
    set("ranges", "1" + String.fromCharCode(0x2013) + "2");
    eq("an en dash works like a hyphen", plannedText(), "a-report-pages-1-2.pdf " + DASH + " pages 1 to 2");
    set("ranges", "2-9");
    eq("a range that runs past the end is named", txt("msg"), String.fromCharCode(0x201C) + "2-9" + String.fromCharCode(0x201D) + ": this PDF has only 3 pages.");
    set("ranges", "5");
    eq("a page that does not exist is named", txt("msg"), String.fromCharCode(0x201C) + "5" + String.fromCharCode(0x201D) + ": this PDF has only 3 pages.");
    ok("and nothing can be made", document.getElementById("dlBtn").disabled && planned.children.length === 0);
    eq("and the tiles say so", txt("sFiles") + txt("sPages"), DASH + DASH);
    set("ranges", "3-1");
    has("a range written backwards", txt("msg"), ": write the smaller number first, as in 1-3.");
    set("ranges", "two");
    has("a word", txt("msg"), "is not a page number or a range like 4-7.");
    set("ranges", "  ");
    eq("nothing typed", txt("msg"), "Type at least one page number.");
    set("ranges", "0");
    has("page 0", txt("msg"), "pages are numbered from 1.");
  });

  /* ================= every page, and parts of the same length ================= */
  step(function () {
    set("mode", "each");
    eq("the ranges box hides", getComputedStyle(document.getElementById("rangesField")).display, "none");
    eq("every page makes three files", txt("sFiles"), "3");
    return download();
  });
  step(function (d) {
    eq("one per page", d.files.map(function (f) { return f.name + ":" + f.pdf.drawn; }).join("|"), "a-report-page-1.pdf:A1|a-report-page-2.pdf:A2|a-report-page-3.pdf:A3");
    eq("and none links into another", d.files.every(function (f) { return f.pdf.pages[0].dests.length === 0; }), true);
    set("mode", "every");
    eq("the part length shows", getComputedStyle(document.getElementById("everyField")).display !== "none", true);
    set("every", "2");
    eq("parts of two: two files, the last shorter", plannedText(), "a-report-pages-1-2.pdf " + DASH + " pages 1 to 2 | a-report-page-3.pdf " + DASH + " page 3");
    set("every", "5");
    eq("a part longer than the PDF is the whole PDF", plannedText(), "a-report-pages-1-3.pdf " + DASH + " pages 1 to 3");
    set("every", "0");
    eq("zero is refused", txt("msg"), "Type how many pages each file should have, from 1 up.");
    set("every", "1.5");
    eq("so is a fraction", txt("msg"), "Type how many pages each file should have, from 1 up.");
    set("every", "1");
    eq("one makes a file per page", txt("sFiles"), "3");
  });

  /* ================= a long plan, a damaged file, files that cannot be used ================= */
  step(function () {
    click("resetBtn");
    eq("clear asks again", txt("msg"), "Choose a PDF to split.");
    eq("and empties the ranges box", val("ranges"), "");
    set("mode", "each");
    feed(F.forty);
    return wait("forty pages are read", read, 10000);
  });
  step(function () {
    eq("thirty files are named, then the rest are counted", planned.children.length + "|" + planned.lastChild.textContent, "31|and 10 more");
    return download();
  });
  step(function (d) {
    eq("forty files in the ZIP", d.files.length, 40);
    eq("each with its own page", d.files.map(function (f) { return f.pdf.drawn; }).join(","),
       Array.from({ length: 40 }, function (_, i) { return "P" + (i + 1); }).join(","));
    set("mode", "ranges");
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    has("a damaged file is read anyway, and the visitor told", txt("msg"), "scan.pdf: 1 page (damaged, but its pages could be found).");
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("it is turned away by name", txt("msg"), "Could not use notes.pdf: not a PDF.");
    ok("with nothing to make", document.getElementById("dlBtn").disabled);
    eq("and blank tiles", txt("sFiles") + txt("sIn"), DASH + DASH);
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one too", txt("msg"), "Could not use locked.pdf: protected with a password.");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () {
    has("its name is shown as text", plannedText(), "<iframe onload=zq>-page-1.pdf");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + planned.children.length, "Choose a PDF to split.|0");
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for split-pdf ----

# ---- START: the test for remove-pdf-pages ----
T["remove-pdf-pages"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  /* a picture whose bytes spell a word, so its presence in a file can be searched for */
  function picture(word) {
    var data = "";
    while (data.length < 192) { data += word; }
    data = data.slice(0, 192);
    return { dict: "<<\/Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length LEN>>", data: data };
  }
  function drawing(text, picName) {
    return { dict: "<<\/Length LEN>>", data: (picName ? "q 50 0 0 50 0 0 cm /" + picName + " Do Q " : "") + "BT /F1 12 Tf 10 10 Td (" + text + ") Tj ET" };
  }

  /* Five pages that all share ONE list of resources holding five pictures,
     one drawn on each page: the layout where a careless copy keeps every
     picture in every extract. Page 3's picture spells SECRETID. */
  var DOC_SHARED = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R 4 0 R 5 0 R 6 0 R 7 0 R] /Count 5 /MediaBox [0 0 100 100] /Resources 8 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 9 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 10 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 11 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 12 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 13 0 R>>",
    "<<\/Font <<\/F1 19 0 R>> /XObject <<\/Im1 14 0 R /Im2 15 0 R /Im3 16 0 R /Im4 17 0 R /Im5 18 0 R>>>>",
    /* page 1 also has a tiny picture written inline, whose eight bytes happen
       to read "/Im3 Do ": they are picture data, not an instruction, and must
       not make page 3's picture look used */
    { dict: "<<\/Length LEN>>", data: "q 50 0 0 50 0 0 cm /Im1 Do Q BI /W 8 /H 1 /CS /G /BPC 8 ID /Im3 Do EI BT /F1 12 Tf 10 10 Td (P1) Tj ET" },
    drawing("P2", "Im2"), drawing("P3", "Im3"), drawing("P4", "Im4"), drawing("P5", "Im5"),
    picture("PICONE.."), picture("PICTWO.."), picture("SECRETID"), picture("PICFOUR."), picture("PICFIVE."),
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Helvetica>>"
  ]);
  var DOC_ENC = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R");
  var F = {
    shared: new File([DOC_SHARED], "statement.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf at all")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC_SHARED], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* a reader for the PDF that comes out, sharing nothing with the page */
  function readOut(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    var head = /^xref\n0 (\d+)\n/.exec(t.slice(at));
    if (!head) { throw new Error("startxref does not point at the table"); }
    var count = Number(head[1]), first = at + head[0].length, objs = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(t.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]), tag = n + " 0 obj\n";
      if (t.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      var start = off + tag.length, si = t.indexOf("\nstream\n", start), ei = t.indexOf("\nendobj\n", start);
      var o = { body: t.slice(start, ei), data: null };
      if (si > -1 && si < ei) {
        o.body = t.slice(start, si);
        var len = Number(/\/Length (\d+)/.exec(o.body)[1]);
        o.data = t.substr(si + 8, len);
        if (t.substr(si + 8 + len, 18) !== "\nendstream\nendobj\n") { throw new Error("object " + n + " has the wrong /Length"); }
      }
      objs[n] = o;
    }
    var root = objs[Number(/\/Root (\d+) 0 R/.exec(t.slice(first + count * 20))[1])];
    var pagesNum = Number(/\/Pages (\d+) 0 R/.exec(root.body)[1]);
    var kids = /\/Kids \[([^\]]*)\]/.exec(objs[pagesNum].body)[1].match(/\d+ 0 R/g).map(function (k) { return parseInt(k, 10); });
    var drawn = kids.map(function (k) {
      var c = /\/Contents (\d+) 0 R/.exec(objs[k].body);
      return c ? (/\(([^)]*)\) Tj/.exec(objs[Number(c[1])].data || "") || [0, "?"])[1] : "?";
    });
    return { text: t, drawn: drawn.join(","), pages: kids.length };
  }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return saved.blob.arrayBuffer().then(function (b) {
      var bytes = new Uint8Array(b), out = null, error = "";
      try { out = readOut(bytes); } catch (e) { error = e.message; }
      return { saved: saved, bytes: bytes, out: out, error: error };
    });
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF.");
  ok("the button starts disabled", document.getElementById("dlBtn").disabled);
  eq("the tiles start blank", txt("sNow") + txt("sGone") + txt("sLeft"), DASH + DASH + DASH);

  /* ================= taking pages out ================= */
  step(function () {
    feed(F.shared);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    eq("it says how many pages there are, and asks which to take out", txt("msg"), "statement.pdf: 5 pages. Type the pages to take out.");
    eq("the tiles show the count", txt("sNow") + "|" + txt("sGone") + "|" + txt("sLeft"), "5|" + DASH + "|" + DASH);
    ok("nothing can be made before pages are typed", document.getElementById("dlBtn").disabled);
    set("pages", "3");
    eq("one page out", txt("sGone") + "|" + txt("sLeft"), "1|4");
    eq("the pages that stay are listed short", txt("kept"), "Pages kept: 1-2, 4-5");
    eq("and the message says so", txt("msg"), "statement.pdf: 5 pages. 1 page comes out, 4 stay.");
    return download();
  });
  step(function (d) {
    ok("the new PDF reads back cleanly", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    eq("named for what was done to it", d.saved.name, "statement (pages removed).pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("the four pages that stay, in order", d.out.drawn, "P1,P2,P4,P5");
    eq("the removed page's picture is not in the file at all, though every page shared one list of pictures",
       d.out.text.indexOf("SECRETID"), -1);
    ok("the pictures of the pages that stay are", ["PICONE..", "PICTWO..", "PICFOUR.", "PICFIVE."].every(function (w) { return d.out.text.indexOf(w) > -1; }));
    eq("four pictures in the file, one for each page that stays", (d.out.text.match(/\/Subtype \/Image/g) || []).length, 4);
    has("and the font the pages write with is still there", d.out.text, "/BaseFont /Helvetica");
    has("the done message counts the pages", txt("msg"), "Done: 4 pages");
    set("pages", "2, 4-");
    eq("ranges and an open end", txt("kept") + "|" + txt("sGone"), "Pages kept: 1, 3|3");
    return download();
  });
  step(function (d) {
    eq("the right pages stay", d.out.drawn, "P1,P3");
    ok("and only their pictures come along", d.out.text.indexOf("SECRETID") > -1 && d.out.text.indexOf("PICTWO..") === -1 && d.out.text.indexOf("PICFIVE.") === -1);
    set("pages", "1, 1, 1-2");
    eq("a page named twice comes out once", txt("sGone") + "|" + txt("kept"), "2|Pages kept: 3-5");
    set("pages", "-4");
    eq("an open start", txt("kept"), "Pages kept: 5");
    set("pages", "1-5");
    eq("taking out every page is refused", txt("msg"), "That takes out every page. A PDF needs at least one, so leave one or more in.");
    ok("with nothing to download", document.getElementById("dlBtn").disabled && txt("kept") === "");
    set("pages", "6");
    eq("a page that is not there is named", txt("msg"), String.fromCharCode(0x201C) + "6" + String.fromCharCode(0x201D) + ": this PDF has only 5 pages.");
    set("pages", "4-2");
    has("a range written backwards", txt("msg"), "write the smaller number first, as in 2-4.");
    set("pages", "");
    eq("an empty box asks again", txt("msg"), "statement.pdf: 5 pages. Type the pages to take out.");
    ok("with nothing to download", document.getElementById("dlBtn").disabled);
  });

  /* ================= a page whose drawing is damaged ================= */
  /* When a page that stays cannot be read to the end, the page cannot know
     which pictures it uses, so it must keep them all rather than guess. */
  step(function () {
    var drawingText = "q 50 0 0 50 0 0 cm /ImA Do Q ";
    for (var i = 0; i < 300; i++) { drawingText += "% filler line " + i + "\n"; }
    drawingText += "q 50 0 0 50 50 50 cm /ImB Do Q";
    var packed = new Blob([bytesOfText(drawingText)]).stream().pipeThrough(new CompressionStream("deflate"));
    return new Response(packed).arrayBuffer().then(function (buffer) {
      var whole = new Uint8Array(buffer);
      var cut = latin1(whole.subarray(0, whole.length - 40));
      var doc = makePdf([
        "<<\/Type /Catalog /Pages 2 0 R>>",
        "<<\/Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] /Resources <<\/XObject <<\/ImA 6 0 R /ImB 7 0 R /ImC 8 0 R>>>>>>",
        "<<\/Type /Page /Parent 2 0 R /Contents 5 0 R>>",
        "<<\/Type /Page /Parent 2 0 R /Contents 9 0 R>>",
        { dict: "<<\/Filter /FlateDecode /Length LEN>>", data: cut },
        picture("PICTUREA"), picture("PICTUREB"), picture("PICTUREC"),
        drawing("Q2", "ImC")
      ]);
      feed(new File([doc], "torn.pdf", { type: "application/pdf" }));
      return wait("a PDF with a torn drawing is read", read, 10000);
    });
  });
  step(function () {
    set("pages", "2");
    return download();
  });
  step(function (d) {
    ok("the page with the torn drawing keeps every picture it might use",
       d && d.out && ["PICTUREA", "PICTUREB", "PICTUREC"].every(function (w) { return d.out.text.indexOf(w) > -1; }), d ? d.error : "no download");
    set("pages", "");
  });

  /* ================= other files ================= */
  step(function () {
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    eq("a damaged file is read anyway, and the visitor told", txt("msg"), "scan.pdf: 1 page (damaged, but its pages could be found). Type the pages to take out.");
    set("pages", "1");
    has("its only page can not be taken out", txt("msg"), "takes out every page");
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("it is turned away by name", txt("msg"), "Could not use notes.pdf: not a PDF.");
    eq("with blank tiles", txt("sNow") + txt("sGone") + txt("sLeft"), DASH + DASH + DASH);
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one too", txt("msg"), "Could not use locked.pdf: protected with a password.");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () {
    has("its name is shown as text", txt("msg"), "<iframe onload=zq>.pdf: 5 pages.");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    return download();
  });
  step(function (d) {
    eq("its new name keeps the characters as they were", d.saved.name, "<iframe onload=zq> (pages removed).pdf");
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + val("pages") + "|" + txt("sNow"), "Choose a PDF.||" + DASH);
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for remove-pdf-pages ----

# ---- START: the test for rotate-pdf ----
T["rotate-pdf"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  function text(s) { return { dict: "<<\/Length LEN>>", data: "BT /F1 12 Tf 10 10 Td (" + s + ") Tj ET" }; }

  /* Four pages: 1 and 2 inherit a quarter turn from their node, 3 has three
     quarters of its own, 4 has none. A bookmark and a title, which a rotation
     must leave alone. */
  var DOC = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R /Outlines 12 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R 6 0 R 7 0 R] /Count 4 /MediaBox [0 0 200 100]>>",
    "<<\/Type /Pages /Parent 2 0 R /Kids [4 0 R 5 0 R] /Count 2 /Rotate 90>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 8 0 R>>",
    "<<\/Type /Page /Parent 3 0 R /Contents 9 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Rotate 270 /Contents 10 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 11 0 R>>",
    text("R1"), text("R2"), text("R3"), text("R4"),
    "<<\/Type /Outlines /First 13 0 R /Last 13 0 R /Count 1>>",
    "<<\/Title (Chapter one) /Parent 12 0 R /Dest [4 0 R /Fit]>>",
    "<<\/Title (Quarterly report) /Producer (a test)>>"
  ], " /Info 14 0 R");
  var ONE = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>", "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 50 50]>>"]);
  var DOC_ENC = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R");
  var F = {
    doc: new File([DOC], "report.pdf", { type: "application/pdf" }),
    one: new File([ONE], "single.pdf", { type: "application/pdf" }),
    packed: new File([fromB64(TINY.objstm)], "packed.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* A reader for the result, sharing nothing with the page. It follows the
     chain of classic tables from the newest back (/Prev), the newest entry
     for each object winning, and checks every position it uses. */
  function readChain(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var where = {}, trailers = [], at = Number(sx[1]), guard = 0;
    while (at >= 0 && guard++ < 10) {
      if (t.substr(at, 5) !== "xref\n") { throw new Error("no table at " + at); }
      var pos = at + 5, m;
      var sub = /^(\d+) (\d+)\n/;
      while ((m = sub.exec(t.slice(pos, pos + 30)))) {
        pos += m[0].length;
        for (var i = 0; i < Number(m[2]); i++) {
          var e = /^(\d{10}) (\d{5}) ([nf]) \n$/.exec(t.substr(pos, 20));
          if (!e) { throw new Error("a malformed entry at " + pos); }
          var num = Number(m[1]) + i;
          if (!(num in where) && e[3] === "n") { where[num] = Number(e[1]); }
          pos += 20;
        }
      }
      if (t.substr(pos, 8) !== "trailer\n") { throw new Error("no trailer after the table at " + at); }
      var trailer = t.slice(pos + 8, t.indexOf("\nstartxref", pos));
      trailers.push(trailer);
      var prev = /\/Prev (\d+)/.exec(trailer);
      at = prev ? Number(prev[1]) : -1;
    }
    function obj(n) {
      var off = where[n], tag = new RegExp("^" + n + " \\d+ obj\n");
      if (off === undefined || !tag.test(t.slice(off, off + 20))) { throw new Error("object " + n + " is not where the table says"); }
      return t.slice(off, t.indexOf("\nendobj\n", off));
    }
    return { text: t, trailers: trailers, obj: obj, sections: trailers.length };
  }
  function rotations(r, pageNums) { return pageNums.map(function (n) { return (/\/Rotate (\d+)/.exec(r.obj(n)) || [0, "-"])[1]; }).join(","); }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return saved.blob.arrayBuffer().then(function (b) { return { saved: saved, bytes: new Uint8Array(b) }; });
  }
  function startsWith(bytes, prefix) {
    if (bytes.length < prefix.length) { return false; }
    for (var i = 0; i < prefix.length; i++) { if (bytes[i] !== prefix[i]) { return false; } }
    return true;
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF.");
  ok("the button starts disabled", document.getElementById("dlBtn").disabled);
  eq("every page and a quarter clockwise are the starting choices", val("which") + "|" + val("turn"), "all|90");
  eq("the page box hides until it is wanted", getComputedStyle(document.getElementById("pagesField")).display, "none");

  /* ================= every page, a quarter clockwise ================= */
  step(function () {
    feed(F.doc);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    eq("the tiles count the pages", txt("sPages") + "|" + txt("sTurned") + "|" + txt("sIn"), "4|4|" + formatBytes(F.doc.size));
    eq("and the message says what will happen, and what will not", txt("msg"),
       "Every page will be turned a quarter turn clockwise. Everything else in the file, bookmarks and forms included, stays as it is.");
    return download();
  });
  step(function (d) {
    eq("named for what was done to it", d.saved.name, "report (rotated).pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    ok("the original file is still there, byte for byte, at the start", startsWith(d.bytes, DOC));
    var r;
    try { r = readChain(d.bytes); } catch (e) { ok("the turned file reads back cleanly", false, e.message); return; }
    eq("one section added to the original", r.sections, 2);
    eq("each page's turn adds to the one it had: 90+90, 90+90, 270+90, 0+90", rotations(r, [4, 5, 6, 7]), "180,180,0,90");
    has("the new section points back at the old one", r.trailers[0], "/Prev " + /startxref\n(\d+)\n%%EOF\n$/.exec(latin1(DOC))[1]);
    has("and keeps the same catalog", r.trailers[0], "/Root 1 0 R");
    has("and the same document details", r.trailers[0], "/Info 14 0 R");
    eq("the bookmark is untouched", r.obj(13).indexOf("(Chapter one)") > -1 && r.obj(1).indexOf("/Outlines 12 0 R") > -1, true);
    eq("only the pages were written again", (latin1(d.bytes.subarray(DOC.length)).match(/ 0 obj\n/g) || []).length, 4);
    has("the done message counts the pages", txt("msg"), "Done: 4 pages turned");
  });

  /* ================= odd pages upside down, typed pages anticlockwise ================= */
  step(function () {
    set("which", "odd");
    set("turn", "180");
    eq("odd pages", txt("sTurned"), "2");
    eq("named in the message", txt("msg").split(".")[0], "Pages 1, 3 will be turned upside down");
    return download();
  });
  step(function (d) {
    var r = readChain(d.bytes);
    eq("pages 1 and 3 turn half way; 2 and 4 are not written again", rotations(r, [4, 6]) + "|" + latin1(d.bytes.subarray(DOC.length)).match(/^\d+ 0 obj$/gm).join(","),
       "270,90|4 0 obj,6 0 obj");
    set("which", "even");
    eq("even pages", txt("sTurned") + "|" + txt("msg").split(".")[0], "2|Pages 2, 4 will be turned upside down");
    set("which", "pages");
    eq("the page box appears", getComputedStyle(document.getElementById("pagesField")).display !== "none", true);
    eq("and asks", txt("msg"), "Type the pages to turn.");
    ok("with nothing to download yet", document.getElementById("dlBtn").disabled);
    set("pages", "2-3");
    set("turn", "270");
    eq("typed pages", txt("msg").split(".")[0], "Pages 2-3 will be turned a quarter turn anticlockwise");
    return download();
  });
  step(function (d) {
    var r = readChain(d.bytes);
    eq("90-90 is upright and 270-90 is upside down", rotations(r, [5, 6]), "0,180");
    set("pages", "4");
    eq("one page is named as one", txt("msg").split(".")[0], "Page 4 will be turned a quarter turn anticlockwise");
    set("pages", "9");
    eq("a page that is not there is named", txt("msg"), String.fromCharCode(0x201C) + "9" + String.fromCharCode(0x201D) + ": this PDF has only 4 pages.");
    ok("and nothing can be made", document.getElementById("dlBtn").disabled);
  });

  /* ================= other kinds of file ================= */
  step(function () {
    set("which", "even");
    feed(F.one);
    return wait("a one-page PDF is read", read, 10000);
  });
  step(function () {
    eq("a one-page PDF has no even pages", txt("msg"), "This PDF has only one page, so it has no even pages to turn.");
    ok("so nothing can be made", document.getElementById("dlBtn").disabled);
    set("which", "all");
    set("turn", "90");
    feed(F.packed);
    return wait("a PDF with a packed index is read", read, 10000);
  });
  step(function () { return download(); });
  step(function (d) {
    var original = fromB64(TINY.objstm);
    ok("a file whose index is a stream keeps its bytes too", startsWith(d.bytes, original));
    var tail = latin1(d.bytes.subarray(original.length));
    has("and gets its addition as a stream as well", tail, "/Type /XRef");
    has("pointing back at the old one", tail, "/Prev " + /startxref\n(\d+)\n%%EOF\n$/.exec(latin1(original))[1]);
    has("with the page's new turn", tail, "/Rotate 90");
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    has("a damaged file is rebuilt, and the visitor told what that costs", txt("msg"),
        "This file's index is damaged, so it will be rebuilt: its pages are kept, but bookmarks and forms are not.");
    return download();
  });
  step(function (d) {
    var t = latin1(d.bytes);
    eq("the rebuilt file has one index of its own", (t.match(/startxref/g) || []).length + "|" + /\/Prev/.test(t), "1|false");
    has("and the page is turned", t, "/Rotate 90");
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("it is turned away by name", txt("msg"), "Could not use notes.pdf: not a PDF.");
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one too", txt("msg"), "Could not use locked.pdf: protected with a password.");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () { return download(); });
  step(function (d) {
    eq("its name stays text", d.saved.name, "<iframe onload=zq> (rotated).pdf");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + val("pages") + "|" + txt("sPages"), "Choose a PDF.||" + DASH);
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for rotate-pdf ----

# ---- START: the test for add-pdf-page-numbers ----
T["add-pdf-page-numbers"] = r"""
  var DASH = String.fromCharCode(0x2014);
  var LQ = String.fromCharCode(0x201C), RQ = String.fromCharCode(0x201D);
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  function text(s) { return { dict: "<<\/Length LEN>>", data: "BT /F1 12 Tf 10 10 Td (" + s + ") Tj ET" }; }

  /* Four pages of 200 x 100 points. Page 2 is shown turned a quarter
     clockwise; page 3 has a crop box; page 4's own fonts already use the name
     the page would choose for its numbers. Resources are shared and
     inherited, and there is a bookmark. */
  var DOC = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R /Outlines 12 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R 4 0 R 5 0 R 6 0 R] /Count 4 /MediaBox [0 0 200 100] /Resources 11 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 7 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /Rotate 90 /Contents 8 0 R>>",
    "<<\/Type /Page /Parent 2 0 R /CropBox [20 10 180 90] /Contents [9 0 R]>>",
    "<<\/Type /Page /Parent 2 0 R /Resources <<\/Font <<\/F108tb 13 0 R>>>> /Contents 10 0 R>>",
    text("N1"), text("N2"), text("N3"), text("N4"),
    "<<\/Font <<\/F1 13 0 R>>>>",
    "<<\/Type /Outlines /Count 0>>",
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Courier>>"
  ]);
  var DOC_ENC = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R");
  var ONE = makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>", "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 50 50]>>"]);
  var F = {
    doc: new File([DOC], "thesis.pdf", { type: "application/pdf" }),
    one: new File([ONE], "single.pdf", { type: "application/pdf" }),
    packed: new File([fromB64(TINY.objstm)], "packed.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf")], "notes.pdf", { type: "application/pdf" })
  };

  /* Helvetica's published widths for the characters the numbers use, kept
     here separately from the page's own table */
  var W = { "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556, "8": 556, "9": 556,
            " ": 278, "/": 278, "P": 667, "a": 556, "g": 556, "e": 556, "o": 556, "f": 278 };
  function width(s, size) { var u = 0; for (var i = 0; i < s.length; i++) { u += W[s.charAt(i)]; } return u / 1000 * size; }
  var MARGIN = 10 * 72 / 25.4;

  /* the reader from the rotate test: classic tables, newest first */
  function readChain(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var where = {}, trailers = [], at = Number(sx[1]), guard = 0;
    while (at >= 0 && guard++ < 10) {
      if (t.substr(at, 5) !== "xref\n") { throw new Error("no table at " + at); }
      var pos = at + 5, m, sub = /^(\d+) (\d+)\n/;
      while ((m = sub.exec(t.slice(pos, pos + 30)))) {
        pos += m[0].length;
        for (var i = 0; i < Number(m[2]); i++) {
          var e = /^(\d{10}) (\d{5}) ([nf]) \n$/.exec(t.substr(pos, 20));
          if (!e) { throw new Error("a malformed entry at " + pos); }
          var num = Number(m[1]) + i;
          if (!(num in where) && e[3] === "n") { where[num] = Number(e[1]); }
          pos += 20;
        }
      }
      if (t.substr(pos, 8) !== "trailer\n") { throw new Error("no trailer after the table at " + at); }
      var trailer = t.slice(pos + 8, t.indexOf("\nstartxref", pos));
      trailers.push(trailer);
      var prev = /\/Prev (\d+)/.exec(trailer);
      at = prev ? Number(prev[1]) : -1;
    }
    function obj(n) {
      var off = where[n], tag = new RegExp("^" + n + " \\d+ obj\n");
      if (off === undefined || !tag.test(t.slice(off, off + 20))) { throw new Error("object " + n + " is not where the table says"); }
      var end = t.indexOf("\nendobj\n", off);
      var body = t.slice(off, end);
      var si = body.indexOf("\nstream\n");
      return si < 0 ? { body: body, data: null } : { body: body.slice(0, si), data: body.slice(si + 8, body.lastIndexOf("\nendstream")) };
    }
    return { text: t, trailers: trailers, obj: obj, sections: trailers.length };
  }
  /* the drawing added to a page, and the numbers in it */
  function added(r, pageNum) {
    var body = r.obj(pageNum).body;
    var refs = /\/Contents \[([^\]]*)\]/.exec(body)[1].match(/\d+ 0 R/g).map(function (x) { return parseInt(x, 10); });
    var first = r.obj(refs[0]).data, last = r.obj(refs[refs.length - 1]).data;
    var m = /^Q\nq (\S+) (\S+) (\S+) (\S+) (\S+) (\S+) cm BT \/(\S+) (\S+) Tf 0 g (\S+) (\S+) Td \((.*)\) Tj ET Q\n$/.exec(last);
    return { body: body, refs: refs, first: first, last: last, m: m,
             matrix: m ? m.slice(1, 7).map(Number) : null, font: m ? m[7] : "", size: m ? Number(m[8]) : 0,
             u: m ? Number(m[9]) : 0, v: m ? Number(m[10]) : 0, text: m ? m[11] : "" };
  }
  function near(a, b) { return Math.abs(a - b) < 0.0011; }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return saved.blob.arrayBuffer().then(function (b) { return { saved: saved, bytes: new Uint8Array(b) }; });
  }
  function startsWith(bytes, prefix) {
    if (bytes.length < prefix.length) { return false; }
    for (var i = 0; i < prefix.length; i++) { if (bytes[i] !== prefix[i]) { return false; } }
    return true;
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF.");
  ok("the button starts disabled", document.getElementById("dlBtn").disabled);
  eq("the usual choices to start with", val("position") + "|" + val("format") + "|" + val("start") + "|" + val("size") + "|" +
     document.getElementById("skipFirst").checked, "bottom-center|plain|1|11|false");

  /* ================= plain numbers, bottom centre ================= */
  step(function () {
    feed(F.doc);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    eq("the tiles count the pages", txt("sPages") + "|" + txt("sNumbered") + "|" + txt("sIn"), "4|4|" + formatBytes(F.doc.size));
    eq("the example shows the first and last numbers", txt("example"), "Page 1 will read " + LQ + "1" + RQ + ", the last page " + LQ + "4" + RQ + ".");
    eq("the message says nothing else changes", txt("msg"), "thesis.pdf: 4 pages. Everything else in the file stays as it is.");
    return download();
  });
  step(function (d) {
    eq("named for what was done to it", d.saved.name, "thesis (numbered).pdf");
    ok("the original is still there, byte for byte, at the start", startsWith(d.bytes, DOC));
    var r;
    try { r = readChain(d.bytes); } catch (e) { ok("the numbered file reads back cleanly", false, e.message); return; }
    var p1 = added(r, 3);
    ok("page 1's drawing is wrapped: q first, the old drawing, then Q and the number", p1.m && p1.first === "q\n" && p1.refs.length === 3 && p1.refs[1] === 7, p1.last);
    eq("page 1 reads 1", p1.text, "1");
    eq("in the size asked for", p1.size, 11);
    ok("centred along the bottom, 10 mm up", near(p1.u, (200 - width("1", 11)) / 2) && near(p1.v, MARGIN), p1.u + " " + p1.v);
    ok("on an upright page the drawing is not turned", p1.matrix.join(" ") === "1 0 0 1 0 0", p1.matrix.join(" "));
    has("the page names Helvetica as its number font", p1.body, "/F108tb ");
    has("next to its own font", p1.body, "/F1 13 0 R");
    var font = /\/F108tb (\d+) 0 R/.exec(p1.body);
    has("and that font is Helvetica, which every reader has built in", r.obj(Number(font[1])).body, "/BaseFont /Helvetica");
    var p2 = added(r, 4);
    eq("a page shown turned a quarter is numbered across what is shown", p2.text, "2");
    ok("with the drawing turned to match: [0 1 -1 0 200 0]", p2.matrix.join(" ") === "0 1 -1 0 200 0", p2.matrix.join(" "));
    ok("and centred along the bottom of the turned page, which is 100 wide", near(p2.u, (100 - width("2", 11)) / 2) && near(p2.v, MARGIN), p2.u + " " + p2.v);
    var p3 = added(r, 5);
    ok("a page with a crop box is numbered inside it", p3.matrix.join(" ") === "1 0 0 1 20 10" && near(p3.u, (160 - width("3", 11)) / 2), p3.matrix.join(" ") + " " + p3.u);
    ok("its drawing list keeps its old stream in the middle", p3.refs.length === 3 && p3.refs[1] === 9);
    var p4 = added(r, 6);
    eq("a page that already uses the name gets another one", p4.font, "F108tb1");
    has("and keeps its own font under the old name", p4.body, "/F108tb 13 0 R");
    eq("the bookmark is untouched", r.obj(1).body.indexOf("/Outlines 12 0 R") > -1, true);
    eq("one section was added", r.sections, 2);
    has("the done message counts the pages", txt("msg"), "Done: 4 pages numbered");
  });

  /* ================= other styles and places ================= */
  step(function () {
    set("format", "pageof");
    set("position", "top-right");
    set("start", "5");
    set("size", "9");
    eq("the example follows", txt("example"), "Page 1 will read " + LQ + "Page 5 of 8" + RQ + ", the last page " + LQ + "Page 8 of 8" + RQ + ".");
    return download();
  });
  step(function (d) {
    var r = readChain(d.bytes);
    var p1 = added(r, 3);
    eq("Page 5 of 8 on the first page", p1.text, "Page 5 of 8");
    ok("against the right edge, 10 mm in, and 10 mm below the top less the letters' height",
       near(p1.u, 200 - MARGIN - width("Page 5 of 8", 9)) && near(p1.v, 100 - MARGIN - 0.72 * 9), p1.u + " " + p1.v);
    eq("and so on to the last", added(r, 6).text, "Page 8 of 8");
    set("format", "slash");
    set("position", "bottom-left");
    set("start", "1");
    document.getElementById("skipFirst").checked = true;
    document.getElementById("skipFirst").dispatchEvent(new Event("change", { bubbles: true }));
    eq("with the cover left out, three pages get numbers", txt("sNumbered"), "3");
    eq("starting on page 2", txt("example"), "Page 2 will read " + LQ + "1 / 3" + RQ + ", the last page " + LQ + "3 / 3" + RQ + ".");
    return download();
  });
  step(function (d) {
    var r = readChain(d.bytes);
    var tail = latin1(d.bytes.subarray(DOC.length));
    eq("the cover is not written again", /^3 0 obj$/m.test(tail), false);
    var p2 = added(r, 4);
    eq("page 2 reads 1 / 3", p2.text, "1 / 3");
    ok("at the bottom left of what is shown", near(p2.u, MARGIN) && near(p2.v, MARGIN));
    eq("page 4 reads 3 / 3", added(r, 6).text, "3 / 3");
    set("format", "page");
    eq("Page n", txt("example"), "Page 2 will read " + LQ + "Page 1" + RQ + ", the last page " + LQ + "Page 3" + RQ + ".");
    set("start", "-1");
    eq("a negative start is refused", txt("msg"), "Type the first number to print: a whole number, 0 or more.");
    ok("with nothing to download", document.getElementById("dlBtn").disabled);
    set("start", "2.5");
    eq("so is a fraction", txt("msg"), "Type the first number to print: a whole number, 0 or more.");
    set("start", "0");
    eq("zero is allowed", txt("example"), "Page 2 will read " + LQ + "Page 0" + RQ + ", the last page " + LQ + "Page 2" + RQ + ".");
    set("size", "5");
    eq("a size below 6 is refused", txt("msg"), "Choose a text size from 6 to 36.");
    set("size", "37");
    eq("and above 36", txt("msg"), "Choose a text size from 6 to 36.");
    set("size", "11");
    set("start", "1");
  });

  /* ================= other kinds of file ================= */
  step(function () {
    feed(F.one);
    return wait("a one-page PDF is read", read, 10000);
  });
  step(function () {
    eq("a single page that is the cover leaves nothing to number", txt("msg"), "This PDF has only one page, and it is the one being left without a number.");
    document.getElementById("skipFirst").checked = false;
    document.getElementById("skipFirst").dispatchEvent(new Event("change", { bubbles: true }));
    set("format", "plain");
    set("position", "bottom-center");
    feed(F.packed);
    return wait("a PDF with a packed index is read", read, 10000);
  });
  step(function () { return download(); });
  step(function (d) {
    var original = fromB64(TINY.objstm);
    ok("a file whose index is a stream keeps its bytes", startsWith(d.bytes, original));
    var tail = latin1(d.bytes.subarray(original.length));
    has("and gets its addition as a stream", tail, "/Type /XRef");
    has("with the number drawn", tail, "(1) Tj");
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    has("a damaged file is rebuilt, and the visitor told", txt("msg"), "This file's index is damaged, so it will be rebuilt: its pages are kept, but bookmarks and forms are not.");
    return download();
  });
  step(function (d) {
    var t = latin1(d.bytes);
    eq("the rebuilt file has one index", (t.match(/startxref/g) || []).length, 1);
    has("its page carries the number", t, "(1) Tj");
    has("and the old drawing", t, "(T1) Tj");
    has("and Helvetica", t, "/BaseFont /Helvetica");
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("it is turned away by name", txt("msg"), "Could not use notes.pdf: not a PDF.");
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one too", txt("msg"), "Could not use locked.pdf: protected with a password.");
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + txt("sPages") + "|" + txt("example"), "Choose a PDF.|" + DASH + "|");
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for add-pdf-page-numbers ----

# ---- START: the test for pdf-metadata-editor ----
T["pdf-metadata-editor"] = r"""
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function hex(s) { var o = ""; for (var i = 0; i < s.length; i++) { o += (s.charCodeAt(i) < 16 ? "0" : "") + s.charCodeAt(i).toString(16).toUpperCase(); } return o; }
  function utf16hex(s) { var o = "FEFF"; for (var i = 0; i < s.length; i++) { o += ("0000" + s.charCodeAt(i).toString(16).toUpperCase()).slice(-4); } return o; }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return out;
  }

  /* The details: a title in Telugu (UTF-16), an author, a company, two
     dates, a "made with" in PDFDocEncoding (0x92 is the trade mark sign), a
     "converted by" in UTF-8 with its mark, an XMP copy naming the author
     again on the catalog and another on the page's drawing, a bookmark.
     The first save had another author, "Old Name"; an incremental edit
     replaced it, which leaves the old version inside the file. */
  var TELUGU = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
  var TM = String.fromCharCode(0x2122);
  var XMP = "<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc:creator>Asha Rao<\/dc:creator><\/x:xmpmeta>";
  var XMP2 = "<x:xmpmeta xmlns:x='adobe:ns:meta/'>Second copy, Asha Rao<\/x:xmpmeta>";
  var DRAWING = "BT /F1 12 Tf 10 10 Td (M1) Tj ET";
  var FIRST = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R /Outlines 6 0 R /Metadata 5 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100]>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 7 0 R>>",
    "<<\/Title (Report) /Author (Old Name) /CreationDate (D:20260914103000+05'30')>>",
    { dict: "<<\/Type /Metadata /Subtype /XML /Length LEN>>", data: XMP },
    "<<\/Type /Outlines /Count 0>>",
    { dict: "<<\/Length 9 0 R /Metadata 8 0 R>>", data: DRAWING },
    { dict: "<<\/Type /Metadata /Subtype /XML /Length LEN>>", data: XMP2 },
    String(DRAWING.length)
  ], " /Info 4 0 R");
  var x1 = /startxref\n(\d+)\n%%EOF\n$/.exec(FIRST)[1];
  var update = "4 0 obj\n<<\/Title <" + utf16hex("Report " + TELUGU) + "> /Author (Asha Rao) /Subject (Quarterly figures) /Keywords (sales, Q3) " +
               "/Creator (Writer" + String.fromCharCode(0x92) + " 7.2) /Producer <EFBBBF" + hex("Maker PDF 1.0") + "> /Company (Acme Traders) /Trapped /False /Extra [1 2] " +
               "/CreationDate (D:20260914103000+05'30') /ModDate (D:20261001090500Z)>>\nendobj\n";
  var at4 = FIRST.length;
  var x2 = at4 + update.length;
  var DOC = bytesOfText(FIRST + update + "xref\n4 1\n" + ("0000000000" + at4).slice(-10) + " 00000 n \ntrailer\n<<\/Size 10 /Root 1 0 R /Info 4 0 R" + " /Prev " + x1 + ">>\nstartxref\n" + x2 + "\n%%EOF\n");
  var DOC_ENC = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R"));
  var DOC_EMPTY = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [] /Count 0>>"]));
  /* signed, as the form says (SigFlags), and signed, as a signature field holding a value says */
  var DOC_SIGNED = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R /AcroForm <<\/Fields [] /SigFlags 3>>>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>"]));
  var DOC_SIGFIELD = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R /AcroForm <<\/Fields [4 0 R]>>>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/T (Group) /Kids [5 0 R]>>",
                         "<<\/FT /Sig /T (Signature1) /Parent 4 0 R /V 6 0 R>>", "<<\/Type /Sig /Filter /Adobe.PPKLite>>"]));
  var DOC_EMPTYSIG = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R /AcroForm <<\/Fields [4 0 R] /SigFlags 2>>>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/FT /Sig /T (Sign here)>>"]));
  var DOC_ONE = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Producer (Printer 2)>>"], " /Info 4 0 R"));
  var F = {
    doc: new File([DOC], "report.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    empty: new File([DOC_EMPTY], "empty.pdf", { type: "application/pdf" }),
    one: new File([DOC_ONE], "one.pdf", { type: "application/pdf" }),
    signed: new File([DOC_SIGNED], "signed.pdf", { type: "application/pdf" }),
    sigField: new File([DOC_SIGFIELD], "contract.pdf", { type: "application/pdf" }),
    emptySig: new File([DOC_EMPTYSIG], "form.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* a reader for the saved file: one classic table, as the page writes it */
  function readOut(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    var head = /^xref\n0 (\d+)\n/.exec(t.slice(at));
    if (!head) { throw new Error("startxref does not point at the table"); }
    var count = Number(head[1]), first = at + head[0].length, objs = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(t.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]), tag = n + " 0 obj\n";
      if (t.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      objs[n] = t.slice(off + tag.length, t.indexOf("\nendobj\n", off));
    }
    var trailer = t.slice(first + count * 20);
    var info = objs[Number(/\/Info (\d+) 0 R/.exec(trailer)[1])];
    var root = objs[Number(/\/Root (\d+) 0 R/.exec(trailer)[1])];
    return { text: t, info: info, root: root, objs: objs, sections: (t.match(/startxref/g) || []).length };
  }
  function infoValue(r, key) {
    var m = new RegExp("/" + key + " <([0-9A-F]*)>").exec(r.info);
    if (!m) { return null; }
    var b = m[1].match(/../g) || [];
    if (b[0] === "FE" && b[1] === "FF") {
      var s = "";
      for (var i = 2; i + 1 < b.length; i += 2) { s += String.fromCharCode(parseInt(b[i] + b[i + 1], 16)); }
      return s;
    }
    return b.map(function (h) { return String.fromCharCode(parseInt(h, 16)); }).join("");
  }
  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    if (window.__saved.length !== before + 1) { return Promise.resolve(null); }
    var saved = window.__saved[window.__saved.length - 1];
    return saved.blob.arrayBuffer().then(function (b) {
      var bytes = new Uint8Array(b), out = null, error = "";
      try { out = readOut(bytes); } catch (e) { error = e.message; }
      return { saved: saved, out: out, error: error };
    });
  }
  /* every stream's /Length is the number of bytes between "stream" and "endstream" */
  function lengthsRight(r) {
    var bad = [];
    Object.keys(r.objs).forEach(function (n) {
      var o = r.objs[n], at = o.indexOf("\nstream\n");
      if (at < 0) { return; }
      var len = Number((/\/Length (\d+)/.exec(o.slice(0, at)) || [])[1]);
      if (o.substr(at + 8 + len) !== "\nendstream") { bad.push(n); }
    });
    return bad.join() || "all right";
  }
  function nowhere(r, text) {
    return r.text.indexOf(text) < 0 && r.text.indexOf(hex(text)) < 0 && r.text.indexOf(utf16hex(text).slice(4)) < 0;
  }

  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function others() { return Array.prototype.map.call(document.getElementById("others").children, function (li) { return li.textContent; }).join(); }
  function checked(id) { return document.getElementById(id).checked; }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF to see the details it carries.");
  ok("the boxes are disabled until there is a file", document.getElementById("fTitle").disabled && document.getElementById("fAuthor").disabled);
  ok("and the button", document.getElementById("dlBtn").disabled);

  /* ================= what the file carries ================= */
  step(function () {
    feed(F.doc);
    eq("while it reads, it says so", txt("msg"), "Reading report.pdf" + String.fromCharCode(0x2026));
    ok("with nothing to save yet", document.getElementById("dlBtn").disabled);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    ok("then the boxes can be typed in", !document.getElementById("fTitle").disabled && !document.getElementById("fProducer").disabled);
    ok("and the file saved", !document.getElementById("dlBtn").disabled);
    eq("the title is read from UTF-16, Telugu and all", val("fTitle"), "Report " + TELUGU);
    eq("the newest author, not the old one", val("fAuthor"), "Asha Rao");
    eq("subject, keywords, made with, converted by",
       [val("fSubject"), val("fKeywords"), val("fCreator"), val("fProducer")].join("|"), "Quarterly figures|sales, Q3|Writer" + TM + " 7.2|Maker PDF 1.0");
    eq("the dates are shown as written, with their offsets", txt("dates"),
       "Created: 14 Sep 2026, 10:30 (UTC+05:30). Last changed: 1 Oct 2026, 09:05 UTC.");
    eq("unusual details are listed, and one that is not text says it is left out", others(),
       "Company: Acme Traders,Trapped: False,Extra: (not text, always left out)");
    ok("in their own box", document.getElementById("othersField").style.display !== "none");
    eq("the message counts the details and mentions the XMP copy", txt("msg"),
       "report.pdf: 6 details are filled in. It also carries a second copy of them (XMP), which is removed when you save, so every reader shows the same details.");

    /* ================= changing two, removing one ================= */
    set("fAuthor", "");
    set("fTitle", "  Q3 report  ");
    set("fKeywords", "R" + String.fromCharCode(0xE9) + "sum" + String.fromCharCode(0xE9) + " " + String.fromCharCode(0x03A9));
    return download();
  });
  step(function (d) {
    ok("the saved file reads back cleanly", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    var r = d.out;
    eq("named for what was done to it", d.saved.name, "report (details edited).pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("the new title, without the spaces around it", infoValue(r, "Title"), "Q3 report");
    ok("plain ASCII stays plain", /\/Title <(?!FEFF)/.test(r.info), r.info);
    eq("a PDFDocEncoding character comes back as itself", infoValue(r, "Creator"), "Writer" + TM + " 7.2");
    eq("UTF-8 is not mistaken for bytes", infoValue(r, "Producer"), "Maker PDF 1.0");
    eq("every stream is as long as it says", lengthsRight(r), "all right");
    eq("only what the document uses is written: catalog, page tree, page, bookmarks, drawing, details",
       Object.keys(r.objs).length, 6);
    eq("text beyond ASCII is written in UTF-16", infoValue(r, "Keywords"), "R" + String.fromCharCode(0xE9) + "sum" + String.fromCharCode(0xE9) + " " + String.fromCharCode(0x03A9));
    eq("an emptied box is left out", infoValue(r, "Author"), null);
    eq("the author's name is nowhere in the file, not in the old version, not in the XMP copy", nowhere(r, "Asha Rao"), true);
    eq("nor the first author's", nowhere(r, "Old Name"), true);
    eq("the old title is gone too", nowhere(r, "Report"), true);
    eq("both XMP copies are removed, the catalog's and the drawing's", /\/Metadata|xmpmeta/.test(r.text), false);
    eq("one version only: the file was written again, not added to", r.sections, 1);
    has("the bookmarks are still there", r.root, "/Outlines");
    eq("the dates are kept", infoValue(r, "CreationDate") + "|" + infoValue(r, "ModDate"), "D:20260914103000+05'30'|D:20261001090500Z");
    eq("and the unusual details", infoValue(r, "Company"), "Acme Traders");
    has("a name stays a name", r.info, "/Trapped /False");
    eq("one that is not text is left out", /Extra/.test(r.text), false);
    eq("no date of this edit is added", (r.info.match(/Date/g) || []).length, 2);
    has("the done message counts what was saved", txt("msg"), "Done: 9 details saved");

    /* ================= the two ticks, one at a time ================= */
    document.getElementById("keepDates").checked = false;
    return download();
  });
  step(function (d) {
    eq("without the dates, no date is in it", /Date/.test(d.out.info), false);
    eq("but the other details are", infoValue(d.out, "Company"), "Acme Traders");
    document.getElementById("keepDates").checked = true;
    document.getElementById("keepOther").checked = false;
    return download();
  });
  step(function (d) {
    eq("without the other details, the dates stay", infoValue(d.out, "CreationDate"), "D:20260914103000+05'30'");
    eq("and the others go", /Company|Trapped|Acme/.test(d.out.text), false);
    eq("the boxes are still saved", infoValue(d.out, "Title"), "Q3 report");
    document.getElementById("keepOther").checked = true;

    /* ================= clearing everything ================= */
    click("clearAllBtn");
    eq("every box empties", ["fTitle", "fAuthor", "fSubject", "fKeywords", "fCreator", "fProducer"].map(val).join(""), "");
    eq("and the dates and other details are let go", document.getElementById("keepDates").checked + "," + document.getElementById("keepOther").checked, "false,false");
    return download();
  });
  step(function (d) {
    eq("the saved file has no details at all", d.out.info.replace(/\s/g, ""), "<<>>");
    has("and says so", txt("msg"), "every detail removed");
    eq("its pages are still there", (d.out.text.match(/\(M1\) Tj/g) || []).length, 1);
    set("fTitle", new Array(2002).join("x"));
    eq("a detail over 2,000 characters is refused", txt("msg"), "Keep each detail under 2,000 characters.");
    ok("with nothing to save", document.getElementById("dlBtn").disabled);
    set("fTitle", "");
  });

  /* ================= other kinds of file ================= */
  step(function () {
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    eq("a damaged file is read, and the visitor told", txt("msg"),
       "scan.pdf: no details are filled in. Its index is damaged; the saved copy is rebuilt from what could be read.");
    eq("it has no details", [val("fTitle"), val("fProducer")].join("|"), "|");
    eq("and no dates", txt("dates"), "Created: not recorded. Last changed: not recorded.");
    eq("and nothing else to list", document.getElementById("othersField").style.display, "none");
    eq("a new file starts with both ticks back on", checked("keepDates") + "," + checked("keepOther"), "true,true");
    return download();
  });
  step(function (d) {
    ok("and it can be saved", d && d.out, d ? d.error : "no download");
    eq("with its page", d && d.out ? (d.out.text.match(/\(T1\) Tj/g) || []).length : 0, 1);
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("it is turned away by name", txt("msg"), "Could not use notes.pdf: not a PDF.");
    ok("and the boxes are disabled", document.getElementById("fTitle").disabled);
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one too", txt("msg"), "Could not use locked.pdf: protected with a password.");
    feed(F.empty);
    return wait("a PDF without pages is looked at", read, 10000);
  });
  step(function () {
    eq("one without pages too", txt("msg"), "Could not use empty.pdf: has no pages.");
    feed(F.one);
    return wait("a PDF with one detail is read", read, 10000);
  });
  step(function () {
    eq("one detail is counted as one, and no XMP is mentioned where there is none", txt("msg"), "one.pdf: 1 detail is filled in.");
    return download();
  });
  step(function (d) {
    has("one detail saved is one", txt("msg"), "Done: 1 detail saved");
    eq("and it is the one that was there", infoValue(d.out, "Producer"), "Printer 2");
    feed(F.signed);
    return wait("a signed PDF is read", read, 10000);
  });
  step(function () {
    eq("a signed PDF is pointed out before saving", txt("msg"),
       "signed.pdf: no details are filled in. It is digitally signed: saving writes the file again, so the signature will no longer be valid.");
    eq("as a warning", document.getElementById("msg").className, "msg msg--bad");
    ok("but it can still be saved", !document.getElementById("dlBtn").disabled);
    feed(F.sigField);
    return wait("a PDF with a filled signature field is read", read, 10000);
  });
  step(function () {
    has("a signature found in a field, below a parent field, is pointed out too", txt("msg"), "It is digitally signed");
    feed(F.emptySig);
    return wait("a PDF with an empty signature field is read", read, 10000);
  });
  step(function () {
    eq("an empty signature field is not a signature", txt("msg"), "form.pdf: no details are filled in.");
    eq("and nothing is wrong with it", document.getElementById("msg").className, "msg msg--ok");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () {
    has("its name is shown as text", txt("msg"), "<iframe onload=zq>.pdf: 6 details");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + val("fTitle") + "|" + txt("dates") + "|" + others(), "Choose a PDF to see the details it carries.|||");
    ok("and locks the boxes again", document.getElementById("fTitle").disabled && document.getElementById("dlBtn").disabled);
  });

  /* ================= a slow disk =================
     Here a file's bytes arrive only when the test lets them through, so a
     clear, or another file, can come while the first is still being read.
     What finishes late must be dropped. (Not done with timers: the test's
     clock is virtual and runs ahead of a real read.) */
  var realArrayBuffer = Blob.prototype.arrayBuffer;
  var held = [];
  function holdReads() {
    Blob.prototype.arrayBuffer = function () {
      var self = this;
      return new Promise(function (resolve) {
        held.push(function () {
          var bytes = realArrayBuffer.call(self);
          resolve(bytes);
          return bytes.then(function () { return pause(20); });
        });
      });
    };
  }
  function release() { return held.shift()(); }
  step(function () {
    holdReads();
    feed(F.doc);
    click("resetBtn");
    return release();
  });
  step(function () {
    eq("a clear while the file is read leaves nothing behind", txt("msg") + "|" + val("fTitle"), "Choose a PDF to see the details it carries.|");
    ok("and nothing to save", document.getElementById("dlBtn").disabled);
    feed(F.doc);
    feed(F.notPdf);
    return release();
  });
  step(function () {
    eq("a late read of an earlier file is not shown", txt("msg") + "|" + val("fAuthor"), "Reading notes.pdf" + String.fromCharCode(0x2026) + "|");
    return release();
  });
  step(function () {
    eq("the file chosen last is the one that counts", txt("msg"), "Could not use notes.pdf: not a PDF.");
    eq("the first one's details do not turn up", val("fAuthor"), "");
    /* the other way round: the refusal of the first file arrives while the
       second is still being read, and must not be shown */
    feed(F.notPdf);
    feed(F.doc);
    return release();
  });
  step(function () {
    eq("a late refusal of an earlier file is not shown", txt("msg"), "Reading report.pdf" + String.fromCharCode(0x2026));
    return release();
  });
  step(function () {
    Blob.prototype.arrayBuffer = realArrayBuffer;
    eq("and the file chosen last is read", val("fAuthor"), "Asha Rao");
    eq("every read was let through", held.length, 0);
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for pdf-metadata-editor ----

# ---- START: the test for protect-pdf ----
T["protect-pdf"] = r"""
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* every Web Crypto call is counted while it runs: when none is running
     at a timer tick, the page has finished (or stopped) its work */
  var inflight = 0;
  ["encrypt", "decrypt", "digest", "importKey"].forEach(function (name) {
    var real = crypto.subtle[name].bind(crypto.subtle);
    crypto.subtle[name] = function () {
      inflight += 1;
      return real.apply(null, arguments).finally(function () { inflight -= 1; });
    };
  });

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function hexOf(bytes) { var o = ""; for (var i = 0; i < bytes.length; i++) { o += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16).toUpperCase(); } return o; }
  function fromHex(h) { var u = new Uint8Array(h.length / 2); for (var i = 0; i < u.length; i++) { u[i] = parseInt(h.substr(i * 2, 2), 16); } return u; }
  function utf16hex(s) { var o = "FEFF"; for (var i = 0; i < s.length; i++) { o += ("0000" + s.charCodeAt(i).toString(16).toUpperCase()).slice(-4); } return o; }
  function join(list) {
    var n = 0, at = 0;
    list.forEach(function (b) { n += b.length; });
    var out = new Uint8Array(n);
    list.forEach(function (b) { out.set(b, at); at += b.length; });
    return out;
  }
  function same(a, b) { if (a.length !== b.length) { return false; } for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) { return false; } } return true; }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return out;
  }

  /* ================= the files ================= */
  var TELUGU = String.fromCharCode(0x0C24, 0x0C46, 0x0C32, 0x0C41, 0x0C17, 0x0C41);
  var XMP = "<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc:creator>Asha Rao<\/dc:creator><\/x:xmpmeta>";
  var DRAWING = "BT /F1 12 Tf 10 10 Td (Secret words) Tj ET";
  var DOC = bytesOfText(makePdf([
    "<<\/Type /Catalog /Pages 2 0 R /Outlines 6 0 R /Metadata 5 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100]>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 7 0 R /Annots [<<\/Type /Annot /Subtype /Link /Rect [0 0 50 20] /A <<\/S /URI /URI (https://example.org/secret-link)>>>>]>>",
    "<<\/Title <" + utf16hex("Report " + TELUGU) + "> /Author (Asha Rao) /Producer (Maker PDF 1.0)>>",
    { dict: "<<\/Type /Metadata /Subtype /XML /Length LEN>>", data: XMP },
    "<<\/Type /Outlines /Count 0>>",
    { dict: "<<\/Length LEN>>", data: DRAWING }
  ], " /Info 4 0 R"));
  var DOC_ENC = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R"));
  var DOC_EMPTY = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [] /Count 0>>"]));
  var DOC_SIGNED = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R /AcroForm <<\/Fields [] /SigFlags 3>>>>",
                         "<<\/Type /Pages /Kids [3 0 R 4 0 R] /Count 2>>", "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>"]));
  var F = {
    doc: new File([DOC], "report.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    empty: new File([DOC_EMPTY], "empty.pdf", { type: "application/pdf" }),
    signed: new File([DOC_SIGNED], "signed.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([DOC], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };
  var PASSWORD = "correct horse battery staple";

  /* ================= reading the saved file ================= */
  function readOut(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { throw new Error("no startxref at the end"); }
    var at = Number(sx[1]);
    var head = /^xref\n0 (\d+)\n/.exec(t.slice(at));
    if (!head) { throw new Error("startxref does not point at the table"); }
    var count = Number(head[1]), first = at + head[0].length, objs = {};
    for (var n = 1; n < count; n++) {
      var m = /^(\d{10}) 00000 n \n$/.exec(t.substr(first + n * 20, 20));
      if (!m) { throw new Error("entry " + n + " is malformed"); }
      var off = Number(m[1]), tag = n + " 0 obj\n";
      if (t.substr(off, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
      var end = t.indexOf("\nendobj\n", off), body = t.slice(off + tag.length, end);
      var s = body.indexOf("\nstream\n");
      objs[n] = s < 0 ? { dict: body, data: null } : { dict: body.slice(0, s), data: body.slice(s + 8, body.length - 10) };
      if (s >= 0 && body.slice(body.length - 10) !== "\nendstream") { throw new Error("object " + n + ": no endstream"); }
    }
    var trailer = t.slice(first + count * 20);
    var enc = Number((/\/Encrypt (\d+) 0 R/.exec(trailer) || [])[1]);
    return { text: t, objs: objs, trailer: trailer, enc: enc, encDict: enc ? objs[enc].dict : "" };
  }
  function hexStrings(text) {
    var out = [], i = 0;
    while (i < text.length) {
      if (text.substr(i, 2) === "<<") { i += 2; continue; }
      if (text[i] === "<") { var e = text.indexOf(">", i); out.push(text.slice(i + 1, e)); i = e + 1; continue; }
      i += 1;
    }
    return out;
  }
  function encValue(r, key) { var m = new RegExp("/" + key + " <([0-9A-F]*)>").exec(r.encDict); return m ? fromHex(m[1]) : null; }

  /* ================= a decryptor of the test's own =================
     Written apart from the page (the modulo here is done as a real big
     number, the page adds bytes up), with Web Crypto underneath. */
  var subtle = crypto.subtle;
  function sha(name, data) { return subtle.digest(name, data).then(function (b) { return new Uint8Array(b); }); }
  function aesEncryptRaw(key, iv, data) {
    return subtle.importKey("raw", key, "AES-CBC", false, ["encrypt"]).then(function (k) {
      return subtle.encrypt({ name: "AES-CBC", iv: iv }, k, data);
    }).then(function (b) { return new Uint8Array(b).slice(0, data.length); });
  }
  function aesDecrypt(key, iv, data) {
    return subtle.importKey("raw", key, "AES-CBC", false, ["decrypt"]).then(function (k) {
      return subtle.decrypt({ name: "AES-CBC", iv: iv }, k, data);
    }).then(function (b) { return new Uint8Array(b); });
  }
  /* AES without padding, one way or the other, for the 32-byte key copies: decrypt
     a block of 16 bytes of padding added at the end, encrypted after the data */
  function aesDecryptRaw(key, iv, data) {
    return aesEncryptRaw(key, data.slice(data.length - 16), new Uint8Array(16).fill(16)).then(function (padBlock) {
      return aesDecrypt(key, iv, join([data, padBlock]));
    });
  }
  function hash2B(pw, salt, u) {
    return sha("SHA-256", join([pw, salt, u])).then(function (k0) {
      var rounds = 0;
      function round(k) {
        var one = join([pw, k, u]), many = new Uint8Array(one.length * 64);
        for (var j = 0; j < 64; j++) { many.set(one, j * one.length); }
        return aesEncryptRaw(k.slice(0, 16), k.slice(16, 32), many).then(function (e) {
          var mod = 0;
          for (var b = 0; b < 16; b++) { mod = (mod * 256 + e[b]) % 3; }
          return sha(["SHA-256", "SHA-384", "SHA-512"][mod], e).then(function (h) {
            rounds += 1;
            if (rounds < 64 || e[e.length - 1] > rounds - 32) { return round(h); }
            return h.slice(0, 32);
          });
        });
      }
      return round(k0);
    });
  }
  /* the file key from the password, or null when the password is wrong */
  function unlock(r, password) {
    var pw = bytesOfText(password), U = encValue(r, "U"), UE = encValue(r, "UE");
    return hash2B(pw, U.slice(32, 40), new Uint8Array(0)).then(function (h) {
      if (!same(h, U.slice(0, 32))) { return null; }
      return hash2B(pw, U.slice(40, 48), new Uint8Array(0)).then(function (k) { return aesDecryptRaw(k, new Uint8Array(16), UE); });
    });
  }
  function unlockOwner(r, password) {
    var pw = bytesOfText(password), O = encValue(r, "O"), OE = encValue(r, "OE"), U = encValue(r, "U");
    return hash2B(pw, O.slice(32, 40), U).then(function (h) {
      if (!same(h, O.slice(0, 32))) { return null; }
      return hash2B(pw, O.slice(40, 48), U).then(function (k) { return aesDecryptRaw(k, new Uint8Array(16), OE); });
    });
  }
  function decryptItem(key, bytes) {
    if (bytes.length < 32 || bytes.length % 16) { return Promise.reject(new Error("not IV + blocks: " + bytes.length)); }
    return aesDecrypt(key, bytes.slice(0, 16), bytes.slice(16));
  }
  /* every string and stream of the file decrypted: { strings: [...], streams: {num: text}, ivs: [...] } */
  function decryptAll(r, key) {
    var out = { strings: [], streams: {}, ivs: [] }, jobs = [];
    Object.keys(r.objs).forEach(function (n) {
      if (Number(n) === r.enc) { return; }
      var o = r.objs[n];
      hexStrings(o.dict).forEach(function (h) {
        var b = fromHex(h);
        out.ivs.push(h.slice(0, 32));
        jobs.push(decryptItem(key, b).then(function (p) { out.strings.push(latin1(p)); }));
      });
      if (o.data !== null) {
        var b = bytesOfText(o.data);
        out.ivs.push(hexOf(b.slice(0, 16)));
        jobs.push(decryptItem(key, b).then(function (p) { out.streams[n] = latin1(p); }));
      }
    });
    return Promise.all(jobs).then(function () { return out; });
  }

  function download(afterClick) {
    var before = window.__saved.length;
    click("dlBtn");
    if (afterClick) { afterClick(); }
    return wait("the protected file is made", function () { return window.__saved.length === before + 1 || /could not be protected/.test(txt("msg")); }, 60000)
      .then(function () {
        var saved = window.__saved[before];
        if (!saved) { return null; }
        return saved.blob.arrayBuffer().then(function (b) {
          var out = null, error = "";
          try { out = readOut(new Uint8Array(b)); } catch (e) { error = e.message; }
          return { saved: saved, out: out, error: error };
        });
      });
  }
  function settled() {
    var quiet = 0;
    return wait("the page's cryptography has stopped", function () { quiet = inflight === 0 ? quiet + 1 : 0; return quiet >= 3; }, 60000);
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }
  var history = [];
  new MutationObserver(function () { history.push(txt("msg")); }).observe(document.getElementById("msg"), { childList: true, characterData: true, subtree: true });

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF and a password", txt("msg"), "Choose a PDF, then a password for it.");
  ok("with nothing to protect yet", document.getElementById("dlBtn").disabled);
  eq("the password boxes hide what is typed", document.getElementById("pw").type + "," + document.getElementById("pw2").type, "password,password");

  /* ================= choosing a password ================= */
  step(function () {
    feed(F.doc);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    eq("the file is read and a password asked for", txt("msg"), "report.pdf: 1 page. Type a password for it.");
    set("pw", "abc");
    eq("then the second box", txt("msg"), "report.pdf: 1 page. Type the password again in the second box, to be sure of it.");
    ok("not ready yet", document.getElementById("dlBtn").disabled);
    set("pw2", "abd");
    eq("two different passwords are caught", txt("msg"), "report.pdf: 1 page. The two passwords are not the same yet.");
    eq("as a problem", document.getElementById("msg").className, "msg msg--bad");
    ok("and nothing can be protected", document.getElementById("dlBtn").disabled);
    set("pw2", "abc");
    eq("the same password twice is ready, with a word about its length", txt("msg"),
       "report.pdf: 1 page. Ready to protect. A password this short can be guessed by a computer; 12 characters or more is much safer.");
    eq("as good news", document.getElementById("msg").className, "msg msg--ok");
    ok("and can be used", !document.getElementById("dlBtn").disabled);
    set("pw", "abcdefg");
    set("pw2", "abcdefg");
    has("seven characters is still short", txt("msg"), "this short");
    set("pw", "abcdefgh");
    set("pw2", "abcdefgh");
    eq("eight is not called short", txt("msg"), "report.pdf: 1 page. Ready to protect.");
    set("pw", TELUGU);
    set("pw2", TELUGU);
    eq("letters beyond an English keyboard are refused, with the reason", txt("msg"),
       "report.pdf: 1 page. Use only letters, digits and symbols from an English keyboard: some PDF readers cannot open a file whose password has other characters.");
    ok("and cannot be used", document.getElementById("dlBtn").disabled);
    set("pw", "tab\there");
    set("pw2", "tab\there");
    has("a control character is refused too", txt("msg"), "Use only letters, digits and symbols");
    set("pw", "~ !\"#$%&'()*+,-./09:;<=>?@AZ[\\]^_`az{|}");
    set("pw2", "~ !\"#$%&'()*+,-./09:;<=>?@AZ[\\]^_`az{|}");
    eq("every symbol of an English keyboard, and the space, is fine", txt("msg"), "report.pdf: 1 page. Ready to protect.");
    var long = new Array(128).join("x");
    set("pw", long);
    set("pw2", long);
    eq("127 characters is fine", txt("msg"), "report.pdf: 1 page. Ready to protect.");
    set("pw", long + "x");
    set("pw2", long + "x");
    eq("128 is too many", txt("msg"), "report.pdf: 1 page. Keep the password to 127 characters or fewer.");
    ok("and cannot be used", document.getElementById("dlBtn").disabled);
    click("showPw");
    eq("the password can be shown", document.getElementById("pw").type + "," + document.getElementById("pw2").type, "text,text");
    click("showPw");
    eq("and hidden again", document.getElementById("pw").type + "," + document.getElementById("pw2").type, "password,password");
    set("pw", PASSWORD);
    set("pw2", PASSWORD);
    history.length = 0;
    return download();
  });

  /* ================= the protected file ================= */
  var first = null;
  step(function (d) {
    ok("the protected file reads back cleanly", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    first = d;
    var r = d.out;
    eq("while it worked, it said so", history.slice(0, 2).join("|"),
       "Locking report.pdf" + String.fromCharCode(0x2026) + " The password is being turned into a key, which takes a moment on purpose.|Encrypting report.pdf" + String.fromCharCode(0x2026) + " 0%");
    has("then that it is done", txt("msg"), "Done: report (protected).pdf, ");
    has("with what to keep in mind", txt("msg"), "locked with AES-256. Keep the password safe: without it nobody can open the file, not even this page. Nothing has left your device.");
    eq("named for what was done to it", d.saved.name, "report (protected).pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("a PDF 1.7 file", r.text.slice(0, 9), "%PDF-1.7\n");
    ok("with an /Encrypt dictionary in the trailer", r.enc > 0, r.trailer);
    ok("and an /ID of two 16-byte strings", /\/ID \[<[0-9A-F]{32}> <[0-9A-F]{32}>\]/.test(r.trailer), r.trailer);
    has("the old /Info is kept, encrypted", r.trailer, "/Info ");
    var e = r.encDict;
    ok("the standard handler, revision 6, AES-256", /\/Filter \/Standard/.test(e) && /\/V 5\b/.test(e) && /\/R 6\b/.test(e) && /\/Length 256\b/.test(e), e);
    ok("one crypt filter, AESV3, for streams and strings", /\/StdCF <<\/Type \/CryptFilter \/CFM \/AESV3 \/AuthEvent \/DocOpen \/Length 32>>/.test(e) &&
       /\/StmF \/StdCF/.test(e) && /\/StrF \/StdCF/.test(e), e);
    ok("every permission given, the XMP encrypted too", /\/P -4\b/.test(e) && /\/EncryptMetadata true/.test(e), e);
    eq("the sizes of the checks and the locked keys", [encValue(r, "U").length, encValue(r, "O").length, encValue(r, "UE").length,
       encValue(r, "OE").length, encValue(r, "Perms").length].join(), "48,48,32,32,16");
    has("marked as using AES-256 for readers of PDF 1.7", r.text, "/Extensions <<\/ADBE <<\/BaseVersion /1.7 /ExtensionLevel 8>>>>");
    eq("nothing is readable: not the drawing, not the details, not the XMP, not the link",
       ["Secret words", "Asha Rao", "Maker PDF", "xmpmeta", "secret-link", hexOf(bytesOfText("Asha Rao")), utf16hex("Report").slice(4)]
         .filter(function (s) { return r.text.indexOf(s) >= 0; }).join(), "");
    return unlock(r, PASSWORD + "!").then(function (key) {
      eq("a wrong password does not unlock it", key, null);
      return unlock(r, PASSWORD);
    }).then(function (key) {
      ok("the password unlocks the key", key && key.length === 32);
      d.key = key;
      return unlockOwner(r, PASSWORD);
    }).then(function (ownerKey) {
      ok("so does the owner's lock, to the same key", ownerKey && same(ownerKey, d.key));
      return aesDecryptRaw(d.key, new Uint8Array(16), encValue(r, "Perms"));
    }).then(function (perms) {
      eq("the encrypted permissions match /P, and say the XMP is encrypted", hexOf(perms.slice(0, 12)), "FCFFFFFFFFFFFFFF54616462");
      return decryptAll(r, d.key);
    }).then(function (all) {
      ok("the title comes back, Telugu and all", all.strings.indexOf(latin1(fromHex(utf16hex("Report " + TELUGU)))) >= 0, all.strings.join(" | "));
      ok("the author and the link too", all.strings.indexOf("Asha Rao") >= 0 && all.strings.indexOf("https://example.org/secret-link") >= 0);
      var streams = Object.keys(all.streams).map(function (n) { return all.streams[n]; });
      ok("the drawing comes back exactly", streams.indexOf(DRAWING) >= 0, streams.join(" | "));
      ok("and the XMP", streams.indexOf(XMP) >= 0);
      eq("every string and stream has its own IV", all.ivs.filter(function (iv, i) { return all.ivs.indexOf(iv) !== i; }).length, 0);
      eq("all of them were found: four strings (title, author, producer, link) and two streams", all.ivs.length, 6);
      has("the bookmarks are still there", r.text, "/Outlines");
      /* typing while it works changes neither the message nor the file */
      return download(function () {
        set("pw2", "changed while working");
        has("typing while it works leaves the message alone", txt("msg"), "Locking report.pdf");
        ok("and the button off", document.getElementById("dlBtn").disabled);
      });
    });
  });
  step(function (d) {
    ok("protecting again works", d && d.out, d ? d.error : "no download");
    ok("and gives a different file: new salts, a new key", d && first && d.out.text !== first.out.text);
    has("it says it is done", txt("msg"), "Done: report (protected).pdf");
    ok("and, the two boxes now differing, offers no more", document.getElementById("dlBtn").disabled);
    set("pw2", PASSWORD);
    return unlock(d.out, PASSWORD).then(function (key) {
      ok("the password chosen when it started opens it", key && !same(key, first.key));
    });
  });

  /* ================= known answers =================
     The page's randomness replaced by a fixed sequence of bytes, so the
     checks and locked keys are fixed too; the answers were worked out
     apart from the page, in Python with Windows' own AES. This sequence
     makes the four slow hashes end after 83, 64, 65 and 66 rounds, one of
     them just past the edge, so a hash that stops a round early or late
     gives a different answer. */
  step(function () {
    var k = 0;
    crypto.getRandomValues = function (a) {
      for (var i = 0; i < a.length; i++, k++) { a[i] = (k * 7 + (k >> 8)) & 255; }
      return a;
    };
    set("pw", "known answer");
    set("pw2", "known answer");
    return download();
  });
  step(function (d) {
    delete crypto.getRandomValues;
    var r = d && d.out;
    ok("the known-answer file is made", r, d ? d.error : "no download");
    if (!r) { return; }
    eq("/U, the check of the password", hexOf(encValue(r, "U")),
       "0E096569C37B3D49E20B7170E113A4D5616CE77E167A5AD3F24B6A90F4BDA407E0E7EEF5FC030A11181F262D343B4249");
    eq("/UE, the key locked with it", hexOf(encValue(r, "UE")), "91DE1E4E5578338511C66BCD873584C6D1570CE1D0B0F3217AAE7935C674AEFC");
    eq("/O, the owner's check", hexOf(encValue(r, "O")),
       "5B6AC26906E5208093F06B2328A1FA998870B787F0B5889F9519A18D4704F8DB50575E656C737A81888F969DA4ABB2B9");
    eq("/OE, the key locked for the owner", hexOf(encValue(r, "OE")), "5EB11BDF6F06DB6CA18B2147584C184FB04052C7512D0E4A5CB831F06679D1E9");
    eq("/Perms", hexOf(encValue(r, "Perms")), "8F580D7E5188BCFADCDA146B821231F6");
    ok("and the page's own randomness is back", crypto.getRandomValues(new Uint8Array(8)).join() !== "0,7,14,21,28,35,42,49");
    set("pw", PASSWORD);
    set("pw2", PASSWORD);
  });

  /* ================= stopping half-way ================= */
  step(function () {
    var before = window.__saved.length;
    click("dlBtn");
    click("resetBtn");
    return settled().then(function () {
      eq("a clear while it works stops it: nothing is saved", window.__saved.length, before);
      eq("and the page is empty again", txt("msg") + "|" + val("pw") + "|" + val("pw2"), "Choose a PDF, then a password for it.||");
    });
  });
  step(function () {
    feed(F.doc);
    return wait("the PDF is read again", read, 10000);
  });
  step(function () {
    set("pw", PASSWORD);
    set("pw2", PASSWORD);
    var before = window.__saved.length;
    click("dlBtn");
    feed(F.notPdf);
    return settled().then(function () { return wait("the other file is looked at", read, 10000); }).then(function () {
      eq("a new file while it works stops it too", window.__saved.length, before);
      eq("and the new file is the one spoken of", txt("msg"), "Could not use notes.pdf: not a PDF.");
    });
  });

  /* ================= other kinds of file ================= */
  step(function () {
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    eq("a damaged file is read, and the visitor told", txt("msg"),
       "scan.pdf: 1 page. Ready to protect. Its index is damaged; the protected copy is rebuilt from what could be read.");
    ok("the password stays when another file is chosen", !document.getElementById("dlBtn").disabled);
    return download();
  });
  step(function (d) {
    ok("and it can be protected", d && d.out, d ? d.error : "no download");
    return unlock(d.out, PASSWORD).then(function (key) { return decryptAll(d.out, key); }).then(function (all) {
      ok("with its page", Object.keys(all.streams).some(function (n) { return all.streams[n].indexOf("(T1) Tj") >= 0; }));
      feed(F.signed);
      return wait("a signed PDF is read", read, 10000);
    });
  });
  step(function () {
    eq("a signed PDF is pointed out before protecting", txt("msg"),
       "signed.pdf: 2 pages. Ready to protect. It is digitally signed: protecting writes the file again, so the signature will no longer be valid.");
    eq("as a warning", document.getElementById("msg").className, "msg msg--bad");
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a file with a password already is turned away", txt("msg"), "Could not use locked.pdf: it already has a password.");
    ok("with nothing to protect", document.getElementById("dlBtn").disabled);
    feed(F.empty);
    return wait("a PDF without pages is looked at", read, 10000);
  });
  step(function () {
    eq("one without pages too", txt("msg"), "Could not use empty.pdf: has no pages.");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () {
    has("its name is shown as text", txt("msg"), "<iframe onload=zq>.pdf: 1 page.");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    click("showPw");
    click("resetBtn");
    eq("clear empties everything", txt("msg") + "|" + val("pw") + "|" + val("pw2"), "Choose a PDF, then a password for it.||");
    eq("and hides the password boxes again", document.getElementById("pw").type + "," + document.getElementById("pw2").type + "," +
       document.getElementById("showPw").checked, "password,password,false");
    ok("with nothing to protect", document.getElementById("dlBtn").disabled);
  });

  chain.then(function () { finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    finish();
  });
"""
# ---- END: the test for protect-pdf ----

# ---- START: the test for pdf-to-image ----
T["pdf-to-image"] = r"""  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  var RC4_PDF = "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMjAwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzOT4+CnN0cmVhbQqreggKebynsir3Iev6CDjCV/GAiArFJrs6yU+rUTJO595LoG2huMoKZW5kc3RyZWFtCmVuZG9iago1IDAgb2JqCjw8L1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhPj4KZW5kb2JqCjYgMCBvYmoKPDwvVGl0bGUgPDI4Yjc4MzU3M2M3NmEwNjgyNDNhOTE4ZT4+PgplbmRvYmoKNyAwIG9iago8PC9GaWx0ZXIgL1N0YW5kYXJkIC9WIDIgL1IgMyAvTGVuZ3RoIDEyOCAvUCAtNCAvTyA8OWY1NGRhNzRhYjZkOWI3YzkxZTVhY2ZiZmZmMmRiY2QxZDMzYTk3OTMxZTQyMThkZTY3NTIyZTRkMWZlNTcxMD4gL1UgPDgyNTJjMTBlMWVmZmE1ZGYwMjk3YjdmMmFjOGU3Nzc2MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA+Pj4KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDYyIDAwMDAwIG4gCjAwMDAwMDAxNDAgMDAwMDAgbiAKMDAwMDAwMDIzNiAwMDAwMCBuIAowMDAwMDAwMzIzIDAwMDAwIG4gCjAwMDAwMDAzOTEgMDAwMDAgbiAKMDAwMDAwMDQ0NCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvSW5mbyA2IDAgUiAvRW5jcnlwdCA3IDAgUiAvSUQgWzxmNTIxODRlNTc4YjZjM2M3NzE0YjEwMGEzZWRmMTk3ZD4gPGY1MjE4NGU1NzhiNmMzYzc3MTRiMTAwYTNlZGYxOTdkPl0+PgpzdGFydHhyZWYKNjQ5CiUlRU9GCg==";
  var AES_PDF = "JVBERi0xLjcKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUiAvRXh0ZW5zaW9ucyA8PC9BREJFIDw8L0Jhc2VWZXJzaW9uIC8xLjcgL0V4dGVuc2lvbkxldmVsIDg+Pj4+Pj4KZW5kb2JqCjIgMCBvYmoKPDwvVHlwZSAvUGFnZXMgL0tpZHMgWzMgMCBSXSAvQ291bnQgMT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAyMDAgMTAwXSAvQ29udGVudHMgNCAwIFI+PgplbmRvYmoKNCAwIG9iago8PC9MZW5ndGggODA+PgpzdHJlYW0KZZ+sjVm9EBZygh0eIQhrv7fU2DAY+Idnvt3ADK36uGc8qpWWPropMgqllvn4u8gO4xtaDIkcKROdjYzJJGG6k4enyJa/ogBiAYU3WBVZDNAKZW5kc3RyZWFtCmVuZG9iago1IDAgb2JqCjw8L0ZpbHRlciAvU3RhbmRhcmQgL1YgNSAvUiA2IC9MZW5ndGggMjU2IC9DRiA8PC9TdGRDRiA8PC9UeXBlIC9DcnlwdEZpbHRlciAvQ0ZNIC9BRVNWMyAvQXV0aEV2ZW50IC9Eb2NPcGVuIC9MZW5ndGggMzI+Pj4+IC9TdG1GIC9TdGRDRiAvU3RyRiAvU3RkQ0YgL08gPEQ2Nzg3MkQ5QTZEMzc1OTEzQzY3RjE2MTlCQjE4Q0JCN0YwRTgxMEU1REVFNzg1QjNBQ0Y1OEE5NTg2REZEMzU4MTNBRDA1MjExRjRGQTQ2Q0ZFRDJDNUUzQUE1QTY2MD4gL1UgPDg0Qzg2OTVBNTU2RDhGREFFODdDMjExREIxRERBOTYyQkVGRENGRDBDOTA1REYyQTkwNjlEOTQ5QTMwOTUxN0RDMUJGMjdGN0M4MkE4RDU4N0RDNDU1RTYwOEYxRTg4Nz4gL09FIDw0RENBNzVDQjNFRDVEQUE4OEI5NDIyRUNFQTFDMEE3REI4NTUyQTI5OTVFMEQ0MzExMDM4OEMzRkFEREZCQ0U4PiAvVUUgPDExMDEzMjE1N0Y3OTgzOTFCMjE5MENBNUJCNDg5RkQxNENFNDJBNDgyMTY1M0E2QUUxRURCQTE1RUFBMTU5RTU+IC9QIC00IC9QZXJtcyA8N0Q3M0I4QTc5RTU5RkMzMTJDQzkzMTAxNzYxODE0NUI+IC9FbmNyeXB0TWV0YWRhdGEgdHJ1ZT4+CmVuZG9iagp4cmVmCjAgNgowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMTUgMDAwMDAgbiAKMDAwMDAwMDEyNCAwMDAwMCBuIAowMDAwMDAwMTc5IDAwMDAwIG4gCjAwMDAwMDAyNjQgMDAwMDAgbiAKMDAwMDAwMDM5MiAwMDAwMCBuIAp0cmFpbGVyCjw8IC9TaXplIDYgL1Jvb3QgMSAwIFIgL0VuY3J5cHQgNSAwIFIgL0lEIFs8QzU1QjNBRjdGM0FGNzRDNzg4QzNENDgwMTlEMURGNTY+IDxDNTVCM0FGN0YzQUY3NEM3ODhDM0Q0ODAxOUQxREY1Nj5dID4+CnN0YXJ0eHJlZgo5NzQKJSVFT0YK";
  var FONTS_PDF = "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUiA1IDAgUiAxMSAwIFJdIC9Db3VudCAzPj4KZW5kb2JqCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDI0MCAxMDBdIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA3IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSPj4KZW5kb2JqCjQgMCBvYmoKPDwvTGVuZ3RoIDM5Pj4Kc3RyZWFtCkJUIC9GMSAzNiBUZiAxMCAzMCBUZCAoSGVsbG8gMTA4KSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDI0MCAxMDBdIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMiA4IDAgUj4+Pj4gL0NvbnRlbnRzIDYgMCBSPj4KZW5kb2JqCjYgMCBvYmoKPDwvTGVuZ3RoIDU0Pj4Kc3RyZWFtCkJUIC9GMiAzNiBUZiAxMCAzMCBUZCA8NjVFNTY3MkM4QTlFMzBDNjMwQjkzMEM4PiBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2EgL0VuY29kaW5nIC9XaW5BbnNpRW5jb2Rpbmc+PgplbmRvYmoKOCAwIG9iago8PC9UeXBlIC9Gb250IC9TdWJ0eXBlIC9UeXBlMCAvQmFzZUZvbnQgL0tvek1pblByNk4tUmVndWxhciAvRW5jb2RpbmcgL1VuaUpJUy1VQ1MyLUggL0Rlc2NlbmRhbnRGb250cyBbOSAwIFJdPj4KZW5kb2JqCjkgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvQ0lERm9udFR5cGUwIC9CYXNlRm9udCAvS296TWluUHI2Ti1SZWd1bGFyIC9DSURTeXN0ZW1JbmZvIDw8L1JlZ2lzdHJ5IChBZG9iZSkgL09yZGVyaW5nIChKYXBhbjEpIC9TdXBwbGVtZW50IDY+PiAvRm9udERlc2NyaXB0b3IgMTAgMCBSIC9EVyAxMDAwPj4KZW5kb2JqCjEwIDAgb2JqCjw8L1R5cGUgL0ZvbnREZXNjcmlwdG9yIC9Gb250TmFtZSAvS296TWluUHI2Ti1SZWd1bGFyIC9GbGFncyA2IC9Gb250QkJveCBbLTQzNyAtMzQwIDExNDcgMTMxN10gL0l0YWxpY0FuZ2xlIDAgL0FzY2VudCAxMTM3IC9EZXNjZW50IC0zNDkgL0NhcEhlaWdodCA3NDIgL1N0ZW1WIDgwPj4KZW5kb2JqCjExIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAyNDAgMTAwXSAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjMgMTMgMCBSPj4+PiAvQ29udGVudHMgMTIgMCBSPj4KZW5kb2JqCjEyIDAgb2JqCjw8L0xlbmd0aCAzND4+CnN0cmVhbQpCVCAvRjMgNDggVGYgMTAgMzAgVGQgKDQ0NDQpIFRqIEVUCmVuZHN0cmVhbQplbmRvYmoKMTMgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9aYXBmRGluZ2JhdHM+PgplbmRvYmoKeHJlZgowIDE0CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAwOSAwMDAwMCBuIAowMDAwMDAwMDU2IDAwMDAwIG4gCjAwMDAwMDAxMjQgMDAwMDAgbiAKMDAwMDAwMDI0NCAwMDAwMCBuIAowMDAwMDAwMzMxIDAwMDAwIG4gCjAwMDAwMDA0NTEgMDAwMDAgbiAKMDAwMDAwMDU1MyAwMDAwMCBuIAowMDAwMDAwNjQ4IDAwMDAwIG4gCjAwMDAwMDA3NzUgMDAwMDAgbiAKMDAwMDAwMDk2MSAwMDAwMCBuIAowMDAwMDAxMTQxIDAwMDAwIG4gCjAwMDAwMDEyNjQgMDAwMDAgbiAKMDAwMDAwMTM0NyAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgMTQgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMTQxOQolJUVPRgo=";
  var JPX_PDF = "JVBERi0xLjUKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDE+PgplbmRvYmoKMyAwIG9iago8PC9UeXBlIC9QYWdlIC9QYXJlbnQgMiAwIFIgL01lZGlhQm94IFswIDAgNjEyIDc5Ml0gL0NvbnRlbnRzIDQgMCBSIC9SZXNvdXJjZXMgPDwvWE9iamVjdCA8PC9JbSA1IDAgUj4+Pj4+PgplbmRvYmoKNCAwIG9iago8PC9MZW5ndGggMzM+PgpzdHJlYW0KcSA0MDAgMCAwIDIwMCAxMDAgMzAwIGNtIC9JbSBEbyBRCmVuZHN0cmVhbQplbmRvYmoKNSAwIG9iago8PC9UeXBlIC9YT2JqZWN0IC9TdWJ0eXBlIC9JbWFnZSAvV2lkdGggMjAwIC9IZWlnaHQgMTAwIC9GaWx0ZXIgL0pQWERlY29kZSAvTGVuZ3RoIDQ5OT4+CnN0cmVhbQoAAAAMalAgIA0KhwoAAAAUZnR5cGpwMiAAAAAAanAyIAAAAC1qcDJoAAAAFmloZHIAAABkAAAAyAADBwcAAAAAAA9jb2xyAQAAAAAAEAAAAaZqcDJj/0//UQAvAAAAAADIAAAAZAAAAAAAAAAAAAAAyAAAAGQAAAAAAAAAAAADBwEBBwEBBwEB/1IADAAAAAEABQQEAAH/XAATQEBISFBISFBISFBISFBISFD/ZAAlAAFDcmVhdGVkIGJ5IE9wZW5KUEVHIHZlcnNpb24gMi41LjT/kAAKAAAAAAEfAAH/k8+0ZBQAKtDvzdN+7wEjaNcXPaKoBnfaMzyjiunPtDgRUFSutA4AAGEXdVj/f8+0VBFQItDm9tqAqijWABFhJ4yJY7pQGsfaEAAhbdklpNJN14DH2hAAIhnbJaTSTdfH2hYAMNGnKq/Uib2yug+Ax9oWADF9qSqv1Im9ssQfw+oRAE+XxRY8XHE5ueHTP39490PugMPqEQBQQ8cWPFxxObnhtr5vp8iyUcfaMACD7a9mQxAkC59PEXqqv5BeESYYtbrqxD+Ax9oSAIPCruZ/XUPMD+P2jYCeijHP/sPQX7AAAMJCGtJhIRLx4wkIgDwYSEeA4/aNgJ5fMU/+w9BfsAAAwkIa0mEhEvHjCQiAPBhIR//ZCmVuZHN0cmVhbQplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDExMSAwMDAwMCBuIAowMDAwMDAwMjM0IDAwMDAwIG4gCjAwMDAwMDAzMTUgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKOTM2CiUlRU9GCg==";
  window.__saved = [];
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };
  var realBitmap = window.createImageBitmap;

  /* pdf.js does its work in a worker, a thread of its own that the test's
     virtual clock does not wait for: with nothing to do but wait, the page
     let the clock run a hundred times faster than the worker, and a
     password check that takes a fifth of a second really took "a minute".
     Each tick of this timer spends a little real time, so the clock keeps
     closer pace with the work. It stops when the test does. */
  var pacer = setInterval(function () {
    var x = 0;
    for (var i = 0; i < 300000; i++) { x = (x + i) % 9973; }
    window.__pace = x;
  }, 1);

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^(Reading|Opening)/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function shown(id) { return document.getElementById(id).style.display !== "none"; }
  function disabled(id) { return document.getElementById(id).disabled; }
  function makePdf(bodies) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : "<<\/Length " + b.data.length + ">>\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R>>\nstartxref\n" + x + "\n%%EOF\n";
    return bytesOfText(out);
  }
  /* pages: [{ box: "0 0 200 100", extra: " /Rotate 90", draw: "1 0 0 rg ..." }], objects 1 and 2 are
     the catalog and the page tree, then each page and its drawing */
  function pagesPdf(pages) {
    var kids = pages.map(function (p, i) { return (3 + i * 2) + " 0 R"; }).join(" ");
    var bodies = ["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [" + kids + "] /Count " + pages.length + ">>"];
    pages.forEach(function (p, i) {
      bodies.push("<<\/Type /Page /Parent 2 0 R /MediaBox [" + p.box + "]" + (p.extra || "") + " /Contents " + (4 + i * 2) + " 0 R>>");
      bodies.push({ data: p.draw || "" });
    });
    return makePdf(bodies);
  }
  function readZip(bytes) {
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), pos = 0, files = [];
    var decoder = new TextDecoder();
    while (pos + 30 <= bytes.length && view.getUint32(pos, true) === 0x04034B50) {
      var method = view.getUint16(pos + 8, true), size = view.getUint32(pos + 18, true);
      var nameLen = view.getUint16(pos + 26, true), extra = view.getUint16(pos + 28, true);
      var name = decoder.decode(bytes.subarray(pos + 30, pos + 30 + nameLen));
      var start = pos + 30 + nameLen + extra;
      if (method !== 0) { throw new Error("an entry is compressed"); }
      files.push({ name: name, bytes: bytes.subarray(start, start + size) });
      pos = start + size;
    }
    if (view.getUint32(pos, true) !== 0x02014B50) { throw new Error("the central directory is not where it should be"); }
    return files;
  }
  /* a picture, decoded by the browser: its size and the colour at a point */
  function picture(bytes, type) {
    return realBitmap(new Blob([bytes], { type: type })).then(function (bmp) {
      var c = new OffscreenCanvas(bmp.width, bmp.height), ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(bmp, 0, 0);
      var data = ctx.getImageData(0, 0, bmp.width, bmp.height).data;
      return {
        w: bmp.width, h: bmp.height,
        at: function (x, y) { var i = (Math.floor(y) * bmp.width + Math.floor(x)) * 4; return [data[i], data[i + 1], data[i + 2], data[i + 3]]; },
        dark: function () { var n = 0; for (var i = 0; i < data.length; i += 4) { if (data[i] + data[i + 1] + data[i + 2] < 200) { n += 1; } } return n; }
      };
    });
  }
  function near(px, want, slack) {
    for (var i = 0; i < want.length; i++) { if (Math.abs(px[i] - want[i]) > slack) { return false; } }
    return true;
  }
  function magic(bytes) { return Array.prototype.slice.call(bytes.subarray(0, 4)).join(","); }
  var PNG_MAGIC = "137,80,78,71", JPEG_MAGIC = "255,216,255";

  function download() {
    var before = window.__saved.length;
    click("dlBtn");
    return wait("the pictures are made", function () {
      return window.__saved.length === before + 1 || /^A page could not/.test(txt("msg"));
    }, 120000).then(function () {
      var saved = window.__saved[before];
      if (!saved) { return null; }
      return saved.blob.arrayBuffer().then(function (b) { return { name: saved.name, type: saved.blob.type, bytes: new Uint8Array(b) }; });
    });
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }
  var history = [];
  new MutationObserver(function () { history.push(txt("msg")); }).observe(document.getElementById("msg"), { childList: true, characterData: true, subtree: true });
  /* every worker the reader starts, and every one it closes */
  var workers = [], closed = 0, RealWorker = window.Worker;
  window.Worker = function (url, options) {
    var w = new RealWorker(url, options), terminate = w.terminate;
    workers.push(String(url));
    w.terminate = function () { closed += 1; return terminate.call(w); };
    return w;
  };
  window.Worker.prototype = RealWorker.prototype;

  /* ================= the files ================= */
  var RED = "1 0 0 rg 0 0 200 100 re f", GREEN = "0 1 0 rg 0 0 200 100 re f", BLUE = "0 0 1 rg 0 0 200 100 re f";
  var COLOURS = pagesPdf([{ box: "0 0 200 100", draw: RED }, { box: "0 0 200 100", draw: GREEN }, { box: "0 0 200 100", draw: BLUE }]);
  /* the left half red, the right half blue: once turned a quarter clockwise, red is on top */
  var HALVES = "1 0 0 rg 0 0 100 100 re f 0 0 1 rg 100 0 100 100 re f";
  var SHAPES = pagesPdf([{ box: "0 0 200 100", extra: " /Rotate 90", draw: HALVES },
                         { box: "0 0 200 100", extra: " /CropBox [100 0 200 100]", draw: HALVES },
                         { box: "0 0 200 100", draw: "" },
                         { box: "0 0 14000 100", draw: "1 0 0 rg 0 0 14000 100 re f" },
                         { box: "0 0 7000 7000", draw: "0 0 1 rg 0 0 7000 7000 re f" }]);
  var many = [];
  for (var k = 0; k < 400; k++) { many.push({ box: "0 0 200 100", draw: k === 4 ? GREEN : RED }); }
  var MANY = pagesPdf(many);
  var oneMB = new Blob([new Uint8Array(1024 * 1024)]);
  var parts = [];
  for (var m = 0; m < 301; m++) { parts.push(oneMB); }
  var F = {
    colours: new File([COLOURS], "doc.pdf", { type: "application/pdf" }),
    shapes: new File([SHAPES], "Shapes.PDF", { type: "application/pdf" }),
    many: new File([MANY], "big.pdf", { type: "application/pdf" }),
    rc4: new File([fromB64(RC4_PDF)], "statement.pdf", { type: "application/pdf" }),
    aes: new File([fromB64(AES_PDF)], "locked.pdf", { type: "application/pdf" }),
    fonts: new File([fromB64(FONTS_PDF)], "fonts.pdf", { type: "application/pdf" }),
    jpx: new File([fromB64(JPX_PDF)], "jpx.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    empty: new File([makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [] /Count 0>>"])], "empty.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf at all")], "notes.pdf", { type: "application/pdf" }),
    huge: new File(parts, "huge.pdf", { type: "application/pdf" }),
    hostile: new File([COLOURS], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF to turn its pages into pictures.");
  ok("with nothing to convert", disabled("dlBtn"));
  eq("all pages, PNG and 150 DPI to start with", val("which") + "|" + val("format") + "|" + val("dpi"), "all|png|150");
  ok("the page numbers and the password are hidden", !shown("pagesField") && !shown("pwField"));
  set("which", "some");
  ok("some pages shows the page numbers", shown("pagesField"));
  eq("and with no PDF there is nothing to say about them", txt("msg"), "Choose a PDF to turn its pages into pictures.");
  set("which", "all");
  ok("all pages hides them again", !shown("pagesField"));
  eq("the PDF reader is not started before a PDF is chosen", workers.length + "|" + document.querySelectorAll("script[src*=pdfjs]").length, "0|0");

  /* ================= three coloured pages ================= */
  step(function () {
    feed(F.colours);
    return wait("the PDF is read", read, 60000);
  });
  step(function () {
    eq("the PDF is described", txt("msg"), "doc.pdf: 3 pages. 3 pages will become PNG pictures in a ZIP file.");
    eq("as good news", document.getElementById("msg").className, "msg msg--ok");
    ok("and can be converted", !disabled("dlBtn"));
    var here = location.href.replace(/tools\/[^\/]*$/, "");
    eq("the reader was started from this site, now that it is needed", workers.join(), here + "js/lib/pdfjs/pdf.worker.min.mjs");
    return wait("the first page is shown", function () { return !!document.querySelector("#preview canvas"); }, 60000);
  });
  step(function () {
    var c = document.querySelector("#preview canvas");
    eq("the preview is 260 pixels wide, in proportion", c.width + "x" + c.height + " " + c.style.width, "260x130 260px");
    eq("styled and described for screen readers", c.className + "|" + c.getAttribute("role") + "|" + c.getAttribute("aria-label"),
       "pdf-preview|img|The first page of doc.pdf");
    var p = c.getContext("2d").getImageData(130, 65, 1, 1).data;
    eq("and it is the first page: red", [p[0], p[1], p[2]].join(), "255,0,0");
    set("format", "jpeg");
    eq("the message follows the format, named as the menu names it", txt("msg"), "doc.pdf: 3 pages. 3 pages will become JPG pictures in a ZIP file.");
    set("format", "png");
    set("which", "some");
    eq("some pages, with none typed, asks for one", txt("msg"), "Type at least one page number.");
    eq("as a problem", document.getElementById("msg").className, "msg msg--bad");
    ok("with nothing to convert", disabled("dlBtn"));
    set("pages", "5");
    eq("a page past the end is refused, quoting it", txt("msg"), String.fromCharCode(0x201C) + "5" + String.fromCharCode(0x201D) + ": this PDF has only 3 pages.");
    set("pages", "2");
    eq("one page is one picture", txt("msg"), "doc.pdf: 3 pages. 1 page will become one PNG picture.");
    ok("and can be converted", !disabled("dlBtn"));
    return download();
  });
  step(function (d) {
    ok("one picture is saved", d, txt("msg"));
    if (!d) { return; }
    eq("named for the PDF and the page, padded to two digits", d.name, "doc-page-02.png");
    eq("as a PNG", d.type + "|" + magic(d.bytes), "image/png|" + PNG_MAGIC);
    ok("the message says what was made", /^Done: 1 page as PNG at 150 DPI, [\d.]+ (B|KB)\. Nothing has left your device\.$/.test(txt("msg")), txt("msg"));
    ok("and that the files are ready while it is done", !disabled("dlBtn"));
    ok("it said which page it was working on", history.indexOf("Turning page 2 into a picture" + String.fromCharCode(0x2026) + " (1 of 1)") >= 0, history.join(" / "));
    return picture(d.bytes, "image/png");
  });
  step(function (pic) {
    if (!pic) { return; }
    eq("150 DPI: 200 by 100 points is 416 by 208 pixels", pic.w + "x" + pic.h, "416x208");
    eq("and it is page 2: green, solid", pic.at(208, 104).join() + " " + pic.at(0, 0).join() + " " + pic.at(415, 207).join(),
       "0,255,0,255 0,255,0,255 0,255,0,255");
    set("pages", "3, 1-2, 2");
    set("format", "jpeg");
    set("dpi", "72");
    eq("pages typed twice count once", txt("msg"), "doc.pdf: 3 pages. 3 pages will become JPG pictures in a ZIP file.");
    return download();
  });
  step(function (d) {
    ok("a ZIP is saved", d, txt("msg"));
    if (!d) { return; }
    eq("named for the PDF", d.name, "doc-pages.zip");
    ok("the message gives its size", /^Done: 3 pages as JPG at 72 DPI, [\d.]+ (B|KB)\. Nothing has left your device\.$/.test(txt("msg")), txt("msg"));
    var files;
    try { files = readZip(d.bytes); } catch (e) { ok("the ZIP reads back", false, e.message); return; }
    eq("three pictures, in the order typed", files.map(function (f) { return f.name; }).join(), "doc-page-03.jpg,doc-page-01.jpg,doc-page-02.jpg");
    ok("all JPEG", files.every(function (f) { return magic(f.bytes).indexOf(JPEG_MAGIC) === 0; }));
    return Promise.all(files.map(function (f) { return picture(f.bytes, "image/jpeg"); }));
  });
  step(function (pics) {
    if (!pics) { return; }
    eq("72 DPI: one pixel a point", pics.map(function (p) { return p.w + "x" + p.h; }).join(), "200x100,200x100,200x100");
    ok("page 3 is blue", near(pics[0].at(100, 50), [0, 0, 255], 8), pics[0].at(100, 50).join());
    ok("page 1 is red", near(pics[1].at(100, 50), [255, 0, 0], 8), pics[1].at(100, 50).join());
    ok("page 2 is green", near(pics[2].at(100, 50), [0, 255, 0], 8), pics[2].at(100, 50).join());
    set("which", "all");
    set("format", "png");
    set("dpi", "300");
    return download();
  });
  step(function (d) {
    var files = d ? readZip(d.bytes) : [];
    eq("all pages, numbered in order", files.map(function (f) { return f.name; }).join(), "doc-page-01.png,doc-page-02.png,doc-page-03.png");
    return Promise.all(files.map(function (f) { return picture(f.bytes, "image/png"); }));
  });
  step(function (pics) {
    eq("300 DPI: 833 by 416 pixels", pics.map(function (p) { return p.w + "x" + p.h; }).join(), "833x416,833x416,833x416");
    eq("red, green, blue", pics.map(function (p) { return p.at(400, 200).slice(0, 3).join(" "); }).join(", "), "255 0 0, 0 255 0, 0 0 255");
  });

  /* ================= turned, cropped, empty and very large pages ================= */
  step(function () {
    set("which", "some");
    set("pages", "1-3");
    set("dpi", "72");
    feed(F.shapes);
    return wait("the shapes PDF is read", read, 60000);
  });
  step(function () {
    eq("choices are kept for the next PDF", txt("msg"), "Shapes.PDF: 5 pages. 3 pages will become PNG pictures in a ZIP file.");
    return wait("its first page is shown", function () { return !!document.querySelector("#preview canvas"); }, 60000);
  });
  step(function () {
    var c = document.querySelector("#preview canvas");
    eq("the preview of a turned page is upright, no taller than an A4 one", c.width + "x" + c.height + " " + c.style.width, "184x368 184px");
    return download();
  });
  step(function (d) {
    var files = d ? readZip(d.bytes) : [];
    eq("the .PDF ending is dropped whatever its case", d && d.name, "Shapes-pages.zip");
    eq("three pictures", files.map(function (f) { return f.name; }).join(), "Shapes-page-01.png,Shapes-page-02.png,Shapes-page-03.png");
    return Promise.all(files.map(function (f) { return picture(f.bytes, "image/png"); }));
  });
  step(function (pics) {
    if (pics.length !== 3) { return; }
    eq("a page turned a quarter is saved upright: 100 wide, 200 high", pics[0].w + "x" + pics[0].h, "100x200");
    eq("its left half now on top", pics[0].at(50, 50).join() + " / " + pics[0].at(50, 150).join(), "255,0,0,255 / 0,0,255,255");
    eq("a cropped page is saved as cropped", pics[1].w + "x" + pics[1].h + " " + pics[1].at(5, 50).join() + " " + pics[1].at(95, 50).join(),
       "100x100 0,0,255,255 0,0,255,255");
    eq("an empty page is white, not see-through", pics[2].at(100, 50).join() + " " + pics[2].at(0, 0).join(), "255,255,255,255 255,255,255,255");
    set("pages", "4");
    return download();
  });
  step(function (d) {
    eq("a very long page is saved", d && d.name, "Shapes-page-04.png");
    eq("and the visitor told it was drawn smaller", txt("msg").replace(/[\d.]+ (B|KB|MB)\./, "N."),
       "Done: 1 page as PNG at 72 DPI, N. One page was too big for that resolution and drawn smaller. Nothing has left your device.");
    return d ? picture(d.bytes, "image/png") : null;
  });
  step(function (pic) {
    if (!pic) { return; }
    ok("no side past 10,000 pixels", pic.w >= 9998 && pic.w <= 10000 && pic.h === 71, pic.w + "x" + pic.h);
    ok("the whole page is in it", near(pic.at(pic.w - 2, 35), [255, 0, 0, 255], 0) && near(pic.at(1, 35), [255, 0, 0, 255], 0),
       pic.at(pic.w - 2, 35).join() + " " + pic.at(1, 35).join());
    set("pages", "5");
    set("format", "jpeg");
    return download();
  });
  step(function (d) {
    eq("a very large page is saved", d && d.name, "Shapes-page-05.jpg");
    has("drawn smaller too", txt("msg"), "One page was too big for that resolution and drawn smaller.");
    return d ? picture(d.bytes, "image/jpeg") : null;
  });
  step(function (pic) {
    if (!pic) { return; }
    ok("held to about 40 million pixels", pic.w === pic.h && pic.w * pic.h <= 40000000 && pic.w > 6300, pic.w + "x" + pic.h);
    ok("blue to the corner", near(pic.at(pic.w - 3, pic.h - 3), [0, 0, 255], 8), pic.at(pic.w - 3, pic.h - 3).join());
    set("pages", "3-");
    set("format", "png");
    eq("open-ended ranges run to the last page", txt("msg"), "Shapes.PDF: 5 pages. 3 pages will become PNG pictures in a ZIP file.");
  });

  /* ================= many pages ================= */
  step(function () {
    set("which", "all");
    feed(F.many);
    return wait("the long PDF is read", read, 60000);
  });
  step(function () {
    eq("more than 300 pages at once are refused", txt("msg"), "Choose up to 300 pages at a time; this would be 400.");
    ok("with nothing to convert", disabled("dlBtn"));
    set("which", "some");
    set("pages", "1-300");
    eq("300 are fine", txt("msg"), "big.pdf: 400 pages. 300 pages will become PNG pictures in a ZIP file.");
    set("pages", "1-301");
    eq("301 are not", txt("msg"), "Choose up to 300 pages at a time; this would be 301.");
    set("pages", "5");
    return download();
  });
  step(function (d) {
    eq("numbers are padded to the longest page number", d && d.name, "big-page-005.png");
    return d ? picture(d.bytes, "image/png") : null;
  });
  step(function (pic) {
    eq("and it is page 5, the green one", pic ? pic.at(100, 50).join() : "none", "0,255,0,255");
  });

  /* ================= what pdf.js fetches as it needs it =================
     Helvetica, not in the file (the standard fonts); Japanese text in a font
     not in the file (the character maps); a JPEG 2000 picture (the picture
     decoder). Each comes from js/lib/pdfjs, fetched by the reader's worker. */
  step(function () {
    set("which", "all");
    set("format", "png");
    set("dpi", "150");
    feed(F.fonts);
    return wait("the fonts PDF is read", read, 60000);
  });
  step(function () { return download(); });
  step(function (d) {
    var files = d ? readZip(d.bytes) : [];
    eq("three pages of text", files.map(function (f) { return f.name; }).join(), "fonts-page-01.png,fonts-page-02.png,fonts-page-03.png");
    return Promise.all(files.map(function (f) { return picture(f.bytes, "image/png"); }));
  });
  step(function (pics) {
    if (pics.length !== 3) { return; }
    ok("Helvetica is drawn", pics[0].dark() > 2500, pics[0].dark());
    /* six characters of 36 points from x = 10 points: one in each cell */
    var cells = [];
    for (var k = 0; k < 6; k++) {
      var n = 0;
      for (var x = Math.ceil((10 + k * 36) * 150 / 72); x < Math.floor((10 + (k + 1) * 36) * 150 / 72); x++) {
        for (var y = 0; y < pics[1].h; y++) { var p = pics[1].at(x, y); if (p[0] + p[1] + p[2] < 200) { n += 1; } }
      }
      cells.push(n);
    }
    ok("the Japanese text is drawn, one character in each place", cells.every(function (n) { return n > 150; }), cells.join());
    /* four check marks in ZapfDingbats: four runs of inked columns */
    var runs = 0, inked = false;
    for (var cx = 0; cx < pics[2].w; cx++) {
      var any = false;
      for (var cy = 0; cy < pics[2].h && !any; cy++) { var q = pics[2].at(cx, cy); any = q[0] + q[1] + q[2] < 200; }
      if (any && !inked) { runs += 1; }
      inked = any;
    }
    eq("ZapfDingbats is drawn: four check marks", runs, 4);
    set("dpi", "72");
    feed(F.jpx);
    return wait("the JPEG 2000 PDF is read", read, 60000);
  });
  step(function () { return download(); });
  step(function (d) {
    eq("a page with a JPEG 2000 picture is saved", d && d.name, "jpx-page-01.png");
    return d ? picture(d.bytes, "image/png") : null;
  });
  step(function (pic) {
    if (!pic) { return; }
    eq("a letter-size page at 72 DPI", pic.w + "x" + pic.h, "612x792");
    ok("its picture is decoded: red on the left, blue on the right, white round it",
       near(pic.at(150, 392), [200, 30, 30], 12) && near(pic.at(450, 392), [30, 30, 200], 12) && near(pic.at(50, 50), [255, 255, 255], 0),
       pic.at(150, 392).join() + " / " + pic.at(450, 392).join() + " / " + pic.at(50, 50).join());
  });

  /* ================= passwords ================= */
  step(function () {
    set("which", "all");
    set("dpi", "150");
    feed(F.rc4);
    return wait("the RC4 PDF is looked at", read, 60000);
  });
  step(function () {
    eq("a PDF with a password asks for it", txt("msg"), "statement.pdf is protected with a password. Type it to open the PDF; it stays in this page.");
    ok("the password box is shown", shown("pwField"));
    ok("with nothing to convert yet", disabled("dlBtn"));
    click("pwBtn");
    eq("an empty password is not tried", txt("msg"), "statement.pdf is protected with a password. Type it to open the PDF; it stays in this page.");
    set("pw", "open sesam");
    click("pwBtn");
    return wait("the wrong password is tried", read, 60000);
  });
  step(function () {
    eq("a wrong password is refused", txt("msg"), "That password is not right. Try again.");
    eq("as a problem", document.getElementById("msg").className, "msg msg--bad");
    ok("the box stays, to try again", shown("pwField"));
    set("pw", "open sesame");
    var input = document.getElementById("pw");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    return wait("the right password is tried", read, 60000);
  });
  step(function () {
    eq("Enter opens it with the right one", txt("msg"), "statement.pdf: 1 page. 1 page will become one PNG picture.");
    ok("the password box is hidden and emptied", !shown("pwField") && val("pw") === "");
    return download();
  });
  step(function (d) {
    eq("the page of the protected PDF is saved", d && d.name, "statement-page-01.png");
    return d ? picture(d.bytes, "image/png") : null;
  });
  step(function (pic) {
    if (!pic) { return; }
    eq("200 by 80 points at 150 DPI", pic.w + "x" + pic.h, "416x166");
    ok("its words are drawn: dark pixels on white", pic.dark() > 500 && near(pic.at(2, 2), [255, 255, 255, 255], 0), pic.dark());
    feed(F.aes);
    return wait("the AES-256 PDF is looked at", read, 60000);
  });
  step(function () {
    eq("AES-256 asks too", txt("msg"), "locked.pdf is protected with a password. Type it to open the PDF; it stays in this page.");
    set("pw", "Open sesame");
    click("pwBtn");
    return wait("the wrong password is tried", read, 60000);
  });
  step(function () {
    eq("passwords are matched exactly, capitals too", txt("msg"), "That password is not right. Try again.");
    set("pw", "open sesame");
    click("pwBtn");
    return wait("the right password is tried", read, 60000);
  });
  step(function () {
    eq("the right one opens it", txt("msg"), "locked.pdf: 1 page. 1 page will become one PNG picture.");
    return download();
  });
  step(function (d) { return d ? picture(d.bytes, "image/png") : null; });
  step(function (pic) {
    ok("its drawing comes out: green round the edge, white in the middle",
       pic && near(pic.at(5, 5), [0, 128, 0, 255], 1) && near(pic.at(208, 104), [255, 255, 255, 255], 0),
       pic && pic.at(5, 5).join() + " / " + pic.at(208, 104).join());
    feed(F.rc4);
    return wait("the RC4 PDF is looked at again", read, 60000);
  });
  step(function () {
    set("pw", "something");
    click("resetBtn");
    ok("clear hides the password box and empties it", !shown("pwField") && val("pw") === "");
    eq("and asks for a PDF again", txt("msg"), "Choose a PDF to turn its pages into pictures.");
  });

  /* ================= files that cannot be used ================= */
  step(function () {
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 60000);
  });
  step(function () {
    eq("one that is not a PDF is turned away", txt("msg"), "Could not use notes.pdf: not a PDF, or too damaged to read.");
    eq("as a problem", document.getElementById("msg").className, "msg msg--bad");
    ok("with nothing to convert", disabled("dlBtn"));
    eq("and no preview", document.getElementById("preview").children.length, 0);
    feed(F.empty);
    return wait("a PDF without pages is looked at", read, 60000);
  });
  step(function () {
    eq("one without pages too", txt("msg"), "Could not use empty.pdf: it has no pages.");
    ok("with nothing to convert", disabled("dlBtn"));
    feed(F.huge);
    eq("one over 300 MB is turned away before it is read", txt("msg"), "Could not use huge.pdf: over the 300 MB this page can read.");
    feed(F.damaged);
    return wait("a PDF with a damaged index is read", read, 60000);
  });
  step(function () {
    ok("a damaged index is worked around", /^scan\.pdf: \d+ pages?\. /.test(txt("msg")), txt("msg"));
    ok("and it can be converted", !disabled("dlBtn"));
    feed(F.hostile);
    return wait("a file named as markup is read", read, 60000);
  });
  step(function () {
    eq("its name is shown as text", txt("msg"), "<iframe onload=zq>.pdf: 3 pages. 3 pages will become PNG pictures in a ZIP file.");
    return wait("its first page is shown", function () { return !!document.querySelector("#preview canvas"); }, 60000);
  });
  step(function () {
    eq("the name made no element", document.querySelectorAll("iframe").length, 0);
    eq("the preview's label holds it as text", document.querySelector("#preview canvas").getAttribute("aria-label"), "The first page of <iframe onload=zq>.pdf");
  });

  /* ================= stopping half-way ================= */
  step(function () {
    feed(F.colours);
    return wait("the PDF is read again", read, 60000);
  });
  step(function () {
    set("dpi", "300");
    var before = window.__saved.length;
    history.length = 0;
    click("dlBtn");
    ok("the button is off while it works", disabled("dlBtn"));
    click("resetBtn");
    eq("clear stops it at once", txt("msg"), "Choose a PDF to turn its pages into pictures.");
    eq("and puts every choice back", val("which") + "|" + val("pages") + "|" + val("format") + "|" + val("dpi"), "all||png|150");
    ok("with the page numbers hidden and no preview", !shown("pagesField") && document.getElementById("preview").children.length === 0);
    /* another PDF, converted to the end: by then the stopped work has had
       every chance to save something, and must not have */
    feed(F.shapes);
    return wait("another PDF is read", read, 60000).then(function () {
      set("which", "some");
      set("pages", "3");
      set("dpi", "72");
      return download();
    }).then(function (d) {
      eq("only the new picture was saved", window.__saved.slice(before).map(function (s) { return s.name; }).join(), "Shapes-page-03.png");
      ok("the stopped work said nothing after it was stopped",
         !history.some(function (h) { return /^Done: 3 pages|^A page could not|\([23] of 3\)/.test(h); }), history.join(" / "));
    });
  });
  step(function () {
    click("resetBtn");
    ok("clear leaves nothing to convert", disabled("dlBtn") && txt("msg") === "Choose a PDF to turn its pages into pictures.");
    return wait("every reader started has been closed", function () { return closed === workers.length; }, 30000);
  });
  step(function () {
    ok("one for each PDF and each try at a password, and none left open", workers.length >= 15 && closed === workers.length, closed + " of " + workers.length);
  });

  chain.then(function () { clearInterval(pacer); finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    clearInterval(pacer);
    finish();
  });
"""
# ---- END: the test for pdf-to-image ----

# ---- START: the test for currency-converter ----
T["currency-converter"] = r"""  /* ================= independent arithmetic and formatting =================
     Numbers are written here with toFixed and hand-made grouping, not with
     Intl as the page does, so a formatting slip on the page shows. */
  function group(digits, indian) {
    if (!indian || digits.length <= 3) { return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
    var head = digits.slice(0, -3), tail = digits.slice(-3);
    return head.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + tail;
  }
  function fixed(x, d, indian) {
    var s = x.toFixed(d), p = s.split(".");
    return group(p[0], indian) + (p[1] ? "." + p[1] : "");
  }
  function trimmed(s) { return s.indexOf(".") < 0 ? s : s.replace(/0+$/, "").replace(/\.$/, ""); }
  function sig(x, n) { return trimmed(Number(x.toPrecision(n)).toString()); }
  function isIndian(code) { return code === "INR" || code === "NPR"; }
  function money(x, code) {
    if (x === 0) { return "0"; }
    return Math.abs(x) >= 1 ? fixed(x, 2, isIndian(code)) : sig(x, 4);
  }
  function rateText(x, code) {
    if (x >= 1) { var p = trimmed(x.toFixed(4)).split("."); return group(p[0], isIndian(code)) + (p[1] ? "." + p[1] : ""); }
    return sig(x, 5);
  }
  var PEG = { AED: ["USD", 3.6725], SAR: ["USD", 3.75], QAR: ["USD", 3.64], OMR: ["USD", 0.3845], BHD: ["USD", 0.376], NPR: ["INR", 1.6] };
  function perEuro(data, code) {
    if (code === "EUR") { return 1; }
    return data.rates[code] || data.rates[PEG[code][0]] * PEG[code][1];
  }
  function expect(data, amount, from, to) { return amount / perEuro(data, from) * perEuro(data, to); }
  function iso(ms) { return new Date(ms).toISOString().slice(0, 10); }
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function spoken(isoDate) { var p = isoDate.split("-"); return Number(p[2]) + " " + MONTHS[Number(p[1]) - 1] + " " + p[0]; }
  var DOT = String.fromCharCode(0x00B7), DASH = String.fromCharCode(0x2014);
  function shown(id) { return txt(id); }
  function rows() {
    return Array.prototype.map.call(document.querySelectorAll("#others tr"), function (tr) {
      return tr.children[0].textContent + "=" + tr.children[1].textContent;
    });
  }

  /* ================= the real file, data/rates.js ================= */
  var REAL = window.EXCHANGE_RATES;
  ok("data/rates.js loaded and set the rates", REAL && typeof REAL === "object");
  ok("with a date", REAL && /^\d{4}-\d{2}-\d{2}$/.test(REAL.date), REAL && REAL.date);
  eq("on the euro", REAL && REAL.base, "EUR");
  var realCodes = REAL ? Object.keys(REAL.rates) : [];
  ok("twenty or more currencies, the dollar and the rupee among them",
     realCodes.length >= 20 && REAL.rates.USD > 0 && REAL.rates.INR > 0, realCodes.join());
  ok("every rate a positive number under a three-letter code", realCodes.every(function (c) {
    return /^[A-Z]{3}$/.test(c) && typeof REAL.rates[c] === "number" && REAL.rates[c] > 0;
  }));
  if (REAL) {
    eq("the page opens on 1 US dollar in rupees, from the real rates", shown("result"), money(expect(REAL, 1, "USD", "INR"), "INR") + " INR");
    eq("and says which day's rates they are", shown("dateLine"),
       "Rates of " + spoken(REAL.date) + ": the euro reference rates of the European Central Bank, published around 4 pm Central European Time on working days.");
  }

  /* ================= from here on, fixed rates dated today ================= */
  var R = {
    date: iso(Date.now()), base: "EUR",
    rates: { AUD: 1.6176, BRL: 5.8610, CAD: 1.5984, CHF: 0.9279, CNY: 7.5259, CZK: 24.470, DKK: 7.4736, GBP: 0.85033,
             HKD: 8.8084, HUF: 369.18, IDR: 20149.32, ILS: 3.4408, INR: 108.1245, ISK: 137.00, JPY: 176.99, KRW: 1513.44,
             MXN: 20.5806, MYR: 4.5849, NOK: 10.8315, NZD: 2.0002, PHP: 70.265, PLN: 4.3775, RON: 5.3488, SEK: 11.2900,
             SGD: 1.4366, THB: 37.710, TRY: 55.1650, USD: 1.1225, ZAR: 18.7839 }
  };
  function use(data) { window.EXCHANGE_RATES = data; set("amount", val("amount")); }
  use(R);

  /* ================= the lists ================= */
  var fromGroups = document.querySelectorAll("#from optgroup");
  eq("two groups in each list", fromGroups.length + "," + document.querySelectorAll("#to optgroup").length, "2,2");
  eq("the most used first", fromGroups[0].label + ": " + Array.prototype.map.call(fromGroups[0].children, function (o) { return o.value; }).join(" "),
     "Most used: INR USD EUR GBP AED SAR SGD AUD CAD JPY");
  var rest = Array.prototype.map.call(fromGroups[1].children, function (o) { return o.textContent; });
  eq("then the other 26, by name", fromGroups[1].label + ": " + rest.length, "All currencies: 26");
  var names = rest.map(function (t) { return t.split(DASH + " ")[1]; });
  ok("in alphabetical order", names.join("|") === names.slice().sort().join("|"), names.join("|"));
  var allValues = Array.prototype.map.call(document.querySelectorAll("#from option"), function (o) { return o.value; });
  eq("36 currencies, none twice", allValues.length + "," + allValues.filter(function (v, i) { return allValues.indexOf(v) === i; }).length, "36,36");
  ok("each written as code and name", document.querySelector('#from option[value="INR"]').textContent === "INR " + DASH + " Indian rupee" &&
     document.querySelector('#from option[value="AED"]').textContent === "AED " + DASH + " UAE dirham");

  /* ================= converting ================= */
  set("amount", "1");
  var usdInr = expect(R, 1, "USD", "INR");
  eq("1 US dollar in rupees", shown("result"), money(usdInr, "INR") + " INR");
  eq("in words below it", shown("resultLabel"), "1.00 USD (US dollar) = " + money(usdInr, "INR") + " INR (Indian rupee)");
  eq("both ways round, to four decimals or five figures", shown("rateLine"),
     "1 USD = " + rateText(usdInr, "INR") + " INR  " + DOT + "  1 INR = " + rateText(1 / usdInr, "USD") + " USD");
  eq("worked out through the euro: 108.1245 / 1.1225", rateText(usdInr, "INR"), "96.3247");
  eq("no warning on today's rates", shown("msg") + "|" + document.getElementById("msg").className, "|msg");
  eq("no fixed-rate note for these two", shown("pegLine"), "");
  eq("the same amount in the other most-used currencies", rows().join("; "),
     ["EUR", "GBP", "AED", "SAR", "SGD", "AUD", "CAD", "JPY"].map(function (c) {
       return c + " " + DASH + " " + document.querySelector('#from option[value="' + c + '"]').textContent.split(DASH + " ")[1] + "=" + money(expect(R, 1, "USD", c), c);
     }).join("; "));

  set("amount", "1,00,000");
  eq("commas in the amount are fine, and rupees are grouped the Indian way", shown("result"), money(expect(R, 100000, "USD", "INR"), "INR") + " INR");
  set("amount", "200000");
  eq("so nearly two crore rupees reads as 1,92,64,944.32", shown("result"), "1,92,64,944.32 INR");
  set("from", "INR"); set("to", "USD"); set("amount", "100000");
  eq("other currencies are grouped in thousands", shown("result"), money(expect(R, 100000, "INR", "USD"), "USD") + " USD");
  eq("which is 1,038.16 (100000 x 1.1225 / 108.1245 = 1038.1555)", shown("result"), "1,038.16 USD");
  set("from", "USD"); set("to", "EUR"); set("amount", "1000000");
  eq("a million dollars in euros, in thousands: 890,868.60", shown("result"), "890,868.60 EUR");
  set("from", "INR"); set("to", "USD");
  set("amount", "1");
  eq("an amount under one keeps four figures", shown("result"), sig(expect(R, 1, "INR", "USD"), 4) + " USD");
  eq("which is 0.01038", shown("result"), "0.01038 USD");
  set("amount", "0");
  eq("nothing converts to nothing", shown("result"), "0 USD");
  /* Dropping every comma read the European "1.000,50" as 1.0005 and "1,5"
     as 15. A comma is a thousands separator here, so the last one must be
     followed by exactly three digits. */
  var DASH = String.fromCharCode(0x2014);
  ["1.000,50", "1.234,567", "1,5", "1,50", "1,0000", "12,34.5"].forEach(function (typed) {
    set("amount", typed);
    eq(typed + " is refused rather than guessed", shown("result"), DASH);
    has(typed + ": and the page asks for a decimal dot", txt("msg"), "Write decimals with a dot");
  });
  set("amount", "12,345.67");
  ok("12,345.67 is fine", shown("result") !== DASH, shown("result"));
  set("amount", "1,00,000.50");
  ok("and so is the Indian 1,00,000.50", shown("result") !== DASH, shown("result"));
  set("amount", "-5");
  eq("a minus amount is refused", shown("result"), DASH);
  has("with the right reason", txt("msg"), "cannot be below zero");
  set("amount", " 2 500.50 ");
  eq("spaces in the amount are fine", shown("result"), money(expect(R, 2500.5, "INR", "USD"), "USD") + " USD");
  set("amount", ".5");
  eq("so is a leading point", shown("result"), money(expect(R, 0.5, "INR", "USD"), "USD") + " USD");

  set("from", "EUR"); set("to", "INR"); set("amount", "1");
  eq("1 euro is the published rate itself", shown("result"), "108.12 INR");
  eq("and its rate line shows it in full", shown("rateLine").split("  ")[0], "1 EUR = 108.1245 INR");
  set("from", "INR"); set("to", "INR");
  eq("a currency into itself", shown("result"), "1.00 INR");
  ok("and neither appears in the table", rows().every(function (r) { return r.indexOf("INR") !== 0; }) && rows().length === 9, rows().join("; "));

  /* ================= currencies fixed to another ================= */
  set("from", "AED"); set("to", "INR");
  eq("dirhams to rupees, through the dollar", shown("result"), money(expect(R, 1, "AED", "INR"), "INR") + " INR");
  eq("which is 108.1245 / (1.1225 x 3.6725)", shown("result"), "26.23 INR");
  eq("with a note saying why", shown("pegLine"), "AED is fixed at 3.6725 to the US dollar, so its rate follows the dollar's.");
  set("from", "NPR");
  eq("Nepalese rupees: 1.6 to the Indian rupee exactly", shown("result"), "0.625 INR");
  eq("its own note", shown("pegLine"), "NPR is fixed at 1.6 to the Indian rupee, so its rate follows the rupee's.");
  set("from", "INR"); set("to", "NPR"); set("amount", "100000");
  eq("Nepalese rupees are grouped the Indian way too", shown("result"), "1,60,000.00 NPR");
  set("amount", "1");
  set("from", "AED"); set("to", "SAR");
  eq("two fixed currencies, two notes", shown("pegLine"),
     "AED is fixed at 3.6725 to the US dollar, so its rate follows the dollar's. SAR is fixed at 3.75 to the US dollar, so its rate follows the dollar's.");
  eq("and their rate is the ratio of the two pegs", shown("rateLine").split("  ")[0], "1 AED = " + rateText(3.75 / 3.6725, "SAR") + " SAR");
  set("from", "OMR"); set("to", "USD");
  eq("the Omani rial: 0.3845 to the dollar", shown("result"), money(1 / 0.3845, "USD") + " USD");
  set("from", "BHD"); set("to", "QAR"); set("amount", "10");
  eq("Bahraini dinars to Qatari riyals", shown("result"), money(10 / 0.376 * 3.64, "QAR") + " QAR");

  /* ================= swap and reset ================= */
  set("from", "GBP"); set("to", "JPY"); set("amount", "250");
  click("swapBtn");
  eq("swap turns the pair round", val("from") + ">" + val("to"), "JPY>GBP");
  eq("and converts again", shown("result"), money(expect(R, 250, "JPY", "GBP"), "GBP") + " GBP");
  click("resetBtn");
  eq("reset goes back to 1 US dollar in rupees", val("amount") + " " + val("from") + ">" + val("to") + " " + shown("result"),
     "1 USD>INR " + money(usdInr, "INR") + " INR");

  /* ================= amounts it refuses ================= */
  [["", "Type an amount."], ["abc", "Type the amount as a number, such as 1500 or 99.50."],
   ["-5", "An amount to convert cannot be below zero."], ["1.2.3", "Type the amount as a number, such as 1500 or 99.50."],
   ["1e5", "Type the amount as a number, such as 1500 or 99.50."], ["2000000000000000", "That amount is too large to convert."]
  ].forEach(function (c) {
    set("amount", c[0]);
    eq("refused: \"" + c[0] + "\"", shown("msg") + "|" + document.getElementById("msg").className, c[1] + "|msg msg--bad");
    eq("  with no result left showing", shown("result") + "|" + shown("rateLine") + "|" + rows().length, DASH + "||0");
  });
  set("amount", "<img src=x onerror=zq>");
  eq("markup typed as an amount is refused as text", document.querySelectorAll("#msg *, #result *, #others img").length, 0);
  set("amount", "1");
  eq("a good amount clears the message", shown("msg"), "");

  /* ================= rates that are old, missing or broken ================= */
  var MONTH_AGO = JSON.parse(JSON.stringify(R));
  MONTH_AGO.date = iso(Date.now() - 30 * 86400000);
  use(MONTH_AGO);
  eq("rates a month old are converted, with a warning", shown("msg"),
     "These rates are 30 days old: the page has not been updated since. Check a current rate before you rely on it.");
  eq("as a problem", document.getElementById("msg").className, "msg msg--bad");
  eq("and the date says so too", shown("dateLine").slice(0, 9 + spoken(MONTH_AGO.date).length), "Rates of " + spoken(MONTH_AGO.date));
  var FOUR = JSON.parse(JSON.stringify(R));
  FOUR.date = iso(Date.now() - 4 * 86400000);
  use(FOUR);
  eq("four days old is a long weekend, not a warning", shown("msg"), "");
  FOUR.date = iso(Date.now() - 5 * 86400000);
  use(FOUR);
  has("five days is", shown("msg"), "These rates are 5 days old");

  use(undefined);
  eq("no rates at all: said plainly", shown("msg"), "The exchange rates could not be loaded. Check the connection and reload the page.");
  eq("with nothing converted and no date", shown("result") + "|" + shown("dateLine") + "|" + rows().length, DASH + "||0");
  var BROKEN = JSON.parse(JSON.stringify(R));
  BROKEN.rates.USD = "1.1225";
  use(BROKEN);
  eq("a dollar rate written as text is not trusted", shown("msg"), "The exchange rates could not be loaded. Check the connection and reload the page.");
  BROKEN = JSON.parse(JSON.stringify(R));
  BROKEN.date = "2 October";
  use(BROKEN);
  eq("nor rates with a date that is not a date", shown("result") + "|" + shown("dateLine"), String.fromCharCode(0x2014) + "|");
  BROKEN = JSON.parse(JSON.stringify(R));
  BROKEN.base = "USD";
  use(BROKEN);
  eq("nor rates on another base", shown("result"), DASH);
  BROKEN = JSON.parse(JSON.stringify(R));
  BROKEN.rates.GBP = -0.85;
  BROKEN.rates["<b>X<\/b>"] = 2;
  use(BROKEN);
  set("from", "GBP");
  eq("a negative rate is left out, and the page says which", shown("msg"), "There is no rate for GBP in these rates.");
  set("from", "USD");
  eq("the other currencies still convert", shown("result"), money(usdInr, "INR") + " INR");
  eq("and a code that is not a code makes nothing", document.querySelectorAll("#others b, #msg b").length, 0);
  BROKEN = JSON.parse(JSON.stringify(R));
  delete BROKEN.rates.ZAR;
  use(BROKEN);
  set("to", "ZAR");
  eq("a currency missing from a day's rates is named", shown("msg"), "There is no rate for ZAR in these rates.");
  use(R);
  eq("and converts again when it is back", shown("result"), money(expect(R, 1, "USD", "ZAR"), "ZAR") + " ZAR");
  window.EXCHANGE_RATES = REAL;
  finish();
"""
# ---- END: the test for currency-converter ----

# ---- START: the test for income-tax-calculator ----
T["income-tax-calculator"] = r"""  /* Every expected figure below was worked out by hand from the official
     slab tables, and agrees with a separate Python implementation. */
  var R = String.fromCharCode(0x20B9), M = String.fromCharCode(0x2212) + " ", DASH = String.fromCharCode(0x2014);
  var KEYS = ["gross", "standard", "deductions", "taxable", "tax", "rebate", "surcharge", "cess", "total", "month", "rate"];
  function column(regime) { return KEYS.map(function (k) { return txt(regime + "-" + k); }).join(" | "); }
  function fill(values) {
    ["salary", "other", "employerNps", "invest", "health", "homeLoan", "nps", "hra", "otherDeductions"].forEach(function (id) {
      set(id, values[id] === undefined ? "" : values[id]);
    });
    set("age", values.age || "below60");
  }

  /* ================= the page as it opens ================= */
  eq("it opens on a salary of 15 lakh", val("salary") + "|" + val("age"), "15,00,000|below60");
  eq("new regime: 14,25,000 taxable, 93,750 tax, 3,750 cess", column("new"),
     [R + "15,00,000", M + R + "75,000", R + "0", R + "14,25,000", R + "93,750", R + "0", R + "0", R + "3,750", R + "97,500", R + "8,125", "6.5%"].join(" | "));
  eq("old regime: 14,50,000 taxable, 2,47,500 tax, 9,900 cess", column("old"),
     [R + "15,00,000", M + R + "50,000", R + "0", R + "14,50,000", R + "2,47,500", R + "0", R + "0", R + "9,900", R + "2,57,400", R + "21,450", "17.2%"].join(" | "));
  eq("the lower tax in large", txt("best"), R + "97,500");
  eq("and which regime gives it", txt("verdict"), "The new regime saves you " + R + "1,59,900 a year.");
  eq("no message", txt("msg"), "");

  /* ================= the 12-lakh rebate ================= */
  fill({ salary: "1275000" });
  eq("12,75,000 of salary: no tax under the new regime", txt("new-taxable") + " " + txt("new-tax") + " " + txt("new-rebate") + " " + txt("new-total"),
     R + "12,00,000 " + R + "60,000 " + M + R + "60,000 " + R + "0");
  eq("1,87,200 under the old", txt("old-total"), R + "1,87,200");
  fill({ salary: "1285000" });
  eq("12,10,000 taxable: the tax is cut to the 10,000 above 12 lakh", txt("new-tax") + " " + txt("new-rebate") + " " + txt("new-cess") + " " + txt("new-total"),
     R + "61,500 " + M + R + "51,500 " + R + "400 " + R + "10,400");
  fill({ other: "1400000" });
  eq("income that is not salary gets no standard deduction", txt("new-standard") + " " + txt("old-standard") + " " + txt("new-taxable"), R + "0 " + R + "0 " + R + "14,00,000");
  fill({ salary: "60000" });
  eq("a salary below the standard deduction only uses up itself", txt("new-standard") + " " + txt("new-taxable"), M + R + "60,000 " + R + "0");

  /* ================= old-regime deductions ================= */
  fill({ salary: "15,00,000", invest: "2,00,000", health: "25000", homeLoan: "300000", nps: "50000", hra: "100000" });
  eq("deductions are capped and counted in the old regime only: 1,50,000 + 25,000 + 2,00,000 + 50,000 + 1,00,000",
     txt("old-deductions") + " " + txt("new-deductions"), M + R + "5,25,000 " + R + "0");
  eq("old: 9,25,000 taxable, 97,500 tax, 1,01,400 to pay", txt("old-taxable") + " " + txt("old-tax") + " " + txt("old-total"),
     R + "9,25,000 " + R + "97,500 " + R + "1,01,400");
  eq("the new regime still wins, by 3,900", txt("verdict"), "The new regime saves you " + R + "3,900 a year.");
  set("employerNps", "100000");
  eq("the employer's NPS counts in both", txt("new-deductions") + " " + txt("old-deductions"), M + R + "1,00,000 " + M + R + "6,25,000");
  eq("now the old regime wins: 80,600 against 81,900", txt("new-total") + " " + txt("old-total") + " " + txt("best"), R + "81,900 " + R + "80,600 " + R + "80,600");
  eq("and says so", txt("verdict"), "The old regime saves you " + R + "1,300 a year.");
  fill({ salary: "1500000", nps: "90000" });
  eq("your own NPS is counted up to 50,000", txt("old-deductions") + " " + txt("old-taxable"), M + R + "50,000 " + R + "14,00,000");
  fill({ salary: "100000", invest: "150000" });
  eq("deductions larger than the income leave nothing taxable, not less than nothing", txt("old-taxable") + " " + txt("old-total"), R + "0 " + R + "0");
  fill({ salary: "15,00,000", invest: "2,00,000", health: "25000", homeLoan: "300000", nps: "50000", hra: "100000", employerNps: "100000" });
  set("otherDeductions", "4,00,000");
  eq("deductions never take taxable income below nothing", txt("old-taxable") + " " + txt("old-total"), R + "4,25,000 " + R + "0");

  /* ================= age, in the old regime ================= */
  fill({ other: "1000000", age: "from80" });
  eq("at 80, five lakh is exempt: 1,00,000 + cess", txt("old-tax") + " " + txt("old-total"), R + "1,00,000 " + R + "1,04,000");
  eq("the new regime ignores age: 40,000, all rebated", txt("new-tax") + " " + txt("new-total"), R + "40,000 " + R + "0");
  set("age", "from60");
  eq("at 60, three lakh is exempt: 10,000 + 1,00,000", txt("old-tax"), R + "1,10,000");
  set("age", "below60");
  eq("below 60, two and a half lakh: 12,500 + 1,00,000", txt("old-tax"), R + "1,12,500");
  fill({ other: "500000", age: "from60" });
  eq("the old regime's rebate: five lakh pays nothing", txt("old-tax") + " " + txt("old-rebate") + " " + txt("old-total"), R + "10,000 " + M + R + "10,000 " + R + "0");
  set("other", "500010");
  eq("ten rupees more and the rebate is gone, with no relief: 10,002 + cess", txt("old-tax") + " " + txt("old-rebate") + " " + txt("old-total"), R + "10,002 " + R + "0 " + R + "10,400");

  /* ================= surcharge ================= */
  fill({ other: "5010000" });
  eq("50,10,000 under the old regime: surcharge cut from 1,31,550 to 7,000", txt("old-tax") + " " + txt("old-surcharge") + " " + txt("old-total"),
     R + "13,15,500 " + R + "7,000 " + R + "13,75,400");
  eq("and under the new: 10,83,000 tax, surcharge 7,000", txt("new-tax") + " " + txt("new-surcharge") + " " + txt("new-total"),
     R + "10,83,000 " + R + "7,000 " + R + "11,33,600");
  fill({ other: "10010000" });
  eq("just over 1 crore: relief from the 10% the step already carries", txt("old-surcharge") + " " + txt("new-surcharge"), R + "2,88,250 " + R + "2,65,000");
  fill({ other: "20010000" });
  eq("just over 2 crore: relief from 15%", txt("old-surcharge") + " " + txt("new-surcharge"), R + "8,78,875 " + R + "8,44,000");
  fill({ other: "50010000" });
  eq("just over 5 crore, old regime: relief from 25%", txt("old-surcharge"), R + "37,10,125");
  fill({ other: "60000000" });
  eq("6 crore: 37% under the old regime, 25% under the new", txt("old-surcharge") + " " + txt("new-surcharge"),
     R + "65,90,625 " + R + "43,95,000");
  fill({ other: "1" });
  eq("one rupee: rounded to nothing", txt("new-taxable") + " " + txt("new-total"), R + "0 " + R + "0");
  fill({ other: "1234565" });
  eq("taxable income is rounded to the nearest ten: 12,34,565 becomes 12,34,570", txt("new-taxable"), R + "12,34,570");
  fill({});
  eq("no income at all: no tax either way", txt("best") + " | " + txt("verdict") + " | " + txt("new-rate"), R + "0 | Both regimes come to the same tax. | 0.0%");

  /* ================= amounts it refuses ================= */
  [["salary", "abc", "Type the salary or pension as a number of rupees, such as 150000."],
   ["salary", "-500000", "Type the salary or pension as a number of rupees, such as 150000."],
   ["invest", "1e5", "Type the investments as a number of rupees, such as 150000."],
   ["hra", "12.5.0", "Type the HRA exemption as a number of rupees, such as 150000."],
   ["other", "2000000000000", "The other income is too large for this calculator."]].forEach(function (c) {
    fill({ salary: "1500000" });
    set(c[0], c[1]);
    eq("refused: " + c[0] + " \"" + c[1] + "\"", txt("msg") + "|" + document.getElementById("msg").className, c[2] + "|msg msg--bad");
    eq("  with nothing worked out", txt("best") + "|" + txt("verdict") + "|" + txt("new-total") + "|" + txt("old-rate"), DASH + "||" + DASH + "|" + DASH);
  });
  fill({ salary: "<img src=x onerror=zq>" });
  eq("markup typed as an amount is refused as text", document.querySelectorAll("#msg *, table.data img").length, 0);
  fill({ salary: " 15 00 000 " });
  eq("spaces in an amount are fine", txt("new-total") + " " + txt("msg"), R + "97,500 ");

  /* ================= reset ================= */
  fill({ salary: "2500000", invest: "150000", age: "from80" });
  click("resetBtn");
  eq("reset brings back 15 lakh and nothing else", val("salary") + "|" + val("invest") + "|" + val("age") + "|" + txt("best"),
     "15,00,000||below60|" + R + "97,500");
  finish();
"""
# ---- END: the test for income-tax-calculator ----

# ---- START: the test for compress-pdf ----
T["compress-pdf"] = r"""
  var TINY = {"objstm": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNDk+PgpzdHJlYW0KeJxFiTEKACAMxHInCC7+z/8/wkkr7WKWEGKSCBvokqv5DmBwLWDTqhcnvyY83nIHgAplbmRzdHJlYW0KZW5kb2JqCnN0YXJ0eHJlZgozNjUKJSVFT0YK", "hybrid": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjYgMCBvYmoKPDwvVHlwZSAvT2JqU3RtIC9OIDMgL0ZpcnN0IDE2IC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9MZW5ndGggMTU5Pj4Kc3RyZWFtCnicVY3NCsIwEITvfYp5g036Ix7CHiqIIIJUb6WH2C5SkESaVPTtJe1BvOyw8+3s5FAosClQQZclMmPo+nkK6GzvEkDHcQhoCyg0HWjnZxehQScZRlv7N1oFBV0pbFXH/B9PcxIXkac4qJHg56mXAGNo711cVKNKmJk5FbgoLgaUq/d7uNzTZb7FZU2mBtU2yEoO8nhJHHvLnH0BxgM8CgplbmRzdHJlYW0KZW5kb2JqCjcgMCBvYmoKPDwvVHlwZSAvWFJlZiAvU2l6ZSA4IC9XIFsxIDQgMl0gL0xlbmd0aCA1Nj4+CnN0cmVhbQoAAAAAAAAAAAAAAAAAAAIAAAAGAAACAAAABgABAAAAAAAAAAIAAAAGAAIAAAAAAAAAAAAAAAAAAAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA4CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA2MiAwMDAwMCBuIAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAxNDIgMDAwMDAgbiAKMDAwMDAwMDQwMCAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgOCAvUm9vdCAxIDAgUiAvWFJlZlN0bSA0MDA+PgpzdGFydHhyZWYKNTM1CiUlRU9GCg==", "bad_offsets": "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDIwIDAwMDAwIG4gCjAwMDAwMDAwNjcgMDAwMDAgbiAKMDAwMDAwMDE0NSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMjEgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "incremental": "JVBERi0xLjQKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTYgMDAwMDAgbiAKMDAwMDAwMDEzNCAwMDAwMCBuIAowMDAwMDAwMjMwIDAwMDAwIG4gCjAwMDAwMDAzMTAgMDAwMDAgbiAKdHJhaWxlcgo8PC9TaXplIDYgL1Jvb3QgMSAwIFI+PgpzdGFydHhyZWYKMzc4CiUlRU9GCjMgMCBvYmoKPDwvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9SZXNvdXJjZXMgPDwvRm9udCA8PC9GMSA1IDAgUj4+Pj4gL0NvbnRlbnRzIDQgMCBSIC9Sb3RhdGUgOTA+PgplbmRvYmoKeHJlZgowIDEKMDAwMDAwMDAwMCA2NTUzNSBmIAozIDEKMDAwMDAwMDU1OSAwMDAwMCBuIAp0cmFpbGVyCjw8L1NpemUgNiAvUm9vdCAxIDAgUiAvUHJldiAzNzg+PgpzdGFydHhyZWYKNjY2CiUlRU9GCg==", "xref_notype": "JVBERi0xLjUKJeLjz9MKMSAwIG9iago8PC9UeXBlIC9DYXRhbG9nIC9QYWdlcyAyIDAgUj4+CmVuZG9iagoyIDAgb2JqCjw8L1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgL01lZGlhQm94IFswIDAgMTUwIDgwXT4+CmVuZG9iagozIDAgb2JqCjw8L1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvUmVzb3VyY2VzIDw8L0ZvbnQgPDwvRjEgNSAwIFI+Pj4+IC9Db250ZW50cyA0IDAgUj4+CmVuZG9iago0IDAgb2JqCjw8L0xlbmd0aCAzMj4+CnN0cmVhbQpCVCAvRjEgMjQgVGYgMjAgMzAgVGQgKFQxKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPDwvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2E+PgplbmRvYmoKNiAwIG9iago8PC9UeXBlIC9YUmVmIC9TaXplIDcgL1cgWzAgNCAyXSAvSW5kZXggWzEgNl0gL1Jvb3QgMSAwIFIgL0xlbmd0aCAzNj4+CnN0cmVhbQoAAAAPAAAAAAA+AAAAAACMAAAAAADsAAAAAAE8AAAAAAGAAAAKZW5kc3RyZWFtCmVuZG9iagpzdGFydHhyZWYKMzg0CiUlRU9GCg==", "objstm_badindex": "JVBERi0xLjUKJeLjz9MKNCAwIG9iago8PC9MZW5ndGggMzI+PgpzdHJlYW0KQlQgL0YxIDI0IFRmIDIwIDMwIFRkIChUMSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago2IDAgb2JqCjw8L1R5cGUgL09ialN0bSAvTiA0IC9GaXJzdCAyMSAvRmlsdGVyIC9GbGF0ZURlY29kZSAvTGVuZ3RoIDE3MT4+CnN0cmVhbQp4nF2OzQqDMBCE7z7FvMEmWvsDIQeFUiiFYnsTD6ldRJCkmFjaty+aQ8HLLjvf7sxKCKTIUmQ45Mghd1skStH9+2JQaYIZXAe6mo49UghUWv95lOncPz3qbKYNqHSTDZCgCz97U7gPagEBmQvsRbM+n+vINkRzUMXeTWPLHkrR0dmwdIk8Zms9B9jANnhs1v8s+3SbHmEZZ1GCCuM5khMPbw59a7ROfqL4RhYKZW5kc3RyZWFtCmVuZG9iago3IDAgb2JqCjw8L1R5cGUgL1hSZWYgL1NpemUgOCAvVyBbMSA0IDJdIC9Sb290IDEgMCBSIC9GaWx0ZXIgL0ZsYXRlRGVjb2RlIC9EZWNvZGVQYXJtcyA8PC9QcmVkaWN0b3IgMTIgL0NvbHVtbnMgNz4+IC9MZW5ndGggNTE+PgpzdHJlYW0KeJw9ybENgDAQxdB3v0BKw37sP0Qqcigg4cayHJvuBEdVfPxuDCuFKW9f7v3r5AHeFwd9CmVuZHN0cmVhbQplbmRvYmoKc3RhcnR4cmVmCjM2NQolJUVPRgo="};
  window.__saved = [];

  /* Decoding and encoding the photos happens off the page's own thread,
     which the test's virtual clock does not wait for: with nothing to do but
     wait, the page let the clock run about a hundred times faster than that
     work, and on a busy machine a 120-second wait ran out while a picture
     was still being encoded. Each tick of this timer spends a little real
     time, so the clock keeps closer pace with the work. It stops when the
     test does. (The same fix as pdf-to-image's.) */
  var pacer = setInterval(function () {
    var x = 0;
    for (var i = 0; i < 300000; i++) { x = (x + i) % 9973; }
    window.__pace = x;
  }, 1);
  window.downloadBlob = function (blob, name) { window.__saved.push({ blob: blob, name: name }); };

  /* the page's slow work (deflate, decoding and encoding pictures) is
     counted while it runs: when none is running at a timer tick, the page
     has finished, or stopped */
  var inflight = 0;
  function counted(promise) { inflight += 1; return promise.finally(function () { inflight -= 1; }); }
  var realArrayBuffer = Response.prototype.arrayBuffer;
  Response.prototype.arrayBuffer = function () { return counted(realArrayBuffer.call(this)); };
  var realBitmap = window.createImageBitmap;
  window.createImageBitmap = function () { return counted(realBitmap.apply(window, arguments)); };
  var realToBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = function (cb, type, q) {
    inflight += 1;
    return realToBlob.call(this, function (b) { inflight -= 1; cb(b); }, type, q);
  };

  /* ================= helpers ================= */
  function feed(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = document.getElementById("file");
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function wait(label, test, budget) { return new Promise(function (resolve) { waitFor(label, test, resolve, budget); }); }
  function read() { return !/^Reading/.test(txt("msg")); }
  function bytesOfText(s) { var u = new Uint8Array(s.length); for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; } return u; }
  function fromB64(s) { return bytesOfText(atob(s)); }
  function latin1(bytes) {
    var parts = [];
    for (var i = 0; i < bytes.length; i += 32768) { parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 32768))); }
    return parts.join("");
  }
  function same(a, b) { if (!a || !b || a.length !== b.length) { return false; } for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) { return false; } } return true; }
  function num(dict, key) { var m = new RegExp("/" + key + " (\\d+)").exec(dict); return m ? Number(m[1]) : null; }
  function makePdf(bodies, trailerExtra) {
    var out = "%PDF-1.4\n", offsets = [];
    bodies.forEach(function (b, i) {
      offsets.push(out.length);
      out += (i + 1) + " 0 obj\n" + (typeof b === "string" ? b : b.dict.replace("LEN", b.data.length) + "\nstream\n" + b.data + "\nendstream") + "\nendobj\n";
    });
    var x = out.length;
    out += "xref\n0 " + (bodies.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (o) { out += ("0000000000" + o).slice(-10) + " 00000 n \n"; });
    out += "trailer\n<<\/Size " + (bodies.length + 1) + " /Root 1 0 R" + (trailerExtra || "") + ">>\nstartxref\n" + x + "\n%%EOF\n";
    return out;
  }
  function inflate(u8) {
    return new Response(new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer()
      .then(function (b) { return new Uint8Array(b); });
  }

  /* ================= a reader for the saved file =================
     The packed layout the page writes: a cross-reference stream at the end,
     small objects in object streams, streams on their own. Unpacked with
     the browser's DecompressionStream, not the page's own inflater. */
  function readPacked(bytes) {
    var t = latin1(bytes);
    var sx = /startxref\n(\d+)\n%%EOF\n$/.exec(t);
    if (!sx) { return Promise.reject(new Error("no startxref at the end")); }
    var at = Number(sx[1]);
    var head = /^(\d+) 0 obj\n(<< \/Type \/XRef [^\n]*>>)\nstream\n/.exec(t.slice(at));
    if (!head) { return Promise.reject(new Error("startxref does not point at a cross-reference stream")); }
    var xdict = head[2], size = num(xdict, "Size"), len = num(xdict, "Length");
    var out = { text: t, xdict: xdict, objs: {}, root: num(xdict, "Root"), info: num(xdict, "Info"), packedCount: 0 };
    if (!/\/W \[1 4 2\]/.test(xdict)) { return Promise.reject(new Error("unexpected /W: " + xdict)); }
    return inflate(bytes.subarray(at + head[0].length, at + head[0].length + len)).then(function (rows) {
      if (rows.length !== size * 7) { throw new Error("the table has " + rows.length + " bytes for " + size + " objects"); }
      var inStreams = {};
      for (var n = 1; n < size; n++) {
        var type = rows[n * 7], a = ((rows[n * 7 + 1] << 24) | (rows[n * 7 + 2] << 16) | (rows[n * 7 + 3] << 8) | rows[n * 7 + 4]) >>> 0;
        var b = (rows[n * 7 + 5] << 8) | rows[n * 7 + 6];
        if (type === 1) {
          var tag = n + " 0 obj\n";
          if (t.substr(a, tag.length) !== tag) { throw new Error("object " + n + " is not where the table says"); }
          var end = t.indexOf("\nendobj\n", a), body = t.slice(a + tag.length, end), s = body.indexOf("\nstream\n");
          if (s < 0) { out.objs[n] = { dict: body, data: null }; }
          else {
            var l = num(body.slice(0, s), "Length"), start = a + tag.length + s + 8;
            if (t.substr(start + l, 10) !== "\nendstream") { throw new Error("object " + n + ": /Length does not end at endstream"); }
            out.objs[n] = { dict: body.slice(0, s), data: bytes.subarray(start, start + l) };
          }
        } else if (type === 2) {
          (inStreams[a] = inStreams[a] || []).push([n, b]);
          out.packedCount += 1;
        } else { throw new Error("object " + n + " has type " + type); }
      }
      var jobs = Object.keys(inStreams).map(function (stm) {
        var o = out.objs[stm];
        if (!o || !/\/Type \/ObjStm/.test(o.dict)) { throw new Error("object " + stm + " is not an object stream"); }
        var first = num(o.dict, "First"), count = num(o.dict, "N");
        return inflate(o.data).then(function (plain) {
          var text = latin1(plain), pairs = text.slice(0, first).trim().split(/\s+/).map(Number);
          if (pairs.length !== count * 2) { throw new Error("object stream " + stm + " lists " + pairs.length / 2 + " objects, says " + count); }
          inStreams[stm].forEach(function (want) {
            var idx = want[1];
            if (pairs[idx * 2] !== want[0]) { throw new Error("object " + want[0] + " is not at index " + idx + " of " + stm); }
            var from = first + pairs[idx * 2 + 1], to = idx + 1 < count ? first + pairs[idx * 2 + 3] : text.length;
            /* strict: an object starts exactly where its stream says, not on the space before it */
            if (/\s/.test(text.charAt(from))) { throw new Error("object " + want[0] + " does not start where object stream " + stm + " says"); }
            out.objs[want[0]] = { dict: text.slice(from, to).trim(), data: null, packed: true };
          });
        });
      });
      return Promise.all(jobs).then(function () { return out; });
    });
  }
  function streamText(o) { return /\/FlateDecode/.test(o.dict) ? inflate(o.data).then(latin1) : Promise.resolve(latin1(o.data)); }
  function imagesOf(r) {
    return Object.keys(r.objs).map(function (n) { return r.objs[n]; }).filter(function (o) { return /\/Subtype \/Image/.test(o.dict); });
  }

  /* ================= the files ================= */
  function canvasJpeg(w, h, quality, paint) {
    var c = new OffscreenCanvas(w, h), ctx = c.getContext("2d");
    paint(ctx, w, h);
    return c.convertToBlob({ type: "image/jpeg", quality: quality }).then(function (b) { return b.arrayBuffer(); }).then(function (b) { return new Uint8Array(b); });
  }
  /* a photograph's smooth colour with fine noise; the top-left corner red, the bottom-right blue */
  function photoPaint(ctx, w, h) {
    var g = ctx.createLinearGradient(0, 0, w, h);
    g.addColorStop(0, "#d02020"); g.addColorStop(1, "#2030d0");
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    var img = ctx.getImageData(0, 0, w, h), seed = 7;
    for (var i = 0; i < img.data.length; i += 4) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      var n = (seed >> 16) % 25 - 12;
      img.data[i] += n; img.data[i + 1] += n; img.data[i + 2] += n;
    }
    ctx.putImageData(img, 0, 0);
  }
  /* an EXIF block saying "turn 90" (orientation 6), put right after the start marker */
  function withExif(jpeg) {
    var seg = [0x45, 0x78, 0x69, 0x66, 0, 0, 0x4D, 0x4D, 0, 0x2A, 0, 0, 0, 8, 0, 1, 0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, 6, 0, 0, 0, 0, 0, 0];
    var out = new Uint8Array(jpeg.length + 4 + seg.length);
    out.set([0xFF, 0xD8, 0xFF, 0xE1, 0, seg.length + 2], 0);
    out.set(seg, 6);
    out.set(jpeg.subarray(2), 6 + seg.length);
    return out;
  }
  /* a zlib stream of "stored" blocks: valid deflate, not packed at all */
  function storedZlib(bytes) {
    var out = new Uint8Array(2 + 5 + bytes.length + 4), a = 1, b = 0;
    out[0] = 0x78; out[1] = 0x01;
    out[2] = 0x01;
    out[3] = bytes.length & 255; out[4] = bytes.length >> 8; out[5] = ~bytes.length & 255; out[6] = (~bytes.length >> 8) & 255;
    out.set(bytes, 7);
    for (var i = 0; i < bytes.length; i++) { a = (a + bytes[i]) % 65521; b = (b + a) % 65521; }
    var at = 7 + bytes.length;
    out[at] = b >> 8; out[at + 1] = b & 255; out[at + 2] = a >> 8; out[at + 3] = a & 255;
    return out;
  }
  function imagePdf(images, extraPages, pre) {
    /* images: [{ data (bytes), w, h, cs, extra }]; one page each, plus text pages. pre: bodies that
       come first, as objects 4, 5, ...; image k is then object 4 + pre.length + 3k */
    var bodies = ["CATALOG", "PAGES", "<<\/Type /Font /Subtype /Type1 /BaseFont /Helvetica>>"].concat(pre || []), kids = [];
    images.forEach(function (im) {
      bodies.push({ dict: "<<\/Type /XObject /Subtype /Image /Width " + im.w + " /Height " + im.h + " /ColorSpace " + im.cs +
                          " /BitsPerComponent 8 " + (im.filter || "/Filter /DCTDecode") + (im.extra || "") + " /Length LEN>>", data: latin1(im.data) });
      var imNum = bodies.length;
      var draw = "q 300 0 0 200 50 50 cm /Im Do Q";
      bodies.push({ dict: "<<\/Length LEN>>", data: draw });
      bodies.push("<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents " + (bodies.length) + " 0 R /Resources <<\/XObject <<\/Im " + imNum + " 0 R>>>>>>");
      kids.push(bodies.length);
    });
    (extraPages || []).forEach(function (text) {
      bodies.push({ dict: "<<\/Length LEN>>", data: text });
      bodies.push("<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents " + (bodies.length) + " 0 R /Resources <<\/Font <<\/F1 3 0 R>>>>>>");
      kids.push(bodies.length);
    });
    bodies[0] = "<<\/Type /Catalog /Pages 2 0 R>>";
    bodies[1] = "<<\/Type /Pages /Kids [" + kids.map(function (k) { return k + " 0 R"; }).join(" ") + "] /Count " + kids.length + ">>";
    bodies.push("<<\/Title (Pictures) /Producer (compress test)>>");
    return bytesOfText(makePdf(bodies, " /Info " + bodies.length + " 0 R"));
  }

  /* text only, with an old revision inside and drawings not packed */
  var LINES = [];
  for (var li = 0; li < 60; li++) { LINES.push("BT /F1 10 Tf 20 " + (780 - li * 12) + " Td (Line " + li + " of a long report, written out plainly) Tj ET"); }
  var DRAWING = LINES.join("\n");
  /* also: an XMP block (must stay readable), 1000 bytes that do not pack
     (must stay as they are) and a stream packed with deflate's "stored"
     blocks, which is no packing at all (must be packed properly) */
  var XMP = "<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc:title>Report, readable by programs that do not unpack PDF streams<\/dc:title><\/x:xmpmeta>";
  var NOISE = "", seed = 99;
  for (var ni = 0; ni < 1000; ni++) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; NOISE += String.fromCharCode((seed >> 16) & 255); }
  var STORED_TEXT = DRAWING + "\n" + DRAWING + "\n" + DRAWING;
  var TEXT_FIRST = makePdf([
    "<<\/Type /Catalog /Pages 2 0 R /Outlines 6 0 R /Metadata 9 0 R /Noise 10 0 R /Stored 11 0 R>>",
    "<<\/Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 800]>>",
    "<<\/Type /Page /Parent 2 0 R /Contents 4 0 R /Resources <<\/Font <<\/F1 5 0 R>>>>>>",
    { dict: "<<\/Length LEN>>", data: "BT /F1 10 Tf 20 20 Td (An old draft of the page that was replaced) Tj ET" },
    "<<\/Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    "<<\/Type /Outlines /Count 0>>",
    "<<\/Title (Report) /Author (Asha Rao)>>",
    "<<\/Unused (nothing points at this object)>>",
    { dict: "<<\/Type /Metadata /Subtype /XML /Length LEN>>", data: XMP },
    { dict: "<<\/Length LEN>>", data: NOISE },
    { dict: "<<\/Filter /FlateDecode /Length LEN>>", data: latin1(storedZlib(bytesOfText(STORED_TEXT))) }
  ], " /Info 7 0 R");
  var x1 = /startxref\n(\d+)\n%%EOF\n$/.exec(TEXT_FIRST)[1];
  var upd = "4 0 obj\n<<\/Length " + DRAWING.length + ">>\nstream\n" + DRAWING + "\nendstream\nendobj\n";
  var TEXT = bytesOfText(TEXT_FIRST + upd + "xref\n4 1\n" + ("0000000000" + TEXT_FIRST.length).slice(-10) + " 00000 n \ntrailer\n<<\/Size 12 /Root 1 0 R /Info 7 0 R" +
                         " /Prev " + x1 + ">>\nstartxref\n" + (TEXT_FIRST.length + upd.length) + "\n%%EOF\n");

  var DOC_ENC = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R] /Count 1>>",
                         "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>", "<<\/Filter /Standard /V 2 /R 3>>"], " /Encrypt 4 0 R"));
  var DOC_EMPTY = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [] /Count 0>>"]));
  var DOC_SIGNED = bytesOfText(makePdf(["<<\/Type /Catalog /Pages 2 0 R /AcroForm <<\/Fields [] /SigFlags 3>>>>",
                         "<<\/Type /Pages /Kids [3 0 R] /Count 1>>", "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 10 10]>>"]));
  var F = {
    text: new File([TEXT], "report.pdf", { type: "application/pdf" }),
    damaged: new File([fromB64(TINY.bad_offsets)], "scan.pdf", { type: "application/pdf" }),
    enc: new File([DOC_ENC], "locked.pdf", { type: "application/pdf" }),
    empty: new File([DOC_EMPTY], "empty.pdf", { type: "application/pdf" }),
    signed: new File([DOC_SIGNED], "signed.pdf", { type: "application/pdf" }),
    notPdf: new File([bytesOfText("not a pdf")], "notes.pdf", { type: "application/pdf" }),
    hostile: new File([TEXT], "<iframe onload=zq>.pdf", { type: "application/pdf" })
  };

  function download(level) {
    var before = window.__saved.length;
    if (level) { document.getElementById("level").value = level; document.getElementById("level").dispatchEvent(new Event("change", { bubbles: true })); }
    click("dlBtn");
    return wait("the compressed file is made", function () {
      return window.__saved.length === before + 1 || /^(This PDF is already|The PDF could not)/.test(txt("msg"));
    }, 120000).then(function () {
      var saved = window.__saved[before];
      if (!saved) { return null; }
      return saved.blob.arrayBuffer().then(function (b) {
        var bytes = new Uint8Array(b);
        return readPacked(bytes).then(function (out) { return { saved: saved, bytes: bytes, out: out, error: "" }; },
                                      function (e) { return { saved: saved, bytes: bytes, out: null, error: e.message }; });
      });
    });
  }
  function settled() {
    var quiet = 0;
    return wait("the page's work has stopped", function () { quiet = inflight === 0 ? quiet + 1 : 0; return quiet >= 3; }, 120000);
  }

  var chain = Promise.resolve();
  function step(fn) { chain = chain.then(fn); }

  /* ================= the page as it opens ================= */
  eq("it asks for a PDF", txt("msg"), "Choose a PDF to make smaller.");
  ok("with nothing to compress", document.getElementById("dlBtn").disabled);
  eq("balanced is chosen to start with", val("level"), "balanced");
  has("and explained", txt("levelHint"), "Photos are saved again as JPEG at good quality, and any longer than 2400 pixels made smaller.");
  set("level", "lossless");
  has("each level says what it does: lossless", txt("levelHint"), "Nothing you can see changes.");
  set("level", "strong");
  has("strong", txt("levelHint"), "at most 1600 pixels; screenshots and other colour pictures become JPEG too");
  set("level", "balanced");

  /* ================= lossless, on text ================= */
  var firstOut = null;
  step(function () {
    feed(F.text);
    return wait("the PDF is read", read, 10000);
  });
  step(function () {
    ok("the file is read and described", /^report\.pdf: 1 page, [\d.]+ KB\.$/.test(txt("msg")), txt("msg"));
    ok("and can be compressed", !document.getElementById("dlBtn").disabled);
    return download("lossless");
  });
  step(function (d) {
    ok("the compressed file reads back", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    firstOut = d;
    var r = d.out;
    eq("named for what was done to it", d.saved.name, "report (compressed).pdf");
    eq("as a PDF", d.saved.blob.type, "application/pdf");
    eq("a PDF 1.7 file", r.text.slice(0, 9), "%PDF-1.7\n");
    ok("smaller than the original", d.bytes.length < TEXT.length, d.bytes.length + " vs " + TEXT.length);
    ok("the small objects are packed in object streams", r.packedCount >= 5, r.packedCount);
    eq("the catalog among them", !!(r.objs[r.root] && r.objs[r.root].packed), true);
    has("its bookmarks kept", r.objs[r.root] ? r.objs[r.root].dict : "", "/Outlines");
    ok("the details kept", r.info && /\/Title <5265706F7274>/.test(r.objs[r.info].dict), r.info && r.objs[r.info].dict);
    var everything = Object.keys(r.objs).map(function (n) { return r.objs[n].dict; }).join("\n");
    eq("the unused object is left behind", /Unused|6E6F7468696E6720706F696E7473/.test(everything), false);
    var streams = Object.keys(r.objs).map(function (n) { return r.objs[n]; }).filter(function (o) { return o.data && !/ObjStm|XRef/.test(o.dict); });
    eq("four streams: the drawing, the XMP, the noise, the stored one", streams.length, 4);
    var xmp = streams.filter(function (o) { return /\/Type \/Metadata/.test(o.dict); })[0];
    ok("the XMP is left readable, not packed", xmp && !/Filter/.test(xmp.dict) && latin1(xmp.data) === XMP, xmp && xmp.dict);
    var noise = streams.filter(function (o) { return o.data.length === 1000; })[0];
    ok("bytes that would not get smaller are left as they are", noise && !/Filter/.test(noise.dict) && latin1(noise.data) === NOISE, noise && noise.dict);
    var packed = streams.filter(function (o) { return /\/Filter \/FlateDecode/.test(o.dict); });
    eq("the drawing and the stored one are packed with deflate", packed.length, 2);
    has("the message gives both sizes and the saving", txt("msg"), "% smaller. Pictures were left untouched. Nothing has left your device.");
    ok("in that form", new RegExp("^Done: [\\d.]+ (?:B|KB|MB) " + String.fromCharCode(0x2192) + " [\\d.]+ (?:B|KB|MB), \\d+% smaller\\.").test(txt("msg")), txt("msg"));
    return Promise.all(packed.map(streamText)).then(function (texts) {
      ok("the stored one is really packed now", packed.some(function (o, i) { return texts[i] === STORED_TEXT && o.data.length < STORED_TEXT.length / 5; }),
         packed.map(function (o) { return o.data.length; }).join());
      ok("the drawing comes back exactly, the newest one: the old draft is left behind", texts.indexOf(DRAWING) >= 0);
      /* the result, compressed again, is no smaller: nothing is saved */
      feed(new File([d.bytes], "again.pdf", { type: "application/pdf" }));
      return wait("the compressed file is read", read, 10000);
    });
  });
  step(function () {
    return download("lossless");
  });
  step(function (d) {
    eq("a file that cannot get smaller is not saved", d, null);
    eq("and the visitor is told, with what else to try", txt("msg"),
       "This PDF is already as small as this page can make it at this level: the result was not smaller, so nothing was saved. Strong may do better.");
    return download("strong");
  });
  step(function (d) {
    eq("at strong, nothing more to suggest", txt("msg"),
       "This PDF is already as small as this page can make it: the result was not smaller, so nothing was saved.");
  });

  /* ================= balanced, on photos ================= */
  var PH = {};
  /* objects 4 and 5 are colour profiles (RGB, grey); picture k is object 6 + 3k */
  var PROFILES = [{ dict: "<<\/N 3 /Length LEN>>", data: "an RGB colour profile would be here" },
                  { dict: "<<\/N 1 /Length LEN>>", data: "a grey colour profile would be here" }];
  step(function () {
    return Promise.all([
      canvasJpeg(3000, 2000, 0.98, photoPaint),
      canvasJpeg(1200, 800, 0.98, photoPaint),
      canvasJpeg(64, 64, 0.3, photoPaint),
      canvasJpeg(800, 600, 0.98, photoPaint),
      canvasJpeg(800, 600, 0.97, photoPaint),
      canvasJpeg(800, 600, 0.96, photoPaint)
    ]).then(function (j) {
      PH.big = j[0]; PH.turned = withExif(j[1]); PH.tiny = j[2]; PH.other = j[3]; PH.withMask = j[4]; PH.profiled = j[5];
      PH.list = [
        { data: PH.big, w: 3000, h: 2000, cs: "/DeviceRGB" },                                     /* 0: made smaller */
        { data: PH.turned, w: 1200, h: 800, cs: "/DeviceRGB" },                                   /* 1: saved again, not turned */
        { data: PH.tiny, w: 64, h: 64, cs: "/DeviceRGB" },                                        /* 2: would grow: kept */
        { data: PH.other, w: 800, h: 600, cs: "/DeviceGray" },                                    /* 3-6: left alone */
        { data: PH.other, w: 800, h: 600, cs: "/DeviceCMYK", extra: " /Decode [1 0 1 0 1 0 1 0]" },
        { data: PH.other, w: 800, h: 600, cs: "/DeviceRGB", extra: " /Decode [0 1 0 1 0 1]" },
        { data: PH.other, w: 800, h: 600, cs: "[/Indexed /DeviceRGB 1 <000000FFFFFF>]" },
        { data: PH.other, w: 800, h: 600, cs: "/DeviceRGB" },                                     /* 7: used as 8's mask: left alone */
        { data: PH.withMask, w: 800, h: 600, cs: "/DeviceRGB", extra: " /SMask " + (6 + 3 * 7) + " 0 R" },  /* 8: saved again, same size */
        { data: PH.other, w: 800, h: 600, cs: "/DeviceRGB", extra: " /Mask [0 10 0 10 0 10]" },   /* 9: exact colours matter: left alone */
        { data: PH.profiled, w: 800, h: 600, cs: "[/ICCBased 4 0 R]" },                          /* 10: RGB profile: saved again */
        { data: PH.other, w: 800, h: 600, cs: "[/ICCBased 5 0 R]" }                               /* 11: grey profile: left alone */
      ];
      PH.pdf = imagePdf(PH.list, ["BT /F1 12 Tf 20 20 Td (Text only) Tj ET"], PROFILES);
      feed(new File([PH.pdf], "photos.pdf", { type: "application/pdf" }));
      return wait("the photos PDF is read", read, 10000);
    });
  });
  step(function () {
    has("thirteen pages", txt("msg"), "photos.pdf: 13 pages, ");
    return download("lossless");
  });
  step(function (d) {
    ok("lossless compresses it too", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    var imgs = imagesOf(d.out);
    eq("and leaves every picture byte for byte", PH.list.filter(function (im) { return imgs.some(function (o) { return same(o.data, im.data); }); }).length, 12);
    has("saying so", txt("msg"), "% smaller. Pictures were left untouched.");
    return download("balanced");
  });
  step(function (d) {
    ok("the photos PDF is compressed", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    var imgs = imagesOf(d.out);
    eq("every picture is still there", imgs.length, 12);
    var masked = imgs.filter(function (o) { return /\/SMask/.test(o.dict); });
    ok("the one with a mask is saved again, at its own size, its mask kept",
       masked.length === 1 && !same(masked[0].data, PH.withMask) && num(masked[0].dict, "Width") === 800 && /\/SMask \d+ 0 R/.test(masked[0].dict),
       masked.map(function (o) { return o.dict; }).join(" | "));
    var profiled = imgs.filter(function (o) { return /ICCBased/.test(o.dict) && !same(o.data, PH.other); });
    ok("the one with an RGB colour profile is saved again, its profile kept", profiled.length === 1 && !same(profiled[0].data, PH.profiled),
       profiled.map(function (o) { return o.dict; }).join(" | "));
    var big = imgs.filter(function (o) { return num(o.dict, "Width") === 2400; });
    eq("the big photo is made 2400 pixels long, its shape kept", big.length === 1 ? num(big[0].dict, "Height") : "none", 1600);
    has("as JPEG", big[0] ? big[0].dict : "", "/Filter /DCTDecode");
    ok("and much smaller", big[0] && big[0].data.length < PH.big.length / 2, big[0] && big[0].data.length);
    var turned = imgs.filter(function (o) { return num(o.dict, "Width") === 1200; });
    eq("the photo with an EXIF turn keeps its stored shape, not turned", turned.length === 1 ? num(turned[0].dict, "Height") : "none", 800);
    ok("and was saved again", turned[0] && !same(turned[0].data, PH.turned));
    eq("a picture that would not get smaller is kept byte for byte", imgs.filter(function (o) { return same(o.data, PH.tiny); }).length, 1);
    eq("grey, CMYK, a /Decode array, an indexed one, a mask, exact colours and a grey profile are left alone",
       imgs.filter(function (o) { return same(o.data, PH.other); }).length, 7);
    eq("the message counts them", txt("msg").replace(/^Done: .*? smaller\./, ""),
       " 4 of 5 pictures saved again; the rest were already smaller as they were. Nothing has left your device.");
    return Promise.all([realBitmap(new Blob([big[0].data], { type: "image/jpeg" })), realBitmap(new Blob([turned[0].data], { type: "image/jpeg" }))]);
  });
  step(function (bitmaps) {
    if (!bitmaps) { return; }
    var c = new OffscreenCanvas(10, 10), ctx = c.getContext("2d", { willReadFrequently: true });
    function corner(bmp, x, y) { ctx.clearRect(0, 0, 10, 10); ctx.drawImage(bmp, x, y, 10, 10, 0, 0, 10, 10); var p = ctx.getImageData(5, 5, 1, 1).data; return [p[0], p[1], p[2]]; }
    var tl = corner(bitmaps[0], 0, 0), br = corner(bitmaps[0], 2390, 1590);
    ok("its colours are kept: red at the top left", tl[0] > 150 && tl[2] < 110, tl.join());
    ok("blue at the bottom right", br[2] > 150 && br[0] < 110, br.join());
    eq("the decoded photo is the size the PDF says", bitmaps[0].width + "x" + bitmaps[0].height, "2400x1600");
    var t = corner(bitmaps[1], 0, 0);
    ok("the turned one still has red at its stored top left: no turn was applied", t[0] > 150 && t[2] < 110, t.join());
    eq("and its stored size", bitmaps[1].width + "x" + bitmaps[1].height, "1200x800");
    /* one picture, which would only grow */
    feed(new File([imagePdf([{ data: PH.tiny, w: 64, h: 64, cs: "/DeviceRGB" }], ["BT /F1 12 Tf 20 20 Td (" + new Array(40).join("Some words on a page. ") + ") Tj ET"])],
                  "small.pdf", { type: "application/pdf" }));
    return wait("the small PDF is read", read, 10000);
  });
  step(function () {
    return download("balanced");
  });
  step(function (d) {
    ok("a PDF whose one picture would only grow is still compressed", d && d.out, d ? d.error : "no download");
    eq("and says the picture was kept", txt("msg").replace(/^Done: .*? smaller\./, ""),
       " Its one colour picture was already smaller as it was, so it was kept. Nothing has left your device.");
  });

  /* ================= strong, on pictures kept without loss ================= */
  step(function () {
    var w = 2000, h = 1000, rgb = new Uint8Array(w * h * 3), grey = new Uint8Array(400 * 300);
    for (var i = 0, p = 0; i < w * h; i++, p += 3) { rgb[p] = (i % w) * 255 / w; rgb[p + 1] = 90; rgb[p + 2] = Math.floor(i / w) * 255 / h; }
    for (var j = 0; j < grey.length; j++) { grey[j] = j % 251; }
    /* 16 bits a channel, which this page does not touch; noise, so that a
       JPEG of it would be much smaller and a page that took it would show */
    var deep = new Uint8Array(100 * 50 * 6), ds = 5;
    for (var k = 0; k < deep.length; k++) { ds = (ds * 1103515245 + 12345) & 0x7fffffff; deep[k] = (ds >> 16) & 255; }
    /* rows with the PNG "none" predictor in front of each: a 0, then the row */
    var pw = 300, ph = 200, pred = new Uint8Array(ph * (1 + pw * 3));
    for (var y = 0, q = 0; y < ph; y++) {
      pred[q++] = 0;
      for (var x = 0; x < pw; x++) { pred[q++] = x * 255 / pw; pred[q++] = 200; pred[q++] = y * 255 / ph; }
    }
    var pack = function (u8) { return new Response(new Blob([u8]).stream().pipeThrough(new CompressionStream("deflate"))).arrayBuffer().then(function (b) { return new Uint8Array(b); }); };
    return Promise.all([pack(rgb), pack(grey), pack(deep), pack(pred)]).then(function (z) {
      PH.rawRgb = z[0]; PH.rawGrey = z[1]; PH.rawDeep = z[2];
      var masked = makeMasked(z[0], z[1], z[2], z[3]);
      PH.strongPdf = masked;
      feed(new File([masked], "screens.pdf", { type: "application/pdf" }));
      return wait("the screenshots PDF is read", read, 10000);
    });
    function makeMasked(rgbZ, greyZ, deepZ, predZ) {
      var bodies = [
        "<<\/Type /Catalog /Pages 2 0 R>>", "<<\/Type /Pages /Kids [3 0 R 4 0 R 9 0 R 10 0 R] /Count 4>>",
        "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 5 0 R /Resources <<\/XObject <<\/Im 6 0 R>>>>>>",
        "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 5 0 R /Resources <<\/XObject <<\/Im 7 0 R>>>>>>",
        { dict: "<<\/Length LEN>>", data: "q 300 0 0 200 50 50 cm /Im Do Q" },
        { dict: "<<\/Type /XObject /Subtype /Image /Width 2000 /Height 1000 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length LEN>>", data: latin1(rgbZ) },
        { dict: "<<\/Type /XObject /Subtype /Image /Width 2000 /Height 1000 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /SMask 8 0 R /Length LEN>>", data: latin1(rgbZ) },
        { dict: "<<\/Type /XObject /Subtype /Image /Width 400 /Height 300 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length LEN>>", data: latin1(greyZ) },
        "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 5 0 R /Resources <<\/XObject <<\/Im 11 0 R>>>>>>",
        "<<\/Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 5 0 R /Resources <<\/XObject <<\/Im 12 0 R>>>>>>",
        { dict: "<<\/Type /XObject /Subtype /Image /Width 100 /Height 50 /ColorSpace /DeviceRGB /BitsPerComponent 16 /Filter /FlateDecode /Length LEN>>", data: latin1(deepZ) },
        { dict: "<<\/Type /XObject /Subtype /Image /Width 300 /Height 200 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode " +
                "/DecodeParms <<\/Predictor 15 /Colors 3 /Columns 300 /BitsPerComponent 8>> /Length LEN>>", data: latin1(predZ) }
      ];
      return bytesOfText(makePdf(bodies));
    }
  });
  step(function () {
    return download("balanced");
  });
  step(function (d) {
    ok("at balanced, pictures kept without loss stay so", d === null || imagesOf(d.out).every(function (o) { return /FlateDecode/.test(o.dict); }),
       d && imagesOf(d.out).map(function (o) { return o.dict; }).join(" | "));
    return download("strong");
  });
  step(function (d) {
    ok("at strong, they are compressed", d && d.out, d ? d.error : "no download");
    if (!d || !d.out) { return; }
    var imgs = imagesOf(d.out);
    var plain = imgs.filter(function (o) { return /DCTDecode/.test(o.dict) && !/SMask/.test(o.dict) && num(o.dict, "Width") !== 300; });
    var masked = imgs.filter(function (o) { return /DCTDecode/.test(o.dict) && /SMask/.test(o.dict); });
    eq("the plain one becomes JPEG, at most 1600 pixels", plain.length === 1 ? num(plain[0].dict, "Width") + "x" + num(plain[0].dict, "Height") : "none", "1600x800");
    eq("the one with a soft mask becomes JPEG at its own size, to match its mask", masked.length === 1 ? num(masked[0].dict, "Width") + "x" + num(masked[0].dict, "Height") : "none", "2000x1000");
    var mask = imgs.filter(function (o) { return /DeviceGray/.test(o.dict); });
    eq("the mask itself stays grey and lossless", mask.length === 1 ? /FlateDecode/.test(mask[0].dict) + "," + num(mask[0].dict, "Width") : "none", "true,400");
    var deep = imgs.filter(function (o) { return /BitsPerComponent 16/.test(o.dict); });
    ok("16 bits a channel is left alone", deep.length === 1 && /FlateDecode/.test(deep[0].dict) && same(deep[0].data, PH.rawDeep),
       deep.map(function (o) { return o.dict; }).join(" | "));
    var predicted = imgs.filter(function (o) { return num(o.dict, "Width") === 300; });
    ok("one with a row predictor becomes JPEG, the predictor gone with the old packing",
       predicted.length === 1 && /DCTDecode/.test(predicted[0].dict) && !/DecodeParms|Predictor/.test(predicted[0].dict),
       predicted.map(function (o) { return o.dict; }).join(" | "));
    PH.strongPlain = plain[0];
    return mask.length === 1 ? inflate(mask[0].data) : null;
  });
  step(function (maskBytes) {
    var grey = new Uint8Array(400 * 300);
    for (var j = 0; j < grey.length; j++) { grey[j] = j % 251; }
    ok("byte for byte, once unpacked", same(maskBytes, grey));
    return PH.strongPlain ? realBitmap(new Blob([PH.strongPlain.data], { type: "image/jpeg" })) : null;
  });
  step(function (bmp) {
    if (!bmp) { return; }
    /* red grows to the right, blue downwards, green is 90 everywhere */
    var c = new OffscreenCanvas(bmp.width, bmp.height), ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    function at(x, y) { var p = ctx.getImageData(x, y, 1, 1).data; return [p[0], p[1], p[2]]; }
    var tl = at(5, 5), tr = at(bmp.width - 6, 5), bl = at(5, bmp.height - 6);
    ok("the colours keep their channels: dark at the top left", tl[0] < 30 && tl[2] < 30 && Math.abs(tl[1] - 90) < 20, tl.join());
    ok("red at the top right", tr[0] > 225 && tr[2] < 30, tr.join());
    ok("blue at the bottom left", bl[2] > 225 && bl[0] < 30, bl.join());
  });

  /* ================= stopping half-way ================= */
  step(function () {
    feed(new File([PH.pdf], "photos.pdf", { type: "application/pdf" }));
    return wait("the photos PDF is read again", read, 10000);
  });
  step(function () {
    var before = window.__saved.length;
    click("dlBtn");
    click("resetBtn");
    return settled().then(function () {
      eq("a clear while it works stops it: nothing is saved", window.__saved.length, before);
      eq("and the page is empty again, balanced chosen", txt("msg") + "|" + val("level"), "Choose a PDF to make smaller.|balanced");
    });
  });

  /* ================= other kinds of file ================= */
  step(function () {
    feed(F.damaged);
    return wait("a damaged PDF is read", read, 10000);
  });
  step(function () {
    has("a damaged file is read, and the visitor told", txt("msg"), "Its index is damaged; the smaller copy is rebuilt from what could be read.");
    eq("as a warning", document.getElementById("msg").className, "msg msg--bad");
    feed(F.signed);
    return wait("a signed PDF is read", read, 10000);
  });
  step(function () {
    has("a signed PDF is pointed out", txt("msg"), "It is digitally signed: compressing writes the file again, so the signature will no longer be valid.");
    feed(F.enc);
    return wait("a protected file is looked at", read, 10000);
  });
  step(function () {
    eq("a protected one is turned away", txt("msg"), "Could not use locked.pdf: protected with a password.");
    ok("with nothing to compress", document.getElementById("dlBtn").disabled);
    feed(F.empty);
    return wait("a PDF without pages is looked at", read, 10000);
  });
  step(function () {
    eq("one without pages too", txt("msg"), "Could not use empty.pdf: has no pages.");
    feed(F.notPdf);
    return wait("a file that is not a PDF is looked at", read, 10000);
  });
  step(function () {
    eq("and one that is not a PDF", txt("msg"), "Could not use notes.pdf: not a PDF.");
    feed(F.hostile);
    return wait("a file named as markup is read", read, 10000);
  });
  step(function () {
    has("its name is shown as text", txt("msg"), "<iframe onload=zq>.pdf: 1 page, ");
    eq("and made no element", document.querySelectorAll("iframe").length, 0);
    set("level", "strong");
    click("resetBtn");
    eq("clear empties everything and goes back to balanced", txt("msg") + "|" + val("level"), "Choose a PDF to make smaller.|balanced");
    ok("with nothing to compress", document.getElementById("dlBtn").disabled);
  });

  chain.then(function () { clearInterval(pacer); finish(); }, function (e) {
    ok("the test ran to the end", false, String(e && e.stack || e));
    clearInterval(pacer);
    finish();
  });
"""
# ---- END: the test for compress-pdf ----

# ===== END: the test bodies ================================================


# ===== START: how long a test body may take ===============================
# 12 virtual seconds is plenty for a text tool. It is not for a body that waits
# on real work several times over - every waitFor poll spends 100ms of virtual
# time, and while a canvas encodes or an image decodes the virtual clock runs
# ahead of the real one. A tool that builds four zips can need twenty virtual
# seconds and two real ones. Ask for more here, per tool; nothing else changes.
BUDGET_MS = {
    "compress-pdf": 600000,
    "income-tax-calculator": 15000,
    "currency-converter": 15000,
    "pdf-to-image": 400000,
    "protect-pdf": 300000,
    "pdf-metadata-editor": 300000,
    "add-pdf-page-numbers": 300000,
    "rotate-pdf": 300000,
    "remove-pdf-pages": 300000,
    "split-pdf": 500000,
    "favicon-generator": 40000,
    "image-splitter": 40000,
    "image-metadata-viewer": 40000,
    "image-cropper": 25000,
    "barcode-generator": 30000,
    "qr-code-generator": 40000,
    "js-minifier": 30000,
    "markdown-previewer": 60000,
    "image-to-pdf": 500000,
    "merge-pdf": 500000,
}


def budget_for(slug):
    return BUDGET_MS.get(slug, 12000)
# ===== END: how long a test body may take =================================


# ===== START: building and running one page ================================
def build(slug):
    """Write tools/_test-<slug>.html: the real page plus the harness."""
    page = (SITE / "tools" / ("%s.html" % slug)).read_text(encoding="utf-8")
    harness = (HARNESS.replace("__TESTS__", COMMON + T[slug])
                       .replace("__SLUG__", slug)
                       .replace("__BUDGET__", str(budget_for(slug))))
    target = SITE / "tools" / ("_test-%s.html" % slug)
    # The LAST </body>, never the first: a page that builds an HTML document in
    # its script (the markdown previewer) has "</body>" inside a string, and
    # putting the harness there cuts the string in half.
    head, closing, tail = page.rpartition("</body>")
    target.write_text(head + harness + "\n" + closing + tail,
                      encoding="utf-8")
    return target


# ===== START: a local web server, for pages that need one ==================
# pdf-to-image runs pdf.js, which fetches its character maps, fonts and
# picture decoders as it needs them. Served over http, as the real site is,
# its worker fetches them; opened from a file, the page fetches them itself,
# and the page's own rules (connect-src 'none') refuse - so a file:// test
# would test something the site never does. These pages are served from
# this folder by Python's own server, on this machine only, and Chrome opens
# them from there. The server notes every request, and a request for a file
# that is not there fails the test: a wrong address for one of pdf.js's
# files may only show as a fallback font on the page, but never as a 404.
HTTP_SLUGS = {"pdf-to-image"}
REQUESTS = []


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map,
                          **{".mjs": "text/javascript", ".wasm": "application/wasm",
                             ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
                             ".icc": "application/vnd.iccprofile"})

    def log_message(self, *args):
        pass

    def log_request(self, code="-", size="-"):
        REQUESTS.append((self.path, int(code) if str(code).isdigit() else 0))


_server = None
_server_lock = threading.Lock()


def local_server():
    """The address of a server for this folder, started the first time it is asked for."""
    global _server
    with _server_lock:
        if _server is None:
            handler = functools.partial(QuietHandler, directory=str(SITE))
            _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=_server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % _server.server_address[1]
# ===== END: a local web server =============================================


def run_one(slug):
    """Open the test page in Chrome and return its PASS/FAIL lines."""
    target = build(slug)
    if slug in HTTP_SLUGS:
        url = local_server() + "/tools/" + urllib.parse.quote(target.name)
    else:
        url = "file:///" + urllib.parse.quote(
            str(SITE).replace("\\", "/") + "/tools/" + target.name)
    limit = max(90, budget_for(slug) // 200)
    timed_out = False
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", NO_ADS_FLAG,
             "--window-size=1280,900",
             "--virtual-time-budget=%d" % (budget_for(slug) + 3000),
             "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=limit).stdout
    except subprocess.TimeoutExpired:
        # Not the same as a script that threw: Chrome itself ran out of time,
        # usually because the machine was busy. Saying so saves a hunt.
        dom = ""
        timed_out = True
    finally:
        # Always clean up. A leftover _test- file is harmless to check.py but
        # confusing to find in a diff a week later.
        try:
            target.unlink()
        except OSError:
            pass

    match = re.search(r'<pre id="RESULTS">(.*?)</pre>', dom or "", re.S)
    if not match and timed_out:
        return slug, ["FAIL  Chrome did not finish within %ds - a busy machine, "
                      "or a page that hangs; run this tool alone to tell" % limit]
    if not match:
        return slug, ["FAIL  the page produced no result at all - its script "
                      "probably threw before the harness ran"]
    body = htmllib.unescape(match.group(1))
    lines = [ln for ln in body.splitlines()
             if ln.startswith("PASS") or ln.startswith("FAIL")
             or ln.startswith("        ")]
    if slug in HTTP_SLUGS:
        mine = [r for r in REQUESTS if "_test-" + slug in r[0] or "/js/" in r[0] or "/css/" in r[0]]
        missing = sorted(set(path for path, code in REQUESTS if code >= 400))
        lines.append(("FAIL" if missing or not mine else "PASS") +
                     "  every file the page asked the server for was there (%d requests)" % len(REQUESTS))
        lines.extend("        missing: " + m for m in missing)
    return slug, lines
# ===== END: building and running one page ==================================


# ===== START: coverage, so tests cannot quietly rot ========================
def registered_slugs():
    registry = (SITE / "js" / "tools-data.js").read_text(encoding="utf-8")
    return set(re.findall(r'slug: "([^"]+)"', registry))
# ===== END: coverage =======================================================


# ===== START: main =========================================================
def main():
    # A failure line can quote the page, rupee sign and all. A Windows
    # console that cannot print one used to crash here, in the middle of
    # the report, and hide every failure after it.
    sys.stdout.reconfigure(errors="backslashreplace")
    wanted = sys.argv[1:]
    slugs = registered_slugs()

    untested = sorted(slugs - set(T))
    orphans = sorted(set(T) - slugs)

    todo = sorted(wanted) if wanted else sorted(set(T) & slugs)
    for bad in [s for s in todo if s not in T]:
        print("no test for '%s'" % bad)
        return 1

    passed = failed = 0
    problems = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for slug, lines in pool.map(run_one, todo):
            p = len([l for l in lines if l.startswith("PASS")])
            f = [l for l in lines if l.startswith("FAIL")]
            passed += p
            failed += len(f)
            flag = "x" if f else "-"
            print("  %s  %-32s %3d passed, %d failed" % (flag, slug, p, len(f)))
            if f:
                problems.append((slug, lines))

    print()
    if problems:
        print("=" * 70)
        for slug, lines in problems:
            print("  %s" % slug)
            show = False
            for line in lines:
                if line.startswith("FAIL"):
                    show = True
                    print("    %s" % line)
                elif show and line.startswith("        "):
                    print("    %s" % line)
                else:
                    show = False
        print("=" * 70)
        print()

    print("  %d tools, %d assertions, %d failed" % (len(todo), passed + failed, failed))

    # Coverage is part of the result, not a footnote. A tool with no test is
    # an untested tool however green the rest of the run looks.
    if untested and not wanted:
        print("  NO TEST AT ALL for: %s" % ", ".join(untested))
    if orphans:
        print("  test with no registered tool: %s" % ", ".join(orphans))

    if failed or (untested and not wanted) or orphans:
        print("\n  NOT ready to deploy.\n")
        return 1
    print("\n  All tools work.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# ===== END: main ===========================================================
