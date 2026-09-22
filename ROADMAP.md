# 108 ToolBox — Roadmap: 10 tools → 108

Phase 1 is done: 10 tools, live, `check.py` passing. This file is the plan for
the other 98. Read it once now, then come back to it every time you sit down
to build.

---

## The one rule that makes this possible

**Every tool is a copy of an existing tool page with the middle swapped out.**

That is why the project has no framework and no build step. A new tool is:

1. `tools/<slug>.html` — copy of the closest existing tool
2. one object in `js/tools-data.js`
3. one `<url>` in `sitemap.xml`
4. `python check.py`

Nothing else changes. `main.js`, `style.css` and the navbar are already
written to work untouched at 108 tools. If you ever find yourself editing
`main.js` to add a tool, stop — you have gone off the path.

### The template — `tools/_template.html` ✅

Already built. Copy it for every new tool:

```
copy tools\_template.html tools\your-slug.html
```

It is the word counter stripped to the skeleton — head, navbar, breadcrumb,
the `<!--TOOL:START-->` / `<!--TOOL:END-->` markers, the "How to use it" and
FAQ shells, the JSON-LD and the footer — with a `TODO` on every line you must
change. `check.py` skips files whose name starts with `_`, so the template is
never reported as an unregistered tool.

### Two gotchas that cost real time

1. **Never type a `\uXXXX` escape into a tool's `<script>` by hand.** It can
   land in the file as the literal character instead of the escape, and
   U+2028 and U+2029 are line terminators as far as JavaScript is concerned —
   a regex literal containing one simply does not parse. The file looks
   perfect in an editor and the whole tool silently does nothing. Build the
   pattern from code points instead; `whitespace-remover.html` shows how.

2. **`check.py` passing does not mean the tool works.** It validates markup,
   titles, links and the registry — it never runs your JavaScript. Always open
   the page and actually use it before you upload.

---

## Build order: five phases

Difficulty is honest: **easy** = one afternoon, **medium** = a weekend,
**hard** = needs a third-party library and real testing.

| Phase | Tools | Why these next | Target |
|---|---|---|---|
| 1 ✅ | 1–10 | Proof the system works | done |
| 2 ✅ | 11–30 | Highest search volume, all easy, no libraries | done |
| 3 | 31–60 | Converters and date/time. Still no libraries | **15 of 30 done** |
| 4 | 61–90 | PDF and files. First real libraries | month 7–9 |
| 5 | 91–108 | The long tail and the India-specific calculators | month 10–12 |

Two tools a week gets you to 108 in about a year. Five a week gets you there
in five months. Pick a number you can keep, not a number that sounds good.

**Batch by category.** Building five text tools in one sitting is roughly
twice as fast as building five unrelated ones, because your head stays in the
same mental model and you keep copying the same page.

---

## Categories: expanded to 8 ✅

Done at the 30-tool mark, 20 Sep 2026. `CATEGORIES` in `js/tools-data.js` is
now:

```js
["Text", "Image", "Calculator", "Developer",
 "Converter", "PDF", "Date & Time", "Random"]
```

Two things this plan originally got wrong, both found while doing it.

**The chips would have been empty.** `main.js` built one chip per entry in
`CATEGORIES`, so expanding the list on its own would have shipped four chips -
Converter, PDF, Date & Time, Random - that open an empty grid. A visitor reads
that as broken, not unfinished. `main.js` now draws a chip only for a category
that has at least one tool, which is why `CATEGORIES` is free to run ahead of
what is built. The chip appears by itself the day the first tool claims it.

**The allowed-category list in `check.py` did not exist.** This file said to
update it; there was nothing to update. It exists now, and it fails the build
if a tool carries a category that is not in `CATEGORIES`. That typo used to be
invisible: the tool still appeared on the grid, but no chip ever matched it, so
search was the only way to reach it.

The footer "Categories" column in `index.html` and `tools.html` was left at the
four real categories on purpose. Those links are searches (`tools.html?q=pdf`),
so pointing them at an empty category is a dead link, not a preview. Add each
one when its category gets its first tool.

### Advertising, once 108 is reached

The owner's decision, taken while the site stood at 56 tools: **advertising
goes on when all 108 are live, and not before.** Until then the site carries
no advertising of any kind.

The awkward part is not the ad code. It is that this site currently *promises*
there are no ads, in four different places, and one of them is on every single
page. Turning ads on without rewriting those first would make the site lie to
its visitors on the day it starts earning — so the promises are rewritten
first, in their own commit, and only then does the ad code go in.

**What has to change**

| Where | What it says now |
|---|---|
| `index.html`, the hero line | "no ads &middot; no tracking" |
| the footer, **all 66 pages** | "No sign-up, no ads, no file uploads." |
| `privacy.html` section 7 | "There are **no advertisements on this site today**." |
| `disclaimer.html` section 9 | "There are no advertisements on this site today." |

`privacy.html` section 7 and `disclaimer.html` section 9 were written in
advance for exactly this day: both already explain what a third-party ad
provider would do, that its cookies belong to it and not to us, and where a
visitor can turn personalised ads off. They need the tense changed, not a
rewrite.

**What stays true, and must not be quietly dropped**

- "Your files never leave your device" — still true. An ad script cannot see
  what is typed into a tool; the tools still run entirely in the browser.
- "No sign-up", "no file uploads", "no watermark", "no limits" — all still true.
- The "Free forever" card never claimed there were no ads, so it stands as is.

So the honest replacements are shorter, not weaker: the footer becomes
"No sign-up, no file uploads." and the hero line trades "no ads &middot; no
tracking" for something that is still true, such as "nothing uploaded".

**What AdSense will need from us**

1. A privacy policy naming the third-party cookies — mostly written already.
2. A consent mechanism for visitors in the EU and UK. Google requires a
   certified CMP there, and an India-first site still gets that traffic.
3. `ads.txt` at the site root. GitHub Pages serves it like any other file.
4. Ad placement that cannot be mistaken for a tool control. Accidental clicks
   are an AdSense policy violation, and on a site of buttons and output boxes
   that is a real risk, not a formality.

**What it costs, measured**

A tool page today is **6 requests and 27 kB**, with **no third-party
JavaScript at all**, and paints in about 220 ms. AdSense typically adds
100-300 kB of third-party script and several more connections. It will be,
by a wide margin, the heaviest thing on the site — roughly ten times
everything else put together. That is the trade, and it is the owner's to
make; it is written here so it is made with the number in front of us.

### Final shape at 108

| Category | Tools | Built |
|---|---|---|
| Calculator | 20 | 16 |
| Developer | 18 | 7 |
| Text | 16 | 13 |
| Image | 14 | 2 |
| Converter | 14 | 13 |
| PDF | 10 | 0 |
| Date & Time | 8 | 5 |
| Random | 8 | 0 |
| **Total** | **108** | **56** |

---

## The full 108

Slugs are final — they become the URL people share and the phrase Google
matches, so do not rename them later. ✅ = already live.

### Text (16)

| # | Slug | Difficulty |
|---|---|---|
| 1 | `word-counter` ✅ | easy |
| 2 | `case-converter` ✅ | easy |
| 3 | `lorem-ipsum-generator` ✅ | easy |
| 4 | `remove-duplicate-lines` ✅ | easy |
| 5 | `find-and-replace` ✅ | easy |
| 6 | `sort-text-lines` ✅ | easy |
| 7 | `remove-line-breaks` ✅ | easy |
| 8 | `whitespace-remover` ✅ | easy |
| 9 | `reverse-text` ✅ | easy |
| 10 | `text-repeater` ✅ | easy |
| 11 | `add-line-numbers` ✅ | easy |
| 12 | `slug-generator` ✅ | easy |
| 13 | `character-frequency-counter` ✅ | easy |
| 14 | `readability-score` | medium — Flesch formula, explain the number |
| 15 | `text-diff-checker` | medium — line diff, colour the changes |
| 16 | `text-to-speech` | medium — browser `speechSynthesis`, no API, no cost |

### Image (14) — all canvas, nothing uploaded

| # | Slug | Difficulty |
|---|---|---|
| 1 | `image-compressor` ✅ | medium |
| 2 | `image-converter` ✅ | medium |
| 3 | `image-resizer` | easy |
| 4 | `image-rotator` | easy |
| 5 | `image-to-base64` | easy |
| 6 | `svg-to-png` | easy |
| 7 | `image-placeholder-generator` | easy |
| 8 | `image-color-picker` | medium — canvas pixel read |
| 9 | `image-cropper` | medium — the drag handles are the hard part |
| 10 | `favicon-generator` | medium — multi-size export |
| 11 | `photo-watermark` | medium |
| 12 | `meme-generator` | medium — the user supplies the image, always |
| 13 | `image-splitter` | medium — grid split for Instagram |
| 14 | `image-metadata-viewer` | hard — EXIF parsing |

### Calculator (20) — your quiet advantage, see the India note below

| # | Slug | Difficulty |
|---|---|---|
| 1 | `percentage-calculator` ✅ | easy |
| 2 | `age-calculator` ✅ | easy |
| 3 | `emi-calculator` ✅ | medium |
| 4 | `bmi-calculator` ✅ | easy |
| 5 | `discount-calculator` ✅ | easy |
| 6 | `tip-calculator` ✅ | easy |
| 7 | `average-calculator` ✅ | easy |
| 8 | `ratio-calculator` ✅ | easy |
| 9 | `fraction-calculator` ✅ | easy |
| 10 | `simple-interest-calculator` ✅ | easy |
| 11 | `compound-interest-calculator` ✅ | easy |
| 12 | `margin-markup-calculator` ✅ | easy |
| 13 | `unit-price-comparison` | easy |
| 14 | `fuel-cost-calculator` ✅ | easy |
| 15 | `gst-calculator` ✅ | easy — India |
| 16 | `sip-calculator` ✅ | medium — India, very high volume |
| 17 | `salary-calculator` ✅ | medium — CTC to in-hand, India |
| 18 | `calorie-calculator` | medium — BMR / TDEE |
| 19 | `scientific-calculator` | medium |
| 20 | `income-tax-calculator` | hard — **rates change yearly, see Maintenance** |

### Developer (18)

| # | Slug | Difficulty |
|---|---|---|
| 1 | `password-generator` ✅ | medium |
| 2 | `json-formatter` ✅ | medium |
| 3 | `base64-encoder-decoder` ✅ | easy |
| 4 | `url-encoder-decoder` ✅ | easy |
| 5 | `html-encoder-decoder` ✅ | easy |
| 6 | `uuid-generator` ✅ | easy — `crypto.randomUUID()` |
| 7 | `color-code-converter` ✅ | easy — HEX / RGB / HSL |
| 8 | `hash-generator` | medium — Web Crypto, SHA-256/384/512 |
| 9 | `jwt-decoder` | medium — decode and display only, never verify |
| 10 | `regex-tester` | medium — live match highlighting |
| 11 | `cron-expression-parser` | medium — "at 05:00 every Monday" |
| 12 | `json-to-csv` | medium |
| 13 | `csv-to-json` | medium |
| 14 | `html-minifier` | medium |
| 15 | `css-minifier` | medium |
| 16 | `sql-formatter` | hard |
| 17 | `markdown-previewer` | hard — needs a library |
| 18 | `js-minifier` | hard — needs a library, lazy-load it |

### Converter (14)

The other shelf that meets 123apps head on — see the note under PDF below.
Their converters are the file kind (video, audio, archives, ebooks), which
genuinely need a server. Most of the fourteen here convert numbers and units,
which never leave the tab, so the overlap is narrower than the word suggests.

| # | Slug | Difficulty |
|---|---|---|
| 1 | `unit-converter` ✅ | medium — length / weight / volume on one page |
| 2 | `temperature-converter` ✅ | easy |
| 3 | `speed-converter` ✅ | easy |
| 4 | `data-storage-converter` ✅ | easy |
| 5 | `area-converter` ✅ | easy — include gunta / bigha / cent, India |
| 6 | `binary-decimal-hex-converter` ✅ | easy |
| 7 | `roman-numeral-converter` ✅ | easy |
| 8 | `number-to-words` ✅ | medium — add the Indian lakh / crore system |
| 9 | `timestamp-converter` ✅ | easy — Unix ↔ human date |
| 10 | `cooking-measurement-converter` ✅ | easy |
| 11 | `shoe-size-converter` ✅ | easy |
| 12 | `text-to-morse` ✅ | easy |
| 13 | `nato-phonetic-converter` ✅ | easy |
| 14 | `currency-converter` | hard — **the only tool needing a live API** |

### PDF (10) — all with `pdf-lib`, all in the browser

| # | Slug | Difficulty |
|---|---|---|
| 1 | `merge-pdf` | medium |
| 2 | `split-pdf` | medium |
| 3 | `remove-pdf-pages` | medium |
| 4 | `rotate-pdf` | medium |
| 5 | `image-to-pdf` | medium |
| 6 | `add-pdf-page-numbers` | medium |
| 7 | `protect-pdf` | medium — add a password |
| 8 | `pdf-metadata-editor` | medium |
| 9 | `pdf-to-image` | hard — needs `pdf.js` as well |
| 10 | `compress-pdf` | hard — re-encodes images, test it a lot |

**This batch is the one that meets a real competitor.** Checked 20 Sep 2026.
<https://123apps.com> has run since about 2014 and carries roughly 51 tools:
18 video, 8 audio, **17 PDF** and 8 converters. Nothing on the site today
overlaps with it — they have no word counter, no GST calculator, no JSON
formatter — but their PDF and Converter shelves are exactly the two this file
plans next.

Their own privacy policy is the reason not to worry about it:

> Any Data which you provide for the purpose of utilizing our Services is
> stored on our servers only for as long as necessary, and is deleted
> automatically no later than twelve (12) hours from the moment you complete
> your use of our Services.

They also load Google Analytics, DoubleClick and AdSense, gate large files
behind a subscription, and ask people to sign in. **None of that is a choice
they can reverse.** Converting video needs a server, and a server needs paying
for, which needs ads and accounts. The upload is load-bearing for their whole
business.

So when these ten get built, lead with the thing they structurally cannot say.
Somebody merging a salary slip, a bank statement or an Aadhaar PDF is being
asked to hand a stranger's server twelve hours with it. `pdf-lib` does the same
job in the tab. On this shelf "nothing is uploaded" stops being a nice line in
the footer and becomes the entire reason to pick this site.

### Date & Time (8)

| # | Slug | Difficulty |
|---|---|---|
| 1 | `date-difference-calculator` ✅ | easy |
| 2 | `add-subtract-days` ✅ | easy |
| 3 | `leap-year-checker` ✅ | easy |
| 4 | `week-number-calculator` ✅ | easy |
| 5 | `days-until-countdown` ✅ | easy |
| 6 | `working-days-calculator` | medium — holidays are the tricky bit |
| 7 | `stopwatch-timer` | medium |
| 8 | `time-zone-converter` | medium — `Intl.DateTimeFormat`, no library |

### Random (8)

| # | Slug | Difficulty |
|---|---|---|
| 1 | `random-number-generator` | easy |
| 2 | `coin-flip` | easy |
| 3 | `dice-roller` | easy |
| 4 | `random-list-shuffler` | easy |
| 5 | `random-picker` | medium — name / winner picker |
| 6 | `username-generator` | easy |
| 7 | `qr-code-generator` | medium — needs a small library |
| 8 | `barcode-generator` | medium — needs a small library |

---

## Tools to never build

Every one of these has traffic. Every one is a bad idea for a site you own
under your real name and domain.

- **Video and audio downloaders** (YouTube, Instagram, TikTok, Spotify) —
  against those platforms' terms, frequently a copyright problem, and the
  reason most tool sites get deindexed or get a legal letter. This is the
  single biggest temptation; the answer is no.
- **DRM removal, paywall bypass, "read any article free".** Same reason.
- **Torrent, proxy or unblocker tools.**
- **Plagiarism checkers and "AI detectors"** — they need a scraped corpus you
  do not have the rights to, and the AI ones do not actually work. Claiming
  otherwise is a consumer-protection problem, not just an accuracy one.
- **Fake ID, fake receipt, fake certificate, fake screenshot generators** —
  the entire purpose is deceiving someone.
- **Email and phone scrapers.**
- **Anything that uploads a user's file to a server you run.** The moment you
  do, you own that data, and both your privacy page and this site's whole
  "nothing is uploaded" promise become false.

Keep that promise literally true. It is your real differentiator against
every other tool site, and it costs you nothing, because canvas, Web Crypto
and `pdf-lib` all run client-side.

---

## Third-party libraries

Most of the 108 need none. About a dozen do. The rules:

1. **MIT or Apache-2.0 only.** Check the licence before you download it.
2. **Self-host the file** in `js/lib/`. Never hotlink a CDN — it leaks your
   visitors' IP addresses to a third party, which contradicts your privacy
   page, and it adds a DNS lookup to every page load.
3. **Keep the licence text.** Create `js/lib/LICENSES.md` listing each
   library, its version, its licence and where you got it. Ten minutes now,
   no problem later.
4. **Load it only on the page that needs it** (see Performance below).

Libraries you will actually need:

| Library | Licence | Used by |
|---|---|---|
| `pdf-lib` | MIT | all 10 PDF tools |
| `pdf.js` | Apache-2.0 | `pdf-to-image` |
| `qrcode` | MIT | `qr-code-generator` |
| `JsBarcode` | MIT | `barcode-generator` |
| `marked` + `DOMPurify` | MIT | `markdown-previewer` |
| `PapaParse` | MIT | `csv-to-json`, `json-to-csv` |
| `Terser` | BSD-2 | `js-minifier` |

`DOMPurify` is not optional next to `marked` — rendering someone's markdown
straight into the page is an XSS hole.

---

## Performance at 108 tools

The good news: this architecture barely degrades. The homepage today is under
60 KB with no images at all — the icons are emoji and the favicon is an
inline SVG. Here is what to watch and what to ignore.

**Ignore:** `tools-data.js` growing. 108 objects is roughly 30 KB, smaller
than one photograph. It is fine. Do not split it.

**Watch these four:**

1. **Never make a global bundle.** Each tool's JavaScript stays in its own
   `<script data-tool>` at the bottom of its own page. Someone visiting the
   word counter must never download the PDF code. This is the whole reason
   the site will still be fast at 108 tools.

2. **Lazy-load the heavy libraries.** `pdf-lib` is around 400 KB. Do not put
   it in the `<head>` of a PDF tool page — load it the first time the user
   actually picks a file:

   ```js
   let pdfLib = null;
   async function getPdfLib() {
     if (!pdfLib) pdfLib = await import("../js/lib/pdf-lib.esm.js");
     return pdfLib;
   }
   ```

   The page then loads in 60 KB like every other page, and the 400 KB only
   arrives for someone who is definitely going to use it.

3. **Script the sitemap after about 30 tools.** Hand-editing `sitemap.xml`
   for 108 entries will eventually drift out of sync with the registry and
   `check.py` will start failing for boring reasons. Write
   `build-sitemap.py` that reads `js/tools-data.js` and regenerates the file,
   and run it before every upload.

4. **Extend `check.py` as you go.** It is already your safety net — it
   catches an unregistered tool, a missing sitemap entry, an over-length
   title and a dead link. When you add categories, add them there too. A
   check that has quietly stopped covering things is worse than no check.

**Targets to hold:** Lighthouse Performance 95+, homepage under 100 KB total,
every tool page interactive in under a second on a mid-range phone. Test on
a real phone on mobile data, not on your laptop.

---

## Maintenance

Most of these tools are finished forever once they work — a percentage
calculator has no news cycle. Three are not:

- `income-tax-calculator` — Indian slabs change with each Union Budget in
  February. Put it in your calendar, and stamp the page with "Updated for
  FY 2026-27" so visitors can trust it.
- `gst-calculator` — rate changes are occasional but real.
- `currency-converter` — the only tool with a live dependency. If the free
  API tier disappears the tool breaks silently, so show the rate's timestamp
  and handle a failed fetch with a visible message rather than a blank box.

Everything else: build it, verify it, move on.

---

## A note on the India angle

You own a `.in` domain and `emi-calculator` is already in Phase 1. That is a
real edge, so lean into it. `gst-calculator`, `sip-calculator`,
`salary-calculator`, `area-converter` (gunta, bigha, cent) and
`number-to-words` (lakh / crore) have serious monthly search volume and far
weaker competition than "word counter", where you are up against domains
fifteen years older than yours. Those tools are how a new site gets its first
thousand visitors. See `SEO.md`.
