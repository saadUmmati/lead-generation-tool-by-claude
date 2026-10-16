"""
email_extract.py — pulls public contact emails and social links out of ONE page of HTML.

Pure functions (bs4 + stdlib only, no network), so they are fully testable. enrich.py
decides which pages to fetch; this module decides what is on them.

Everything here reads text a site publishes for every visitor: mailto links, visible
text, light obfuscation ("name [at] domain [dot] com", "&#64;", Cloudflare's XOR
scheme), structured data (JSON-LD / microdata) and addresses sitting in the page's
own script blobs. Nothing is decrypted that a browser would not show a visitor, and
nothing here talks to a site.
"""

import json
import re
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
# "name [at] domain [dot] com", "name (at) domain.com", "name{at}domain{dot}com", "name [@] domain.com"
_OBF_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:[\(\[\{]\s*(?:at|AT|@)\s*[\)\]\}]|\s+AT\s+)\s*"
    r"([a-zA-Z0-9\-]+(?:\s*(?:[\(\[\{]\s*(?:dot|DOT|\.)\s*[\)\]\}]|\s+DOT\s+|\.)\s*[a-zA-Z0-9\-]+)*)")
_DOT_WORDS = re.compile(r"\s*(?:[\(\[\{]\s*(?:dot|DOT|\.)\s*[\)\]\}]|\s+DOT\s+)\s*")

FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "bmp", "css", "js", "mjs",
             "map", "woff", "woff2", "ttf", "otf", "eot", "mp4", "webm", "mp3", "pdf", "zip", "json", "xml", "html", "php"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yoursite.com",
                       "email.com", "mysite.com", "website.com", "company.com", "test.com", "sentry.io",
                       "wixpress.com", "godaddy.com", "sentry.wixpress.com"}
PLACEHOLDER_LOCALS = {"name", "yourname", "your.name", "user", "username", "email", "youremail", "your.email",
                      "john.doe", "jane.doe", "firstname.lastname", "first.last", "someone", "mail", "test"}
# Addresses that appear inside scripts/JSON belong to the site builder, not the business.
THIRD_PARTY_DOMAINS = {"wix.com", "squarespace.com", "wordpress.com", "wordpress.org", "automattic.com", "google.com",
                       "googlemail.com.invalid", "facebook.com", "cloudflare.com", "shopify.com", "godaddy.com",
                       "weebly.com", "elementor.com", "gstatic.com", "w3.org", "schema.org", "jquery.com",
                       "sentry.io", "mailchimp.com", "hubspot.com", "intercom.io", "zendesk.com", "stripe.com",
                       "paypal.com", "yoast.com", "wpengine.com", "bluehost.com", "hostgator.com", "namecheap.com"}
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "aol.com",
             "yahoo.co.uk", "hotmail.co.uk", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "web.de",
             "yandex.com", "mail.com", "msn.com", "me.com", "zoho.com", "yahoo.fr", "orange.fr", "libero.it"}

SOCIAL_DOMAINS = {
    "facebook": ["facebook.com", "fb.com"],
    "instagram": ["instagram.com"],
    "linkedin": ["linkedin.com"],
    "whatsapp": ["wa.me", "api.whatsapp.com", "whatsapp.com"],
    "x_twitter": ["twitter.com", "x.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "tiktok": ["tiktok.com"],
}
# A social "link" that is really a share button / embed / generic page is not the business's profile.
_SOCIAL_JUNK = ("sharer", "/share", "intent/tweet", "/plugins/", "dialog/", "/embed", "login", "/tr?", "/policies",
                "/legal", "/help", "/about/", "developers.", "/watch?", "/hashtag/", "/explore/")

# Contact-page words, many languages (the tool is meant to work worldwide).
CONTACT_HINTS = [
    "contact", "kontakt", "contacto", "contatto", "contatti", "contactez", "nous-contacter", "contactar",
    "iletisim", "iletişim", "kapcsolat", "contato", "contacte", "yhteystiedot", "kontakta", "kontaktai",
    "kontakty", "kontakti", "elerhetoseg", "bize-ulasin", "hubungi", "lien-he", "liên-hệ", "lienhe",
    "about", "ueber-uns", "uber-uns", "über-uns", "a-propos", "apropos", "chi-siamo", "sobre", "quienes-somos",
    "nosotros", "hakkimizda", "hakkımızda", "o-nas", "om-oss", "om-os", "over-ons", "tentang", "gioi-thieu",
    "impressum", "imprint", "legal-notice", "mentions-legales", "aviso-legal", "datenschutz",
    "team", "staff", "locations", "location", "find-us", "reach", "connect", "get-in-touch", "getintouch",
    "customer-service", "enquir", "inquir", "book", "reserv", "appointment", "rezerv",
]
# Words that make a path MORE likely to hold an address (used for ordering).
_STRONG = ("contact", "kontakt", "contacto", "contatt", "iletisim", "impressum", "imprint", "mentions-legales",
           "aviso-legal", "enquir", "inquir", "get-in-touch", "nous-contacter", "hubungi", "lien-he")
PROBE_PATHS = ["/contact", "/contact-us", "/contactus", "/contact.html", "/contacts", "/kontakt", "/contacto",
               "/contatti", "/impressum", "/about", "/about-us"]


def _registrable(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    return _registrable(urlparse(a).netloc) == _registrable(urlparse(b).netloc)


def _domain_root(host: str) -> str:
    parts = _registrable(host).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def clean_email(raw: str) -> str | None:
    """Normalises one candidate; returns None for things that are not usable addresses."""
    if not raw:
        return None
    e = _ZERO_WIDTH.sub("", unquote(raw)).strip().strip(".,;:<>()[]{}\"'")
    e = e.replace("mailto:", "")
    if not EMAIL_RE.fullmatch(e):
        return None
    local, _, domain = e.rpartition("@")
    domain = domain.lower()
    tld = domain.rsplit(".", 1)[-1]
    if tld in FILE_TLDS or tld.isdigit():
        return None
    if re.search(r"@\d+x(?:\.|$)", e.lower()) or local.lower().endswith(("@2x", "@3x")):
        return None  # image@2x.png style asset names
    if domain in PLACEHOLDER_DOMAINS or local.lower() in PLACEHOLDER_LOCALS or ".." in e or len(e) > 254:
        return None
    return f"{local}@{domain}"


def decode_cf_email(encoded: str) -> str | None:
    """Cloudflare's published email-protection scheme: first byte is an XOR key. The visitor's
    browser reverses it with plain JavaScript; we do the same arithmetic without running JS."""
    try:
        key = int(encoded[:2], 16)
        return bytes(int(encoded[i:i + 2], 16) ^ key for i in range(2, len(encoded), 2)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def classify_social(href: str) -> str | None:
    low = (href or "").lower()
    if low.startswith(("mailto:", "tel:", "javascript:")) or any(j in low for j in _SOCIAL_JUNK):
        return None
    host = urlparse(low if "//" in low else "//" + low).netloc.split(":")[0]
    for platform, domains in SOCIAL_DOMAINS.items():
        for d in domains:
            if host == d or host.endswith("." + d):
                return platform
    return None


def _deobfuscate(text: str) -> set:
    out = set()
    for m in _OBF_RE.finditer(text):
        local, dom = m.group(1), _DOT_WORDS.sub(".", m.group(2))
        dom = re.sub(r"\s*\.\s*", ".", dom).strip(".")
        if "." in dom:
            out.add(f"{local}@{dom}")
    return out


def _jsonld_nodes(soup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                yield cur
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)


def extract_page(html: str, page_url: str) -> tuple:
    """Returns (emails, socials).
    emails: {address: how_found}; socials: {platform: url}."""
    emails: dict = {}
    socials: dict = {}
    if not html:
        return emails, socials
    own_root = _domain_root(urlparse(page_url).netloc)

    def add(raw, how, third_party_check=False):
        e = clean_email(raw)
        if not e or e in emails:
            return
        dom = e.rsplit("@", 1)[1]
        if third_party_check:
            if _domain_root(dom) != own_root and dom not in FREE_MAIL:
                return  # script-sourced addresses count only if they are the business's own domain / free mail
        if dom in THIRD_PARTY_DOMAINS and _domain_root(dom) != own_root:
            return
        emails[e] = how

    soup = BeautifulSoup(html, "html.parser")

    # 1) mailto links (url-decoded, query stripped)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            for part in re.split(r"[,;]", unquote(href[7:].split("?")[0])):
                add(part, "mailto link")
            continue
        p = classify_social(href)
        if p and p not in socials:
            socials[p] = urljoin(page_url, href)

    # 2) data attributes some themes use to hide the address from simple scrapers
    for tag in soup.find_all(True):
        for attr in ("data-email", "data-mail", "data-address", "data-contact-email"):
            v = tag.get(attr)
            if v:
                add(str(v), "page attribute")
        user, dom = tag.get("data-user") or tag.get("data-name"), tag.get("data-domain")
        if user and dom:
            add(f"{user}@{dom}", "page attribute")
        if tag.get("itemprop") == "email":
            add(tag.get("content") or tag.get_text(" ", strip=True), "structured data")

    # 3) structured data: JSON-LD email + sameAs profiles
    for node in _jsonld_nodes(soup):
        v = node.get("email")
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, str):
                add(item, "structured data")
        same = node.get("sameAs")
        for u in (same if isinstance(same, list) else [same]):
            if isinstance(u, str):
                p = classify_social(u)
                if p and p not in socials:
                    socials[p] = u

    # 4) visible text, incl. light obfuscation
    visible = _ZERO_WIDTH.sub("", soup.get_text(" "))
    for m in EMAIL_RE.findall(visible):
        add(m, "page text")
    for m in _deobfuscate(visible):
        add(m, "obfuscated text")

    # 5) Cloudflare-protected
    for enc in CF_EMAIL_RE.findall(html):
        dec = decode_cf_email(enc)
        if dec:
            add(dec, "Cloudflare-protected")

    # 6) the page's own raw source (script blobs, JSON state, HTML entities). Strictest filter.
    raw = _ZERO_WIDTH.sub("", html).replace("&#64;", "@").replace("&#x40;", "@").replace("\\u0040", "@").replace("&commat;", "@")
    for m in EMAIL_RE.findall(raw):
        add(m, "page source", third_party_check=True)

    return emails, socials


def contact_candidates(html: str, base_url: str, limit: int = 4) -> list:
    """Same-site links that probably lead to a contact/about/legal page, best first."""
    soup = BeautifulSoup(html or "", "html.parser")
    scored = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href).split("#")[0]
        if not same_site(full, base_url):
            continue
        path = urlparse(full).path.lower()
        text = a.get_text(" ", strip=True).lower()
        hay = path + " " + text
        if not any(h in hay for h in CONTACT_HINTS):
            continue
        if re.search(r"\.(?:jpg|jpeg|png|gif|pdf|zip|webp|svg)$", path):
            continue
        score = 2 if any(h in hay for h in _STRONG) else 1
        scored[full] = max(score, scored.get(full, 0))
    ordered = sorted(scored, key=lambda u: (-scored[u], len(u)))
    return ordered[:limit]


def sitemap_contact_urls(xml_text: str, base_url: str, limit: int = 3) -> list:
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text or "", flags=re.I)
    hits = []
    for u in urls:
        if not same_site(u, base_url):
            continue
        low = urlparse(u).path.lower()
        if any(h in low for h in _STRONG) and not low.endswith((".xml", ".jpg", ".png", ".pdf")):
            hits.append(u)
    return sorted(dict.fromkeys(hits), key=len)[:limit]
