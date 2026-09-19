#!/usr/bin/env python3
"""
108 ToolBox — one-time setup.

Replaces every placeholder (site name, domain, contact email) across the whole
project in one go, so you never have to hunt for them by hand.

USAGE
  Windows:  python setup.py
  Mac/Linux: python3 setup.py

It will ask you three questions. You can also pass them as arguments:

  python setup.py --name "QuickTools" --url "https://raviteja.github.io/quicktools" \
                  --email "you@gmail.com"

Safe to run more than once: it always rewrites from the current values, and it
tells you exactly what it changed. Run check.py afterwards to confirm nothing
was missed.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent

# ---- What we are replacing -------------------------------------------------
# These are DETECTED from the current files, not hard-coded, so this script
# still works after it has already been run once. Change your domain later and
# just run it again.

def detect():
    """Read the current name, URL, email and logo out of the project."""
    index = (ROOT / "index.html").read_text(encoding="utf-8")
    robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
    contact = (ROOT / "contact.html").read_text(encoding="utf-8")

    # Base URL lives in robots.txt as "Sitemap: <base>/sitemap.xml"
    m = re.search(r"Sitemap:\s*(\S+)/sitemap\.xml", robots)
    url = m.group(1) if m else "https://example.com"

    # Email from the contact page's mailto: link
    m = re.search(r"mailto:([^\"'>\s]+)", contact)
    email = m.group(1) if m else "hello@example.com"

    # Brand name from the footer copyright line
    m = re.search(r"&copy;\s*\d{4}\s*([^<]+?)\s*</span>", index)
    name = m.group(1).strip() if m else "108 ToolBox"

    # Logo: [mark] word
    m = re.search(r'<span class="logo__mark">([^<]*)</span><span>([^<]*)</span>', index)
    mark, word = (m.group(1), m.group(2)) if m else ("108", "ToolBox")

    return name, word, mark, url, email


OLD_NAME, OLD_WORD, OLD_MARK, OLD_URL, OLD_EMAIL = detect()

# The factory defaults. We always replace these AS WELL as whatever was
# detected, because files can fall out of step — regenerating only the HTML
# pages, for instance, puts example.com back in them while robots.txt and
# sitemap.xml still hold the real domain. Detection alone would then see
# "already correct" and silently skip every page.
FACTORY_NAME  = "108 ToolBox"
FACTORY_WORD  = "ToolBox"
FACTORY_URL   = "https://example.com"
FACTORY_EMAIL = "hello@example.com"

# Every file this script rewrites
TARGETS = (
    list(ROOT.glob("*.html"))
    + list((ROOT / "tools").glob("*.html"))
    + [ROOT / "robots.txt", ROOT / "sitemap.xml", ROOT / "README.md"]
)


def ask(prompt, default=""):
    """Prompt with a default shown in brackets."""
    suffix = " [%s]: " % default if default else ": "
    try:
        answer = input(prompt + suffix).strip()
    except EOFError:
        answer = ""
    return answer or default


def normalise_url(url):
    """Strip a trailing slash so we never produce '...//tools/x.html'."""
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--name",  help='Site name, e.g. "QuickTools"')
    parser.add_argument("--url",   help='Live base URL, no trailing slash')
    parser.add_argument("--email", help="Contact email")
    parser.add_argument("--mark",  help="1-3 characters for the square logo (default: 108)")
    args = parser.parse_args()

    print("\n" + "=" * 62)
    print("  108 ToolBox setup")
    print("=" * 62)

    name = args.name or ask(
        "\n1. Site name\n   The brand shown in the title bar, logo and footer.\n   Name",
        OLD_NAME)

    print("\n2. Live URL")
    print("   GitHub Pages project site: https://USERNAME.github.io/REPO")
    print("   Your own domain:           https://yourdomain.com")
    url = normalise_url(args.url or ask("   URL", OLD_URL))

    email = args.email or ask(
        "\n3. Contact email\n   Shown on the contact page.\n   Email", OLD_EMAIL)

    # ---- The logo is [MARK] WORD, e.g. [108] ToolBox ----------------------
    # Work out a sensible mark from the name so the two halves never repeat
    # each other. "108 ToolBox" -> [108] ToolBox.  "QuickTools" -> [Q] QuickTools.
    def derive(site_name):
        parts = site_name.split()
        first = parts[0] if parts else site_name
        if len(parts) > 1 and first.isdigit():
            # A NUMBER in front is the mark: "108 ToolBox" -> [108] ToolBox.
            # Only numbers, so "Ravi Tools" does not become [Ravi] Tools.
            return first[:4], site_name[len(first):].strip()
        # Otherwise: initials as a badge beside the full wordmark
        initials = "".join(w[0] for w in parts)[:3].upper() or site_name[:1].upper()
        return initials, site_name

    auto_mark, auto_word = derive(name)

    if args.mark:
        mark = args.mark.strip()[:4]
    else:
        print("\n4. Logo mark")
        print("   The 1-3 characters inside the blue square, shown as [%s] %s"
              % (auto_mark, auto_word))
        mark = ask("   Mark", auto_mark).strip()[:4] or auto_mark

    # Strip the mark from the word ONLY when it is a whole leading token
    # ("108 ToolBox" -> [108] ToolBox), never a bare prefix, or "QuickTools"
    # with mark "Q" would render as [Q] uickTools.
    word = (name[len(mark):].strip()
            if name.lower().startswith(mark.lower() + " ")
            else auto_word)
    if not word:
        word = name

    print("\n" + "-" * 62)
    print("  Name  : %s" % name)
    print("  Logo  : [%s] %s" % (mark, word))
    print("  URL   : %s" % url)
    print("  Email : %s" % email)
    print("-" * 62)
    if ask("\nApply these changes? (y/n)", "y").lower() not in ("y", "yes"):
        print("Cancelled. Nothing was changed.")
        return 1

    changed_files = 0
    total_edits = 0

    for path in TARGETS:
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        text = original

        # 1. URLs first. Doing this before the name keeps a name containing
        #    "example.com" (unlikely, but possible) from breaking the URLs.
        #    Longest first, so a detected URL that contains the factory one
        #    cannot be half-replaced.
        for old_url in sorted({OLD_URL, FACTORY_URL}, key=len, reverse=True):
            if old_url and old_url != url:
                text = text.replace(old_url, url)

        # 2. Email
        for old_email in {OLD_EMAIL, FACTORY_EMAIL}:
            if old_email and old_email != email:
                text = text.replace(old_email, email)

        # 3. The logo: <span class="logo__mark">108</span><span>ToolBox</span>
        text = re.sub(
            r'(<span class="logo__mark">)[^<]*(</span>)',
            lambda m: m.group(1) + mark + m.group(2),
            text)
        for old_word in {OLD_WORD, FACTORY_WORD}:
            if old_word and old_word != word:
                text = text.replace("<span>%s</span>" % old_word, "<span>%s</span>" % word)

        # 4. The full brand name everywhere else (titles, footer, About copy)
        for old_name in sorted({OLD_NAME, FACTORY_NAME}, key=len, reverse=True):
            if old_name and old_name != name:
                text = text.replace(old_name, name)

        if text != original:
            # Count the edits so the summary is honest
            edits = sum(original.count(old) for old in
                        {OLD_URL, FACTORY_URL, OLD_EMAIL, FACTORY_EMAIL, OLD_NAME, FACTORY_NAME})
            path.write_text(text, encoding="utf-8")
            changed_files += 1
            total_edits += edits
            print("  updated  %s" % path.relative_to(ROOT))

    # GitHub Pages runs Jekyll by default, which ignores files starting with
    # an underscore. This empty file switches that off. Harmless elsewhere.
    (ROOT / ".nojekyll").write_text("")

    print("\nDone. %d files updated, %d placeholders replaced." % (changed_files, total_edits))
    print("Added .nojekyll (tells GitHub Pages to serve every file as-is).")
    print("\nNext: run  python check.py  to confirm nothing was missed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
