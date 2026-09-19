# 108 ToolBox — SEO and traffic

Written for someone who has never done SEO. Read the honest timeline first,
because it is the part that stops people from quitting in month two.

---

## The honest timeline

A brand-new domain with no history does not rank quickly. Nothing is wrong
with your site if this is what you see:

| When | What normally happens |
|---|---|
| Week 1–2 | Google finds and indexes your pages. Zero visitors. |
| Month 1–2 | A handful of impressions. Almost no clicks. Still normal. |
| Month 3–4 | First clicks, on long, specific phrases you never targeted. |
| Month 6 | 50–200 visitors a day **if** you kept publishing tools. |
| Month 12 | With all 108 tools live, a few thousand a day is realistic. |

The single biggest predictor of where you land is not clever SEO. It is
whether you were still shipping tools in month seven. Most people are not.

---

## How Google decides to rank a tool page

For a query like "word counter", Google is not looking for the best essay
about counting words. It is looking for a page where someone can count words
**immediately**. That is called matching search intent, and it changes what a
good page looks like:

- The tool must be visible without scrolling. Your pages already do this —
  the workspace sits directly under the `<h1>`, with the explanation below.
- The 300–600 words underneath ("How to use it" plus the FAQ) are what tells
  Google what the page is *about*. They are not filler; they are the ranking
  surface. Write them properly for every new tool.
- Never put a wall of text above the tool. Sites that do this lose to sites
  that do not, because visitors bounce straight back to the results.

Google also watches whether people who click your result stay, or bounce back
and click a competitor. A fast, clean, ad-free tool wins that comparison. You
already have that advantage — do not trade it away later for ad revenue.

---

## Keyword strategy

### Go long-tail first

"calculator" is unwinnable. "gst calculator with inclusive and exclusive
option" is winnable in a few months. The pattern is: **more words, more
specific, less competition, more likely to convert.**

Rank order for what to target:

1. **Tool name + modifier** — "word counter online free", "image compressor
   without losing quality", "percentage calculator with steps"
2. **Question form** — "how many words is a 5 minute speech", "how old am i
   if i was born in 2001"
3. **Regional** — "gst calculator india", "sip calculator with step up",
   "gunta to square feet"
4. **Bare tool name** — "word counter". Target this last. You will not win it
   in year one, and that is fine.

### How to find keywords without paying for a tool

- **Google autocomplete.** Type your tool name and read every suggestion.
  Then type it with a trailing "a", then "b", then "c" — different
  suggestions each time.
- **"People also ask"** on the results page. Every one of those is an FAQ
  entry for your tool page, and answering them well is how you show up in
  that box yourself.
- **"Related searches"** at the bottom of the results page.
- **Google Search Console**, once you have any traffic at all. Queries → sort
  by impressions with few clicks. Those are phrases where you are *almost*
  ranking. Improving that page is the cheapest traffic you will ever get.

One primary keyword per page. Do not build two pages for "percent calculator"
and "percentage calculator" — that splits your own strength between them.

### Do not keyword-stuff

Writing "word counter" nine times in one paragraph made pages rank in 2009
and gets them buried now. Use the phrase in the title, the `<h1>`, the first
sentence and once or twice naturally in the body. Then stop.

---

## On-page checklist for every new tool

Your template already handles most of this. Verify each one — `check.py`
enforces the starred items.

- [ ] **Title** ★ — under 60 characters, primary keyword first.
      `Word Counter — Count Words & Characters Free | 108 ToolBox`
- [ ] **Meta description** ★ — 140–155 characters. Google often rewrites it,
      but it still drives the click when shown.
- [ ] **Canonical tag** ★ — the full `https://108toolbox.in/tools/...` URL.
- [ ] **One `<h1>`**, containing the keyword, matching what the page does.
- [ ] **Slug = the searched phrase.** `image-resizer`, not `resize-tool`.
- [ ] **JSON-LD** ★ — `SoftwareApplication` + `FAQPage` + `BreadcrumbList`.
      The FAQ schema is what can win you an expanded result.
- [ ] **300–600 original words** in "How to use it" and the FAQ. Original
      means you wrote it. Do not spin a competitor's copy — Google is good at
      spotting that and it is also someone else's work.
- [ ] **Internal links** — the related-tools strip is already automatic; make
      sure the tool is in `tools-data.js` so it appears in other tools' strips.
- [ ] **Sitemap entry** ★.

---

## Technical setup (one hour, once)

1. **Google Search Console** — add `108toolbox.in`, verify via DNS TXT,
   submit `https://108toolbox.in/sitemap.xml`. `DEPLOY.md` Step 7 covers
   this.
2. **Bing Webmaster Tools** — it imports from Search Console in two clicks.
   Bing is maybe 3–5% of traffic, but it costs you two minutes and it also
   feeds DuckDuckGo and ChatGPT search.
3. **HTTPS on, enforced.** Already in `DEPLOY.md` Step 6.
4. **Analytics.** If you add any, pick a privacy-respecting one
   (Plausible, Umami, Cloudflare Web Analytics). Adding Google Analytics
   contradicts the "no tracking" line on your own homepage — either drop that
   claim or do not add GA. Do not do both.
5. **Re-submit the sitemap** whenever you ship a batch of tools.

Then leave it alone. Checking Search Console daily in month one is a way to
feel bad, not a strategy. Once a week is plenty.

---

## Where the first visitors actually come from

Google will not send you anyone for months. These will:

- **Answer real questions.** Find people on Reddit, Quora or a forum asking
  the exact thing your tool solves, and give them a genuine answer with the
  link as one part of it. Read each community's self-promotion rules first —
  drive-by link drops get you banned, and deservedly.
- **Product Hunt / Hacker News "Show HN"** — one shot each, so wait until you
  have 25–30 solid tools. Lead with the real hook: everything runs in the
  browser, nothing is uploaded, no ads, no sign-up. That is genuinely
  unusual and it is what people will share.
- **Your own circles.** WhatsApp groups, college groups, your LinkedIn. The
  first hundred visitors are almost always people who know you.
- **Short video.** A 30-second screen recording of the image compressor
  halving a file size is very cheap to make and does well on Shorts, Reels
  and Twitter/X.
- **Be genuinely linkable.** The one asset that earns links without asking is
  a tool that does something everyone else charges for or gates behind a
  sign-up. Keep finding those.

Do not buy backlinks, do not join link-exchange schemes, do not post spun
articles to directory sites. Those all worked once and are now penalties.

---

## What to measure

Ignore: total pageviews, bounce rate in isolation, and your ranking for any
single keyword checked manually.

Watch monthly, in Search Console:

1. **Total impressions** — are more of your pages being *seen* in results?
   This moves first, long before clicks.
2. **Total clicks** — the real number.
3. **Pages with impressions but no clicks** — your title or description is
   not compelling. Rewrite it; this is the fastest win available.
4. **Pages ranking 8–20** — nearly there. Expand the FAQ, add internal links
   from related tools, improve the page. Far easier than starting a new one.
5. **Indexed page count** — should climb in step with your tool count. If a
   new tool never gets indexed, something is broken; check the sitemap and
   the canonical tag first.

---

## The three things that matter most

Everything above is detail. If you only do three things:

1. **Ship tools consistently.** 108 good pages beats 10 perfect ones. Volume
   of genuinely useful pages is the whole strategy.
2. **Make every page fast and put the tool at the top.** You already win here
   against most competitors. Protect it.
3. **Write the FAQ yourself, for every single tool.** Four real questions,
   answered plainly, are worth more than any technical SEO trick — and they
   are the part a competitor cannot copy from you without it being obvious.
