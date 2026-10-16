import asyncio, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from email_extract import extract_page, clean_email, contact_candidates, sitemap_contact_urls, classify_social

URL = "https://www.grandhotel.co.uk/"


def emails(html, url=URL):
    return set(extract_page(html, url)[0])


def test_clean_email_rejects_junk():
    for bad in ["logo@2x.png", "sprite@3x.webp", "you@example.com", "name@domain.com", "a@b.js", "x..y@site.com", "user@sentry.io"]:
        assert clean_email(bad) is None, bad
    assert clean_email("Info%40Grand.co.uk") == "Info@grand.co.uk"
    assert clean_email("​info@grand.co.uk.") == "info@grand.co.uk"


def test_mailto_variants_and_query():
    h = '<a href="mailto:Sales@grandhotel.co.uk?subject=Hi">x</a><a href="MAILTO:a%40grandhotel.co.uk,b@grandhotel.co.uk">y</a>'
    assert emails(h) == {"Sales@grandhotel.co.uk", "a@grandhotel.co.uk", "b@grandhotel.co.uk"}


def test_obfuscations():
    assert emails("<p>Write to reservations [at] grandhotel [dot] co [dot] uk</p>") == {"reservations@grandhotel.co.uk"}
    assert emails("<p>info(at)grandhotel.co.uk</p>") == {"info@grandhotel.co.uk"}
    assert emails("<p>info&#64;grandhotel.co.uk</p>") == {"info@grandhotel.co.uk"}
    assert emails("<p>inf​o@grandhotel.co.uk</p>") == {"info@grandhotel.co.uk"}
    assert emails('<span data-user="stay" data-domain="grandhotel.co.uk"></span>') == {"stay@grandhotel.co.uk"}
    assert emails('<span data-email="hello@grandhotel.co.uk"></span>') == {"hello@grandhotel.co.uk"}


def test_no_false_positive_from_prose():
    assert emails("<p>Open Mon-Fri AT 10.00 AM, ask AT the desk. Price 5.50</p>") == set()


def test_jsonld_email_and_sameas():
    h = '''<script type="application/ld+json">{"@type":"Hotel","email":"mailto:book@grandhotel.co.uk",
    "sameAs":["https://www.facebook.com/grandhotelleeds","https://www.instagram.com/grandhotel/"]}</script>'''
    e, s = extract_page(h, URL)
    assert set(e) == {"book@grandhotel.co.uk"} and s["facebook"].endswith("grandhotelleeds") and "instagram" in s


def test_cloudflare_protected():
    key = 0x42
    addr = "info@grandhotel.co.uk"
    enc = "42" + "".join(f"{b ^ key:02x}" for b in addr.encode())
    assert emails(f'<a class="__cf_email__" data-cfemail="{enc}">[email protected]</a>') == {addr}


def test_script_blob_only_counts_for_own_domain_or_free_mail():
    h = '''<script>window.__DATA__={"support":"help@wix.com","owner":"manager@grandhotel.co.uk","alt":"grandhotel.leeds@gmail.com",
    "agency":"hello@webagency.io"}</script>'''
    assert emails(h) == {"manager@grandhotel.co.uk", "grandhotel.leeds@gmail.com"}


def test_third_party_visible_email_blocked_but_other_business_domain_allowed():
    assert emails("<p>Mail privacy@wix.com or bookings@grand-group.com</p>") == {"bookings@grand-group.com"}


def test_social_junk_filtered():
    h = '''<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a><a href="https://twitter.com/intent/tweet?x">t</a>
    <a href="https://www.facebook.com/grandhotel">fb</a><a href="https://notfacebook.com.evil.io/x">no</a><a href="https://wa.me/447700900123">wa</a>'''
    s = extract_page(h, URL)[1]
    assert s == {"facebook": "https://www.facebook.com/grandhotel", "whatsapp": "https://wa.me/447700900123"}
    assert classify_social("https://x.com/grand") == "x_twitter" and classify_social("https://box.com/x") is None


def test_contact_candidates_multilingual_and_ordering():
    h = '''<nav><a href="/de/kontakt">Kontakt</a><a href="/about-us">About</a><a href="/menu">Menu</a>
    <a href="/fr/nous-contacter">Nous</a><a href="https://other.com/contact">ext</a><a href="/pic.jpg">contact photo</a>
    <a href="/iletisim">İletişim</a><a href="#top">contact</a></nav>'''
    c = contact_candidates(h, "https://grand.de/", limit=4)
    assert "https://grand.de/menu" not in c and all(u.startswith("https://grand.de/") for u in c)
    assert c[0] in ("https://grand.de/iletisim", "https://grand.de/de/kontakt") and "https://grand.de/about-us" not in c[:2]
    # link TEXT alone can qualify a page whose path is opaque
    assert "https://grand.de/p?id=7" in contact_candidates('<a href="/p?id=7">Contact us</a>', "https://grand.de/")


def test_sitemap_contact_urls():
    xml = "<urlset><url><loc>https://grand.de/</loc></url><url><loc>https://grand.de/en/contact-us/</loc></url><url><loc>https://x.com/contact</loc></url><url><loc>https://grand.de/impressum</loc></url></urlset>"
    assert sitemap_contact_urls(xml, "https://grand.de/") == ["https://grand.de/impressum", "https://grand.de/en/contact-us/"]


# ---- enrich_one integration with a scripted fake site (httpx stubbed when not installed) ----
def _load_enrich():
    try:
        import httpx  # noqa: F401
    except ImportError:
        m = types.ModuleType("httpx")
        for n in ("HTTPError", "InvalidURL", "TimeoutException", "ConnectError"):
            setattr(m, n, type(n, (Exception,), {}))
        m.AsyncClient = object
        sys.modules["httpx"] = m
    import enrich
    return enrich


class FakeResp:
    def __init__(self, status, text, ctype="text/html"):
        self.status_code, self.text, self.headers = status, text, {"content-type": ctype}


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def get(self, url, timeout=None, follow_redirects=True):
        self.calls.append(url)
        if url in self.pages:
            body = self.pages[url]
            return FakeResp(200, body, "application/xml" if url.endswith(".xml") else "text/html")
        return FakeResp(404, "")


def _enrich(pages, website="https://grand.example.net/"):
    en = _load_enrich()
    client = FakeClient(pages)
    res = asyncio.run(en.enrich_one(client, asyncio.Semaphore(2), {"website": website}))
    return res, client


def test_enrich_finds_email_via_unlinked_contact_path():
    res, c = _enrich({"https://grand.example.net/": "<html><body><h1>Grand</h1><div id=app></div></body></html>",
                      "https://grand.example.net/contact": '<p>Email <a href="mailto:hi@grand.example.net">us</a></p>'})
    assert res["extra_emails"] == "hi@grand.example.net" and "/contact" in res["email_sources"]


def test_enrich_uses_sitemap_and_stops_after_first_hit():
    pages = {"https://grand.example.net/": "<html><body>hi</body></html>",
             "https://grand.example.net/sitemap.xml": "<urlset><url><loc>https://grand.example.net/en/get-in-touch</loc></url></urlset>",
             "https://grand.example.net/en/get-in-touch": "<p>office@grand.example.net</p>"}
    res, c = _enrich(pages)
    assert res["extra_emails"] == "office@grand.example.net"
    assert len([u for u in c.calls if u.endswith("/contact")]) == 0  # found via sitemap, no blind probing


def test_enrich_does_not_probe_when_homepage_already_has_email():
    res, c = _enrich({"https://grand.example.net/": '<a href="mailto:info@grand.example.net">m</a>'})
    assert res["extra_emails"] == "info@grand.example.net" and c.calls == ["https://grand.example.net/"]


def test_enrich_probe_budget_is_capped():
    res, c = _enrich({"https://grand.example.net/": "<html><body>nothing here</body></html>"})
    assert res["extra_emails"] == "" and len(c.calls) <= 1 + 1 + 4
