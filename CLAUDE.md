# 108 ToolBox — context for Claude

Read this first. It is loaded automatically in every session opened in this
folder, so you never need to be told the project's history again.

**What this is:** a static site of free browser-based utilities, live at
<https://108toolbox.in>. Plain HTML, CSS and JavaScript. No framework, no npm,
no build step, and it stays that way.

**Where it stands:** 51 of a planned 108 tools are built, tested and live.
Phase 2 is finished; Phase 3 has started.

---

## The rules that matter

### 1. A new tool is four steps, and nothing else changes

1. Copy `tools/_template.html` → `tools/<slug>.html`, work through every `TODO`
2. Add one object to `js/tools-data.js` — **the registry, the spine of the site**
3. Add one `<url>` to `sitemap.xml`
4. Run `python check.py`

`main.js`, `style.css` and the navbar are written to work untouched at 108
tools. **If you find yourself editing `main.js` to add a tool, stop** — you
have gone off the path. (Editing it for a site-wide design change is fine.)

### 2. Never type a tool count into a page

`51 tools live` is not typed anywhere. Any element with `data-tool-count` is
filled in by `main.js` straight from the registry:

```html
<span data-tool-count="live">51</span> tools live
<span data-tool-count="remaining">57</span> on the way
```

This used to be hand-typed in `index.html`, `tools.html` and `about.html`, and
it went stale on the live site **twice** — the page said "15 tools live" while
the grid underneath it showed 20. The number left in the HTML is only a
fallback for the moment before JavaScript runs.

**That fallback is now checked.** Calling it "only a fallback" is exactly why
nobody noticed it rotting: it sat at 25 while the registry held 45, so every
visitor with JavaScript off and every crawler that does not render read the
wrong number on three pages. `check.py` now fails if any `data-tool-count`
span disagrees with the registry, and tells you the number it should be. It
also fails if the spans disappear entirely, which would mean the markup was
renamed and `main.js` had quietly stopped filling anything in.

### 3. Bump `?v=` whenever you touch `css/` or `js/`

Every page links its assets like this:

```html
<link rel="stylesheet" href="css/style.css?v=2">
```

GitHub Pages sends `Cache-Control: max-age=600`. Without a new version stamp a
returning visitor keeps the old stylesheet and swears nothing changed.
Find-and-replace `?v=12` → `?v=13` across all 59 pages (currently `?v=12`,
287 occurrences), the template included.

**This is checked now — it failed three times on memory alone.** The worst was
the quietest: tools 46 and 47 were added to `js/tools-data.js` without a bump,
so every page still asked for `tools-data.js?v=9`, the exact URL browsers
already held with 45 tools in it. Both pages returned 200, the server served
the new registry, `curl` reported 47 — and the site still looked untouched to
anyone who had visited before, because their browser never asked. `curl` has
no cache, which is exactly why verifying with it proved nothing.

`assets.lock` holds the current stamp and a hash of every file in `css/` and
`js/`. `check.py` fails if those files move and the stamp does not, and names
the number to bump to. It also fails if pages disagree about the stamp, which
is a half-finished find-and-replace and hands some visitors the new CSS with
the old script. When you bump correctly, the lock updates itself — commit it
with the change. The hash normalises line endings first, because Git rewrites
LF to CRLF on checkout here and raw bytes would disagree between machines.

### 4. Two scripts, and you run BOTH, every time

```
python check.py        reads the files   — markup, titles, meta, canonicals,
                                           JSON-LD, the registry, links
python test_tools.py   RUNS the files    — every tool, in real Chrome,
                                           driving the real controls
```

`check.py` never executes a line of JavaScript. A tool that throws on the
first keystroke still has a perfect title, a valid canonical and clean
JSON-LD, and `check.py` will wave it through.

**`test_tools.py` runs all 51, not just the one you changed.** That is the
point of it — a shared change like a `main.js` edit or a `?v=` bump can break
a tool you never opened. It also **fails if a registered tool has no test at
all**, so a new tool is not finished until its assertions exist. One tool
while you work: `python test_tools.py gst-calculator`.

Adding a tool means adding its test body to `T` in `test_tools.py`. Both
scripts green, then push.

### 5. Never type a `\uXXXX` escape into a tool's `<script>`

It can land in the file as the literal character. U+2028 and U+2029 are
JavaScript line terminators, so a regex literal containing one does not parse —
the file looks perfect in an editor and the entire tool silently does nothing.
Build the pattern from code points instead; `tools/whitespace-remover.html`
shows the pattern.

### 6. Nothing is ever uploaded

Every tool runs client-side — canvas, Web Crypto, `pdf-lib`. The homepage
promises "nothing is uploaded, nothing is stored" and that promise must stay
literally true. It is the site's only real differentiator. See the
"Tools to never build" section of `ROADMAP.md` before adding anything that
touches a server, a third-party API, or someone else's content.

### 7. Do not add Google Analytics

The homepage says "no tracking". If analytics are ever wanted, use Plausible,
Umami or Cloudflare Web Analytics — or drop the claim. Not both.

### 8. Never name another website on the site

Not a comparison, not a "better than X", not a link, **not one letter**. The
owner's instruction, and it is the right call: naming a competitor on your own
pages sends visitors to look them up, hands them a free mention, and dates the
page the moment they change. Keep every page about what this site does.

Competitor research belongs in `ROADMAP.md`, which is a working document, not
a page anybody lands on. The 123apps note under **PDF** is the shape to
follow.

This is about *other websites*, not about honest disclosure. `privacy.html`
naming GitHub Pages as the host stays — it is a legal requirement and it is a
supplier, not a rival. Same for a library credited in a code comment.

### 9. Mark where every function and feature starts and ends

The owner asked for this so a bug can be found and fixed without reading the
whole file. In any `<script data-tool>`, and in the Python scripts:

```js
/* ---- START: recovering the base from a GST-inclusive total ---- */
function removeGst(total, rate) {
  return total / (1 + rate / 100);
}
/* ---- END: recovering the base from a GST-inclusive total ---- */
```

Name the **job**, not the function — "recovering the base from a
GST-inclusive total" is findable six months later; "removeGst helper" is not.
Wrap a whole feature the same way when several functions serve one job, and
nest the inner ones. `test_tools.py` shows the pattern at both levels.

### 10. Every tool page carries the check-the-result notice

The owner's instruction, and a correct one: **a tool can be wrong, and the
visitor has to be told so where they read the answer.** Not buried in the FAQ,
not only on `disclaimer.html` — directly under the tool.

```html
<!--TOOL:END-->
<aside class="tool-warn">…Check the result before you rely on it…</aside>
```

It is in `tools/_template.html`, so a new tool inherits it by copying. Do not
reword it per tool — one sentence everywhere is what makes it read as a
standing policy rather than an admission about that one page. A tool with a
risk of its own adds a second, specific line inside its own output, the way
`sip-calculator.html` says returns are not guaranteed.

Both scripts enforce it, and they catch different failures:

- `check.py` fails if the markup, the wording or the `disclaimer.html` link
  is missing from any tool page.
- `test_tools.py` fails if the notice is in the markup but **not on the
  screen** — zero height, `display:none`, hidden, transparent. `check.py`
  cannot see that, because it never executes CSS. This was proved by hiding
  the notice on `word-counter.html`: `check.py` passed it, the browser run
  caught it.

`check.py` also compares the two dark-theme blocks token for token. They are
deliberate duplicates — the OS preference and the site's own toggle — so a
token added to one and forgotten in the other is always a bug, and it only
shows for half the visitors. `--warn` shipped exactly that way for ten
minutes: cream-on-cream, unreadable, for anyone using the toggle.

---

## Layout

```
index.html         Homepage — hero, search, popular grid
tools.html         All tools — search + category chips + grid
about / privacy / contact / 404.html
css/style.css      Design system: TOKENS first, then components
css/tool.css       Tool pages only: panels, stat tiles, drop zones, tables
js/tools-data.js   THE REGISTRY — one object per tool
js/main.js         Renders grids, search, chips, theme, related strip
js/tool-helpers.js copyText, downloadText, downloadBlob, showToast,
                   formatBytes, formatNumber
tools/*.html       One self-contained file per tool
tools/_template.html   Start here for a new tool (check.py skips `_` files)
check.py           Reads the files — run before every push
test_tools.py      RUNS every tool in real Chrome — run before every push
setup.py           One-time domain/name/email rewrite (already run)
```

**Docs:** `ROADMAP.md` (all 108 tools, phases, what never to build,
performance at scale) · `SEO.md` (traffic and keyword playbook) ·
`DEPLOY.md` (hosting and DNS) · `README.md` (quick start).

---

## Design system

Everything reads from tokens at the top of `css/style.css`. **Never hard-code a
colour or a size** — add a token first.

- Indigo `--primary` with a cyan `--accent` that only ever appears in gradients
- Dark mode is a deep blue-black (`#0b0d16`), not neutral grey
- Four category accents — Text violet, Image rose, Calculator amber,
  Developer emerald — driven by `data-cat` on the card, set in `main.js`
- Spacing is a strict 4px scale, `--s1` … `--s9`
- No webfonts, no image files. Icons are emoji, the favicon is inline SVG.
  The homepage weighs about 46 KB and should stay under 100 KB.
- **The homepage may be loud. A tool page must be calm** — someone landed
  there to do one job. Tool above the fold, always; explanation below it.

---

## Deployment

```
Host    GitHub Pages, branch main, folder /
Repo    https://github.com/RAVITEJAanand/108toolbox  (public)
Domain  108toolbox.in — GoDaddy DNS, 4 A records to GitHub + www CNAME
```

Push to `main` and it deploys in under a minute. Before pushing: run
`check.py`, and open the changed pages in a browser.

**HTTPS is live and enforced.** Resolved 19 Sep 2026. The certificate covers
`108toolbox.in` and `www.108toolbox.in`, and **Enforce HTTPS** is on, so
`http://` returns a 301 to `https://`.

A note for next time, because the diagnosis here was wrong twice.

The certificate was issued at **12:09 UTC** on 19 Sep 2026 with nothing done
to trigger it. For hours before that, the server answered with the generic
`CN=*.github.io`, which is indistinguishable from a failed request, and this
file said the request had stalled and that the custom domain had to be removed
and re-added.

The domain was in fact removed and re-added later that day, at 18:35 UTC -
the `Delete CNAME` and `Create CNAME` commits in the history. It made no
difference to the certificate: the one being served now still carries the
12:09 issue time, so no new certificate was ever requested.

**The lesson is to wait.** GitHub can take most of a day, and what the server
serves lags behind what GitHub has already issued.

Two ways to check, and they disagree during that waiting period:

```bash
# What the server is actually serving right now
echo | openssl s_client -servername 108toolbox.in -connect 108toolbox.in:443 2>/dev/null | openssl x509 -noout -subject

# What GitHub thinks, which turns green first
gh api repos/RAVITEJAanand/108toolbox/pages --jq '.https_certificate.state, .https_enforced'
```

`gh` keeps its login in the Windows keyring, and a terminal that cannot reach
the keyring reports "not logged in" when you are. Running `gh auth login` once
in that terminal fixes it.

**Google Search Console is set up.** Verified 20 Sep 2026 as a **Domain
property** (`sc-domain:108toolbox.in`), which covers the apex, `www`, `http`
and `https` in one go. Google added the verifying TXT record itself through
GoDaddy's official integration, so no DNS was edited by hand:

```
TXT  108toolbox.in  google-site-verification=odYOhro-hY_X3jyvTVuZRURPhaFkmY0zzpLcW6Upxtg
```

**Never delete that TXT record** - the property un-verifies the moment it goes.
The four A records and the `www` CNAME were untouched by the process; that was
checked afterwards, and the site stayed up throughout.

One trap, which cost a round trip here. A Domain property **rejects
`sitemap.xml`** in the Sitemaps box with "Invalid sitemap address". A Domain
property spans four protocol-and-host combinations, so it cannot guess which
one is meant. Give it the whole URL:

```
https://108toolbox.in/sitemap.xml
```

A URL-prefix property would have accepted the bare filename. The sitemap holds
33 URLs while the site has 34 pages, and that is correct: `404.html` is
deliberately left out, because an error page must never be offered to an index.

"Discovered pages: 0" straight after submitting is normal - Google has only
accepted the sitemap, not yet read it.

---

## What is built, and what is next

**Live (51):** word-counter, case-converter, lorem-ipsum-generator,
remove-duplicate-lines, find-and-replace, sort-text-lines,
remove-line-breaks, whitespace-remover, reverse-text, text-repeater,
add-line-numbers, slug-generator, character-frequency-counter,
image-compressor, image-converter, percentage-calculator, age-calculator,
emi-calculator, bmi-calculator, discount-calculator, tip-calculator,
average-calculator, ratio-calculator, gst-calculator,
simple-interest-calculator, compound-interest-calculator,
fraction-calculator, margin-markup-calculator, area-converter,
number-to-words, temperature-converter, date-difference-calculator,
add-subtract-days, sip-calculator, unit-converter,
binary-decimal-hex-converter, timestamp-converter, days-until-countdown,
password-generator, json-formatter, roman-numeral-converter,
data-storage-converter, speed-converter, leap-year-checker,
week-number-calculator, salary-calculator, fuel-cost-calculator,
text-to-morse, nato-phonetic-converter, cooking-measurement-converter,
shoe-size-converter.

That count is checked: `check.py` fails if it drifts from the registry.
It read 30 while listing 40 for a whole batch, which is the same hand-typed
count problem rule 2 exists to stop, one file further out.

**Next batch (Phase 3, tools 46–50):** more converters and date/time, per
`ROADMAP.md`. Phase 3 runs 31–60 and still needs no libraries.

`salary-calculator` is still deliberately skipped. CTC to in-hand
needs current income tax slabs, and a figure that goes stale without anyone
noticing is worse than no tool. Build it with a visible "rates as of" date
and add it to the Maintenance list, or leave it.

**Categories are 8 now**, expanded on 20 Sep 2026 at the 30-tool mark:
Text, Image, Calculator, Developer, Converter, PDF, Date & Time, Random.
Converter and Date & Time filled up on 21 Sep 2026 and their chips appeared
on their own. PDF and Random have no tools yet, and that is fine — **`main.js` draws a chip only
for a category that has at least one tool**, so `CATEGORIES` is the plan, not
the inventory. A chip that opens an empty grid reads as broken rather than
unfinished, which is exactly what the naive version of this change would have
shipped. `check.py` now also fails if a tool claims a category that is not in
the list; that typo used to be invisible, because the tool still rendered but
no chip ever matched it.

The footer "Categories" column stays at the four real ones. Those links are
searches (`tools.html?q=pdf`), so adding an empty category there is a dead
link. Add each one when its category gets its first tool.

**The India angle is the real SEO edge.** `gst-calculator`, `sip-calculator`,
`salary-calculator`, `area-converter` and `number-to-words` (lakh/crore) have
high volume and weak competition, unlike "word counter". See `SEO.md`.

---

## Working with the owner

Explain things plainly — they are learning, and they asked for beginner-level
clarity while intending to get advanced. Say *why*, not just *what*. The
existing code comments are written in that voice; match it.

They write in Telugu and in English. **Reply in whichever language they used.**
Keep technical terms (A record, CNAME, regex, cache) in English, since that is
what they will see on screen.
