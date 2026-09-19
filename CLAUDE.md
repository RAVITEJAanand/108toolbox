# 108 ToolBox — context for Claude

Read this first. It is loaded automatically in every session opened in this
folder, so you never need to be told the project's history again.

**What this is:** a static site of free browser-based utilities, live at
<https://108toolbox.in>. Plain HTML, CSS and JavaScript. No framework, no npm,
no build step, and it stays that way.

**Where it stands:** 20 of a planned 108 tools are built, tested and live.

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

### 2. Bump `?v=` whenever you touch `css/` or `js/`

Every page links its assets like this:

```html
<link rel="stylesheet" href="css/style.css?v=2">
```

GitHub Pages sends `Cache-Control: max-age=600`. Without a new version stamp a
returning visitor keeps the old stylesheet and swears nothing changed — this
already happened once, with the redesign. Find-and-replace `?v=2` → `?v=3`
across all 27 pages. `check.py` understands the stamp.

### 3. `check.py` passing does NOT mean the tool works

It validates markup, titles, meta lengths, canonicals, JSON-LD, the registry
and internal links. **It never runs your JavaScript.** Always open the page in
a browser and actually use the tool before deploying.

### 4. Never type a `\uXXXX` escape into a tool's `<script>`

It can land in the file as the literal character. U+2028 and U+2029 are
JavaScript line terminators, so a regex literal containing one does not parse —
the file looks perfect in an editor and the entire tool silently does nothing.
Build the pattern from code points instead; `tools/whitespace-remover.html`
shows the pattern.

### 5. Nothing is ever uploaded

Every tool runs client-side — canvas, Web Crypto, `pdf-lib`. The homepage
promises "nothing is uploaded, nothing is stored" and that promise must stay
literally true. It is the site's only real differentiator. See the
"Tools to never build" section of `ROADMAP.md` before adding anything that
touches a server, a third-party API, or someone else's content.

### 6. Do not add Google Analytics

The homepage says "no tracking". If analytics are ever wanted, use Plausible,
Umami or Cloudflare Web Analytics — or drop the claim. Not both.

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
check.py           Pre-deploy validation — run before every push
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

**Open item — HTTPS is broken, not merely slow.** Checked 19 Sep 2026: the
server still answers with GitHub's generic `CN=*.github.io` certificate, so no
certificate has ever been issued for `108toolbox.in` and `https://` does not
work. DNS is correct, and this has not moved in hours — a stalled request, not
a slow one. The fix is to remove the custom domain in the repo's Pages settings
and add it straight back, which re-triggers the request (~30 seconds of
downtime). **Enforce HTTPS** can only be ticked afterwards.

Check what the server actually serves — no `gh` login needed, and it reports
reality rather than GitHub's own status field:

```bash
echo | openssl s_client -servername 108toolbox.in -connect 108toolbox.in:443 \
  2>/dev/null | openssl x509 -noout -subject
```

`CN=*.github.io` means still broken. `CN=108toolbox.in` means fixed.
(The `gh api .../pages --jq '.https_certificate.state'` route also works, but
`gh` stores its login in the Windows keyring and some terminals cannot reach
it, reporting "not logged in" when you are.)

---

## What is built, and what is next

**Live (20):** word-counter, case-converter, lorem-ipsum-generator,
image-compressor, image-converter, percentage-calculator, age-calculator,
emi-calculator, password-generator, json-formatter, remove-duplicate-lines,
find-and-replace, sort-text-lines, remove-line-breaks, whitespace-remover,
reverse-text, text-repeater, add-line-numbers, slug-generator,
character-frequency-counter.

**Next batch (Phase 2, tools 21–25):** the easy calculators —
`bmi-calculator`, `discount-calculator`, `tip-calculator`,
`average-calculator`, `ratio-calculator`. Text is now 13 of its 16, and the
three left (`readability-score`, `text-diff-checker`, `text-to-speech`) are all
medium, so this is the moment to switch category rather than push through.
Full plan in `ROADMAP.md`.

**Categories expand from 4 to 8 at the 30-tool mark.** `ROADMAP.md` names the
exact three files that change.

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
