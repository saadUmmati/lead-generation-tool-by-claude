"""
site_summary.py — turns the pages already fetched during enrichment into a
compact, factual summary that can later be handed to an AI writer.

Pure BeautifulSoup, no network. It never fetches anything itself.

Output fields (all strings/ints, CSV friendly):
  site_title, site_description, site_headings, site_text_excerpt,
  site_text_chars, site_signals, site_platform, site_language,
  site_pages_read, site_status

site_status:
  ok                 readable text found
  thin               some text, but too little to write a specific pitch
  blocked_or_parked  bot-challenge page or a parked/for-sale domain
  none               nothing fetched
Signals mean "detected present". Absence means "not detected", never "does not have".
"""

import re

from bs4 import BeautifulSoup

SUMMARY_FIELDS = [
    "site_title", "site_description", "site_headings", "site_text_excerpt",
    "site_text_chars", "site_signals", "site_platform", "site_language",
    "site_pages_read", "site_status",
]

MAX_EXCERPT = 1800
MAX_HEADINGS = 14
MIN_READABLE_CHARS = 300
PHRASE_CHECK_BELOW = 1200

JUNK_TAGS = ["script", "style", "noscript", "svg", "iframe", "template",
             "nav", "footer", "aside", "form"]
JUNK_HINT_RE = re.compile(r"cookie|consent|gdpr|banner-cookie|cc-window|popup|modal", re.I)

CHALLENGE_PHRASES = [
    "checking your browser", "verify you are human", "just a moment",
    "enable javascript and cookies", "attention required", "access denied",
    "are you a robot", "captcha", "ddos protection",
]
PARKED_PHRASES = [
    "this domain is for sale", "domain is for sale", "buy this domain",
    "domain parking", "parked free", "this domain may be for sale",
    "website coming soon", "under construction", "account suspended",
    "default web site page", "index of /",
]

PLATFORM_HINTS = [
    ("wordpress", ["wp-content", "wp-includes"]),
    ("wix", ["wixstatic.com", "wix.com"]),
    ("squarespace", ["squarespace.com", "squarespace-cdn"]),
    ("shopify", ["cdn.shopify.com", "myshopify.com"]),
    ("webflow", ["webflow.com", "webflow.io"]),
    ("godaddy", ["websitebuilder.godaddy", "secureserver.net"]),
    ("weebly", ["weebly.com"]),
    ("joomla", ["/media/jui/", "joomla"]),
    ("drupal", ["drupal-settings-json", "/sites/default/files"]),
]

SIGNAL_RULES = {
    "online_booking": ["book online", "book now", "book an appointment", "online booking",
                       "schedule an appointment", "calendly.com", "booksy", "opentable",
                       "resy.com", "simplybook", "setmore", "acuityscheduling", "fresha.com"],
    "live_chat_or_chatbot": ["livechat", "tawk.to", "intercom", "drift.com", "crisp.chat",
                             "tidio", "zopim", "hubspot.com/conversations", "chatbot", "live chat"],
    "ecommerce": ["add to cart", "add to basket", "checkout", "woocommerce", "cdn.shopify.com",
                  "/cart", "shopping cart"],
}


def empty_summary() -> dict:
    d = {k: "" for k in SUMMARY_FIELDS}
    d["site_text_chars"] = 0
    d["site_pages_read"] = 0
    d["site_status"] = "none"
    return d


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(JUNK_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        attrs = " ".join([" ".join(tag.get("class", []) or []), tag.get("id", "") or ""])
        if attrs.strip() and JUNK_HINT_RE.search(attrs):
            tag.decompose()
    return _clean(soup.get_text(" "))


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return ""


def summarize_site(pages: dict, rendered_js: bool = False) -> dict:
    """pages: {url: html}. Returns a dict with SUMMARY_FIELDS keys."""
    out = empty_summary()
    if not pages:
        return out

    texts, headings = [], []
    raw_all = []
    title = description = lang = ""

    for idx, (_url, html) in enumerate(pages.items()):
        if not html:
            continue
        raw_all.append(html.lower())
        soup = BeautifulSoup(html, "html.parser")
        if idx == 0:
            title = _clean(soup.title.get_text()) if soup.title else ""
            description = _meta(soup, "description", "og:description")
            htmltag = soup.find("html")
            lang = (htmltag.get("lang", "") if htmltag else "") or ""
        for h in soup.find_all(["h1", "h2", "h3"]):
            t = _clean(h.get_text(" "))
            if 3 <= len(t) <= 140 and t not in headings:
                headings.append(t)
        texts.append(_visible_text(soup))

    blob = " ".join(raw_all)
    full_text = _clean(" ".join(texts))
    chars = len(full_text)

    signals = [name for name, needles in SIGNAL_RULES.items() if any(n in blob for n in needles)]
    if "<form" in blob:
        signals.append("contact_form")
    platform = next((p for p, needles in PLATFORM_HINTS if any(n in blob for n in needles)), "")

    status = "ok"
    low = full_text.lower()
    if chars < PHRASE_CHECK_BELOW and (
        any(p in low for p in CHALLENGE_PHRASES) or any(p in low for p in PARKED_PHRASES)
    ):
        status = "blocked_or_parked"
    elif chars < MIN_READABLE_CHARS:
        status = "thin"

    out.update({
        "site_title": title[:160],
        "site_description": description[:300],
        "site_headings": " | ".join(headings[:MAX_HEADINGS]),
        "site_text_excerpt": full_text[:MAX_EXCERPT],
        "site_text_chars": chars,
        "site_signals": ", ".join(signals),
        "site_platform": platform,
        "site_language": lang.split("-")[0].lower()[:5],
        "site_pages_read": len([h for h in pages.values() if h]),
        "site_status": status,
    })
    return out
