"""
enrich.py — adds social profile links and extra public emails to a
gosom leads CSV by crawling each business's own website.

Usage:
    python enrich.py --input leads.csv --output enriched.csv

What this does (and does not do):
  - Fetches each business's website homepage + a few likely internal
    pages (contact, about, team, impressum) — all publicly reachable,
    no login, no bypassing of any access control.
  - Extracts: mailto: links, plain-text emails, and lightly obfuscated
    emails ("name (at) domain (dot) com" style).
  - Extracts links to facebook/instagram/linkedin/whatsapp/x(twitter)/
    youtube/tiktok found in the page's own HTML.
  - Does NOT log into any social platform, does NOT scrape the social
    platforms' own pages beyond a single best-effort public fetch (many
    will just redirect to a login wall and yield nothing beyond the
    link itself — that's expected and not a bug).
  - Is polite: limited concurrency, timeouts, and skips businesses with
    no website at all.
"""

import argparse
import asyncio
import csv
import re
import sys
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from site_summary import SUMMARY_FIELDS, empty_summary, summarize_site

CONCURRENCY = 8
TIMEOUT = 8.0
PER_BUSINESS_BUDGET = 25.0        # hard cap for the fast (no-JS) path
PER_BUSINESS_BUDGET_JS = 45.0     # higher cap when the JS-render fallback may kick in
INTERNAL_PAGE_HINTS = [
    "contact", "about", "team", "impressum", "locations", "staff",
    "get-in-touch", "getintouch", "reach-us", "reach", "connect",
    "our-team", "meet-the-team", "find-us", "customer-service",
    "enquir", "inquir",  # matches enquiry/enquiries/inquiry/inquiries
]

CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')

SOCIAL_DOMAINS = {
    "facebook": ["facebook.com", "fb.com"],
    "instagram": ["instagram.com"],
    "linkedin": ["linkedin.com"],
    "whatsapp": ["wa.me", "api.whatsapp.com", "whatsapp.com"],
    "x_twitter": ["twitter.com", "x.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "tiktok": ["tiktok.com"],
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# catches "name (at) domain (dot) com" / "name [at] domain [dot] com" variants
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\(\[]?\s*(?:at|AT)\s*[\)\]]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\(\[]?\s*(?:dot|DOT)\s*[\)\]]?\s*"
    r"([a-zA-Z]{2,})"
)

GENERIC_EXCLUDE = {"example.com", "sentry.io", "wixpress.com", "godaddy.com"}


def normalize_url(url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    return url


def classify_social(href: str) -> str | None:
    href_l = href.lower()
    for platform, domains in SOCIAL_DOMAINS.items():
        for d in domains:
            if d in href_l:
                return platform
    return None


def extract_emails(text: str) -> set[str]:
    found = set()
    for m in EMAIL_RE.findall(text):
        domain = m.split("@")[-1].lower()
        if domain not in GENERIC_EXCLUDE:
            found.add(m.strip(".,;:"))
    for m in OBFUSCATED_RE.findall(text):
        candidate = f"{m[0]}@{m[1]}.{m[2]}"
        found.add(candidate.lower())
    return found


def decode_cf_email(encoded: str) -> str | None:
    """Decodes Cloudflare's email-obfuscation format (data-cfemail="...").
    This is a well-documented, simple XOR scheme: the first byte is the
    key, every following byte is XORed against it to recover the address.
    Not a bypass of anything - the browser decodes this in plain
    JavaScript for every visitor; we're just doing the same free, public
    algorithm without needing to actually run that JavaScript."""
    try:
        key = int(encoded[:2], 16)
        decoded = bytes(
            int(encoded[i:i + 2], 16) ^ key
            for i in range(2, len(encoded), 2)
        )
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def extract_cf_protected_emails(html: str) -> set[str]:
    found = set()
    for encoded in CF_EMAIL_RE.findall(html):
        decoded = decode_cf_email(encoded)
        if decoded and "@" in decoded:
            found.add(decoded.lower())
    return found


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    return None


def _empty_result() -> dict:
    result = {
        "extra_emails": "", "email_sources": "", "facebook": "", "instagram": "",
        "linkedin": "", "whatsapp": "", "x_twitter": "", "youtube": "", "tiktok": "",
    }
    result.update(empty_summary())
    return result


async def enrich_one_bounded(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict,
                              browser_pool=None) -> dict:
    """Same as enrich_one, but guarantees this business can never stall the
    whole batch beyond its time budget, even if a site hangs in a way
    individual request timeouts don't catch (slow trickle, etc)."""
    budget = PER_BUSINESS_BUDGET_JS if browser_pool else PER_BUSINESS_BUDGET
    try:
        return await asyncio.wait_for(enrich_one(client, sem, row, browser_pool), timeout=budget)
    except asyncio.TimeoutError:
        return _empty_result()


async def enrich_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict,
                      browser_pool=None) -> dict:
    result = _empty_result()

    website = normalize_url(row.get("website", ""))
    if not website:
        return result

    async with sem:
        base_html = await fetch(client, website)
        if base_html is None:
            return result

        pages_to_check = {website: base_html}
        soup = BeautifulSoup(base_html, "html.parser")
        parsed_base = urlparse(website)

        # find internal links matching contact/about/etc hints, same domain only
        internal_candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(website, href)
            parsed = urlparse(full)
            if parsed.netloc != parsed_base.netloc:
                continue
            path_l = parsed.path.lower()
            if any(hint in path_l for hint in INTERNAL_PAGE_HINTS):
                internal_candidates.append(full)

        # limit to a few extra pages to stay polite
        for extra_url in list(dict.fromkeys(internal_candidates))[:4]:
            html = await fetch(client, extra_url)
            if html:
                pages_to_check[extra_url] = html

        all_emails: dict[str, str] = {}
        socials: dict[str, str] = {}

        for page_url, html in pages_to_check.items():
            page_soup = BeautifulSoup(html, "html.parser")

            for a in page_soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip()
                    if addr and addr not in all_emails:
                        all_emails[addr] = page_url
                    continue
                platform = classify_social(href)
                if platform and platform not in socials:
                    socials[platform] = urljoin(page_url, href)

            for addr in extract_emails(page_soup.get_text(" ")):
                if addr not in all_emails:
                    all_emails[addr] = page_url
            for addr in extract_cf_protected_emails(html):
                if addr not in all_emails:
                    all_emails[addr] = f"{page_url} (Cloudflare-protected)"

        # Fallback: if the fast path found nothing at all, and a browser
        # pool was provided, render the homepage with real JavaScript and
        # try again - this catches sites whose contact info is injected
        # client-side and never appears in the plain HTML response.
        # Also render when the page is "thin" (likely JS-built) so the pitch
        # writer gets real text. Never for blocked/parked pages: we do not
        # try to get around bot protection.
        summary = summarize_site(pages_to_check)
        rendered_html = None
        need_js = ((not all_emails and not socials) or summary["site_status"] == "thin") \
            and summary["site_status"] != "blocked_or_parked"
        if need_js and browser_pool is not None:
            rendered_html = await browser_pool.render(website)
            if rendered_html:
                render_soup = BeautifulSoup(rendered_html, "html.parser")
                for a in render_soup.find_all("a", href=True):
                    href = a["href"]
                    if href.lower().startswith("mailto:"):
                        addr = href[7:].split("?")[0].strip()
                        if addr and addr not in all_emails:
                            all_emails[addr] = f"{website} (JS-rendered)"
                        continue
                    platform = classify_social(href)
                    if platform and platform not in socials:
                        socials[platform] = urljoin(website, href)
                for addr in extract_emails(render_soup.get_text(" ")):
                    if addr not in all_emails:
                        all_emails[addr] = f"{website} (JS-rendered)"
                for addr in extract_cf_protected_emails(rendered_html):
                    if addr not in all_emails:
                        all_emails[addr] = f"{website} (JS-rendered, Cloudflare-protected)"

        if rendered_html:
            summary = summarize_site({**pages_to_check, website + "#js": rendered_html}, rendered_js=True)
        result.update(summary)
        result["extra_emails"] = "; ".join(sorted(all_emails.keys()))
        result["email_sources"] = "; ".join(f"{e} ({u})" for e, u in all_emails.items())
        for platform, link in socials.items():
            if platform in result:
                result[platform] = link

    return result


async def run(input_path: str, output_path: str) -> None:
    with open(input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    extra_fields = [
        "extra_emails", "email_sources", "facebook", "instagram",
        "linkedin", "whatsapp", "x_twitter", "youtube", "tiktok",
    ] + SUMMARY_FIELDS
    for field in extra_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    sem = asyncio.Semaphore(CONCURRENCY)
    done_count = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0)"}
    ) as client:
        async def _track(row):
            nonlocal done_count
            res = await enrich_one_bounded(client, sem, row)
            done_count += 1
            print(f"[{done_count}/{len(rows)}] enriched: {row.get('title', '')}", file=sys.stderr)
            return res

        results = await asyncio.gather(*[_track(row) for row in rows])

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row, extra in zip(rows, results):
            merged = dict(row)
            merged.update(extra)
            writer.writerow(merged)

    print(f"Done. Wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.input, args.output))
