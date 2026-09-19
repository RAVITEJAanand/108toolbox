# 108 ToolBox — Phase 1 (10 tools)

Live domain: **108toolbox.in** (already configured in every file)

Plain HTML, CSS and JavaScript. No build step, no npm, no framework.

## Run it locally

Open `index.html` in your browser — that is genuinely all it takes.

A local server is nicer though (clipboard copy needs a secure context):

    python3 -m http.server 8000
    # then open http://localhost:8000

## Put it online (free)

**GitHub Pages**
1. Create a repo, upload every file in this folder.
2. Settings → Pages → Source: `main`, folder `/root`. Save.
3. Live at `https://<you>.github.io/<repo>/` in about a minute.

**Netlify** — drag this whole folder onto app.netlify.com/drop. Done.

## Before you go live

Already done for 108toolbox.in. Only re-run this if you change domain,
site name or contact email — it detects the current values, so it is safe
to run again:

    python setup.py      # Windows
    python3 setup.py     # Mac / Linux

It asks for your site name, live URL, contact email and logo mark, then
rewrites all 16 pages, the sitemap and robots.txt (~76 edits).

Then verify:

    python check.py

`check.py` fails if any placeholder is left, if a tool is missing from
`tools-data.js` or the sitemap, if a title or meta description is the wrong
length, if a page has no canonical tag or has broken JSON-LD, or if any
internal link is dead. Run it before every upload.

Full step-by-step instructions: see **DEPLOY.md**.

## Add a new tool (about 20 minutes)

1. Copy `tools/word-counter.html` → `tools/your-tool.html`
2. Change the `<title>`, `<meta name="description">`, `<link rel="canonical">`,
   the breadcrumb, the `<h1>` and the lead paragraph.
3. Replace everything between `<!--TOOL:START-->` and `<!--TOOL:END-->`.
4. Replace the code inside `<script data-tool>` at the bottom.
5. Rewrite "How to use it" and the FAQ block. Keep the JSON-LD in sync.
6. Set `window.CURRENT_TOOL = "your-tool";`
7. **Add one object to `js/tools-data.js`** — this is what makes it appear
   on the homepage, in the grid, in search and in related-tool strips.
8. Add one `<url>` entry to `sitemap.xml`.

Never skip step 7. That file is the spine of the whole site.

## Files

    index.html        Homepage
    tools.html        All tools, searchable and filterable
    about/privacy/contact/404.html
    robots.txt, sitemap.xml
    css/style.css     Design system: tokens, navbar, footer, cards, buttons
    css/tool.css      Tool pages only: panels, stat tiles, drop zones, tables
    js/tools-data.js  THE REGISTRY — one object per tool
    js/main.js        Renders grids, runs search, mobile menu
    js/tool-helpers.js  copyText, downloadBlob, showToast, formatBytes
    tools/*.html      One file per tool
    CNAME             Tells GitHub Pages your domain is 108toolbox.in
    setup.py          One-time: set your name, URL and email everywhere
    check.py          Pre-deploy checks — run before every upload
    DEPLOY.md         Step-by-step guide to getting online free
    ROADMAP.md        The plan for tools 11-108 — what to build, in what order
    SEO.md            How to get traffic and rank — beginner playbook
