# Getting 108toolbox.in online (free hosting, ~40 minutes)

Your domain is bought. Your files are already configured for it — every
canonical tag, the sitemap and robots.txt all say `https://108toolbox.in`.
There is nothing left to edit.

No command line needed. GitHub Pages hosts it free, forever, with free HTTPS.

---

## Step 0 — VERIFY YOUR DOMAIN EMAIL. Do this first, today.

GoDaddy is showing **"Your domain is pending WHOIS verification"**.

This is not a suggestion. Under ICANN rules the registrar must verify your
contact email, and **if you do not verify within 15 days the domain is
suspended** — it stops resolving, and your site goes dark no matter how
perfectly everything else is set up.

1. Click **Validate** on that yellow banner, or
2. Check the inbox of the email you registered with (including spam) for a
   message from GoDaddy with a verification link, and click it

The banner disappears and **Domain Status** changes from `IDLE` once it's done.
Do not skip this.

---

## Step 1 — Set up your email address

Your contact page says **hello@108toolbox.in**. Make it real before you
promote the site:

- **GoDaddy Email Forwarding** — in your domain's **Products** tab. Often
  included free with a `.in` registration. Forwards hello@108toolbox.in to
  your Gmail.
- **Cloudflare Email Routing** — free, unlimited addresses, but you'd have to
  move your DNS to Cloudflare. Worth it later, not now.

If you'd rather use a different address, run `python setup.py` again and
change it — the script detects the current values, so it's safe to re-run any
time.

---

## Step 2 — Create a GitHub account and repository

1. Sign up at **github.com** (free)
2. Click **+** top right → **New repository**
3. **Name**: `108toolbox` (lowercase, no spaces)
4. Set it **Public** — Pages won't serve a private repo on the free plan
5. Do **not** tick "Add a README"
6. **Create repository**

---

## Step 3 — Upload the files

On the empty repo page, click **uploading an existing file**.

> Open your `108-tools` folder, select **everything inside it**, and drag that
> in. Do **not** drag the `108-tools` folder itself.

GitHub must see `index.html` at the top level. If it lands at
`108-tools/index.html`, your site 404s.

Three files are easy to miss because your computer may hide them — make sure
these are uploaded too:

| File | What it does if missing |
|---|---|
| `CNAME` | GitHub forgets your custom domain on every upload |
| `.nojekyll` | Usually fine, but can break files starting with `_` |
| `robots.txt` | Crawlers get no sitemap pointer |

On Windows, turn on **View → Show → Hidden items** in File Explorer first.

Scroll down, click **Commit changes**.

---

## Step 4 — Turn Pages on

1. **Settings** (top row of the repo) → **Pages** (left sidebar)
2. **Source**: Deploy from a branch
3. **Branch**: `main`, folder `/ (root)` → **Save**
4. Under **Custom domain**, type `108toolbox.in` → **Save**

GitHub will say *"Domain's DNS record could not be verified"*. That's expected
— you haven't pointed the domain yet. Next step fixes it.

---

## Step 5 — Point the domain at GitHub (GoDaddy DNS)

In GoDaddy: **My Domains → 108toolbox.in → DNS**.

### First, delete what GoDaddy put there

GoDaddy pre-fills a parking page. Delete these two:

- The **A** record with Name `@` (points at a GoDaddy parking IP)
- The **CNAME** record with Name `www` (points to a GoDaddy domain)

Leave everything else — especially any `MX` or `TXT` records, which handle
email and verification.

### Then add these

Click **Add New Record** for each. Four A records, all Name `@`:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | @ | 185.199.108.153 | 1 hour |
| A | @ | 185.199.109.153 | 1 hour |
| A | @ | 185.199.110.153 | 1 hour |
| A | @ | 185.199.111.153 | 1 hour |
| CNAME | www | `YOURUSERNAME.github.io` | 1 hour |

Replace `YOURUSERNAME` with your actual GitHub username. Note the trailing
`.github.io` — no repository name, no `https://`, no trailing slash.

**Optional but recommended** — IPv6, so the site loads for mobile networks
that are IPv6-only:

| Type | Name | Value |
|---|---|---|
| AAAA | @ | 2606:50c0:8000::153 |
| AAAA | @ | 2606:50c0:8001::153 |
| AAAA | @ | 2606:50c0:8002::153 |
| AAAA | @ | 2606:50c0:8003::153 |

Save.

---

## Step 6 — Wait, then turn on HTTPS

DNS changes take **10 minutes to a few hours** to spread. Usually under an
hour with GoDaddy.

Check progress by opening `https://108toolbox.in` now and then. Once it loads:

1. Go back to **GitHub → Settings → Pages**
2. The custom domain now shows a green tick
3. Tick **Enforce HTTPS**

That last box is what gives you the padlock. It can stay greyed out for up to
24 hours while GitHub issues the certificate — that's normal, just come back.

**Your site is live at https://108toolbox.in**

---

## Step 7 — Tell Google

Without this, Google may not find you for weeks.

1. **search.google.com/search-console** → **Add property** → **URL prefix**
2. Enter `https://108toolbox.in`
3. Verify. Easiest here is the **TXT record** method, since you already have
   GoDaddy DNS open: copy the TXT value Google gives you, add it in GoDaddy
   DNS as Type `TXT`, Name `@`, then click Verify
4. Once verified: **Sitemaps** in the left sidebar → enter `sitemap.xml` →
   **Submit**

Repeat at **bing.com/webmasters** — two minutes, and it feeds DuckDuckGo and
several AI search tools.

---

## Step 8 — Confirm it all worked

| Check | How | Expect |
|---|---|---|
| WHOIS verified | GoDaddy domain page | No yellow banner |
| Site loads | `https://108toolbox.in` | Homepage, 8 tool cards |
| www works | `https://www.108toolbox.in` | Redirects to the non-www address |
| HTTPS | Address bar | Padlock, not "Not secure" |
| Tools work | Word Counter, type something | Counts update live |
| Phone | Open it on your phone | Single column, hamburger menu |
| Speed | pagespeed.web.dev | 95+ on mobile |
| Indexed | Google `site:108toolbox.in` | Pages listed — takes 3–7 days |

---

## Updating the site later

1. Edit the file on your computer
2. In the repo: **Add file → Upload files**, drag the changed files, **Commit**

Live in about a minute. Always run `python check.py` before uploading.

**If you ever change domain**, run `python setup.py` again with the new URL,
update `CNAME`, and re-upload. The script reads the current values, so
re-running is safe.

---

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Domain suddenly stops working | WHOIS never verified | Check email, click the GoDaddy link. Step 0. |
| "Domain's DNS record could not be verified" | DNS not propagated yet | Wait. Check at dnschecker.org for `108toolbox.in` |
| 404 on the homepage | Files uploaded inside a subfolder | Re-upload the folder's *contents* |
| Site loads unstyled | `css` folder missing | Confirm `css/style.css` is in the repo |
| Custom domain keeps resetting | `CNAME` file missing | Add a file named `CNAME` containing `108toolbox.in` |
| Enforce HTTPS greyed out | Certificate still issuing | Wait up to 24h, then remove and re-add the custom domain |
| www doesn't work | CNAME wrong | Must be `USERNAME.github.io` — no repo name |
| Old version showing | Browser cache | Ctrl+Shift+R |

---

## Want it live in 2 minutes instead?

**app.netlify.com/drop** — drag the folder on, get an instant URL. Good for
showing someone today while DNS propagates. GitHub Pages is the better
long-term home because you keep a history of every change.
