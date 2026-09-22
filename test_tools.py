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
import pathlib
import re
import subprocess
import sys
import urllib.parse
import html as htmllib

SITE = pathlib.Path(__file__).parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Four at a time. Chrome is heavy and the machine still has to be usable.
WORKERS = 4


# ===== START: the in-page harness ==========================================
# Every helper any test body uses lives here. The bodies below were written
# against several older harnesses, so this one is deliberately the union of
# all of them rather than the smallest set that would do.
HARNESS = r"""
<script>
(function () {
  var results = [];
  var done = false;

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
      if (waited >= (budgetMs || 6000)) {
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
  }, 12000);

  /* Any uncaught error anywhere on the page is itself a failure */
  window.addEventListener("error", function (e) {
    record(false, "page threw: " + e.message + " @ " + (e.filename || "?") +
           ":" + e.lineno);
  });
  window.addEventListener("unhandledrejection", function (e) {
    record(false, "unhandled rejection: " + e.reason);
  });
  /* ---- END: finishing ---- */

  try {
    __TESTS__
  } catch (e) {
    record(false, "threw: " + (e && e.message ? e.message : e));
    finish();
  }
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

    set("input", "");
    check("empty shows a dash", txt("mean"), String.fromCharCode(0x2014));
    check("and zero count", txt("count"), "0");

    /* A value that collides with a built-in property name */
    set("input", "1, 1, 2");
    check("mode still works", txt("mode"), "1");
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
              finish();
            });
        });
    });
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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

T["remove-line-breaks"] = r"""
    set("input", "line one\nline two\nline three");
    ok("breaks removed", txt("output").indexOf("\n") === -1, txt("output"));
    ok("words still separated", txt("output").indexOf("one line") > -1, txt("output"));
    check("lines in", txt("cIn"), "3");
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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

    set("start", "2026-01-31"); set("end", "2026-02-28");
    has("31 Jan to 28 Feb is one month", txt("breakdown"), "0 years, 0 months, 28 days");

    set("start", "");
    shown("a missing date is refused", "errWrap");
    finish();
"""

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

    set("start", "");
    shown("a missing date is refused", "errWrap");
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
    finish();
"""

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
  set("basicPct", 40);
  eq("and the warning clears", txt("msg"), "");

  set("ctc", "");
  near("an empty CTC does not crash", txt("inHand"), -200, 1);
    finish();
"""

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
    finish();
"""

# ===== END: the test bodies ================================================


# ===== START: building and running one page ================================
def build(slug):
    """Write tools/_test-<slug>.html: the real page plus the harness."""
    page = (SITE / "tools" / ("%s.html" % slug)).read_text(encoding="utf-8")
    harness = HARNESS.replace("__TESTS__", COMMON + T[slug]).replace("__SLUG__", slug)
    target = SITE / "tools" / ("_test-%s.html" % slug)
    target.write_text(page.replace("</body>", harness + "\n</body>"),
                      encoding="utf-8")
    return target


def run_one(slug):
    """Open the test page in Chrome and return its PASS/FAIL lines."""
    target = build(slug)
    url = "file:///" + urllib.parse.quote(
        str(SITE).replace("\\", "/") + "/tools/" + target.name)
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             "--window-size=1280,900", "--virtual-time-budget=15000",
             "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90).stdout
    except subprocess.TimeoutExpired:
        dom = ""
    finally:
        # Always clean up. A leftover _test- file is harmless to check.py but
        # confusing to find in a diff a week later.
        try:
            target.unlink()
        except OSError:
            pass

    match = re.search(r'<pre id="RESULTS">(.*?)</pre>', dom or "", re.S)
    if not match:
        return slug, ["FAIL  the page produced no result at all - its script "
                      "probably threw before the harness ran"]
    body = htmllib.unescape(match.group(1))
    return slug, [ln for ln in body.splitlines()
                  if ln.startswith("PASS") or ln.startswith("FAIL")
                  or ln.startswith("        ")]
# ===== END: building and running one page ==================================


# ===== START: coverage, so tests cannot quietly rot ========================
def registered_slugs():
    registry = (SITE / "js" / "tools-data.js").read_text(encoding="utf-8")
    return set(re.findall(r'slug: "([^"]+)"', registry))
# ===== END: coverage =======================================================


# ===== START: main =========================================================
def main():
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
