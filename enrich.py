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

from email_extract import extract_page, contact_candidates, sitemap_contact_urls, PROBE_PATHS
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


async def fetch_ex(client: httpx.AsyncClient, url: str):
    """Returns (html | None, note). note says why a fetch failed, so the CSV
    can show it. A 403/429 is respected: we report it and do not work around it."""
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.ConnectError as exc:
        msg = str(exc).upper()
        return None, "SSL certificate problem" if ("CERTIFICATE" in msg or "SSL" in msg) else "connection error"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return None, type(exc).__name__
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    if "html" not in resp.headers.get("content-type", "").lower():
        return None, "not an HTML page"
    return resp.text, ""


async def fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    """Plain GET for small non-HTML public files such as sitemap.xml."""
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    return resp.text[:500_000] if resp.status_code == 200 else None


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    return (await fetch_ex(client, url))[0]


def _empty_result() -> dict:
    result = {
        "extra_emails": "", "email_sources": "", "facebook": "", "instagram": "",
        "linkedin": "", "whatsapp": "", "x_twitter": "", "youtube": "", "tiktok": "",
    }
    result.update(empty_summary())
    return result


RETRY_HTTP_NOTES = ("SSL certificate problem", "connection error")


async def enrich_one_bounded(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict,
                              browser_pool=None) -> dict:
    """Same as enrich_one, but guarantees this business can never stall the
    whole batch beyond its time budget, even if a site hangs in a way
    individual request timeouts don't catch (slow trickle, etc)."""
    budget = PER_BUSINESS_BUDGET_JS if browser_pool else PER_BUSINESS_BUDGET
    try:
        return await asyncio.wait_for(enrich_one(client, sem, row, browser_pool), timeout=budget)
    except asyncio.TimeoutError:
        res = _empty_result()
        res["site_fetch_note"] = "stalled (time budget reached)"
        return res


async def enrich_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict,
                      browser_pool=None) -> dict:
    result = _empty_result()

    website = normalize_url(row.get("website", ""))
    if not website:
        return result

    async with sem:
        base_html, note = await fetch_ex(client, website)
        if base_html is None and website.startswith("https://") and note in RETRY_HTTP_NOTES:
            alt = "http://" + website[len("https://"):]
            base_html, note2 = await fetch_ex(client, alt)
            if base_html is not None:
                website, note = alt, ""
            else:
                note = f"{note}; http retry: {note2}"
        if base_html is None:
            result["site_fetch_note"] = note
            return result

        pages_to_check = {website: base_html}
        all_emails: dict[str, str] = {}
        socials: dict[str, str] = {}

        def absorb(page_url: str, html: str, tag: str = "") -> None:
            found_e, found_s = extract_page(html, page_url)
            for addr, how in found_e.items():
                if addr not in all_emails:
                    note = f"{how}{tag}" if how != "mailto link" else tag.strip(" ,()") or "mailto"
                    all_emails[addr] = f"{page_url} ({note})" if note else page_url
            for platform, link in found_s.items():
                socials.setdefault(platform, link)

        absorb(website, base_html)

        # contact / about / legal pages linked from the homepage (many languages), same site only
        for extra_url in contact_candidates(base_html, website, limit=4):
            html = await fetch(client, extra_url)
            if html:
                pages_to_check[extra_url] = html
                absorb(extra_url, html)

        # Still no address anywhere? Many sites don't link their contact page from the homepage
        # (JS menus) - try the few conventional public paths, and the site's own sitemap.xml.
        # A handful of extra GETs, only for sites that would otherwise yield nothing; a 403/404 is
        # simply the end of that road.
        if not all_emails:
            origin = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
            tried = 0
            sm = await fetch_text(client, origin + "/sitemap.xml")
            probe = sitemap_contact_urls(sm or "", website, limit=2)
            probe += [origin + p for p in PROBE_PATHS]
            for u in dict.fromkeys(probe):
                if u in pages_to_check or tried >= 4 or all_emails:
                    continue
                tried += 1
                html = await fetch(client, u)
                if html:
                    pages_to_check[u] = html
                    absorb(u, html)

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
                absorb(website, rendered_html, tag=", JS-rendered")

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
