import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from site_summary import summarize_site
from offer_profile import load_offer_profile
from pitch_filter import is_eligible, match_offer, annotate_row

LONG = "<html lang='en-GB'><head><title>Smile Dental</title><meta name='description' content='Family dentist'></head><body><nav>Menu</nav><h1>Family dentistry in Leeds</h1><p>" + " ".join(f"Service number {i} covers check-ups, hygiene and implants for patients." for i in range(20)) + "</p><a href='https://calendly.com/x'>Book online</a><footer>ignore me</footer></body></html>"

def test_summary_ok():
    s = summarize_site({"https://a.com": LONG})
    assert s["site_status"] == "ok" and s["site_language"] == "en"
    assert "online_booking" in s["site_signals"] and "ignore me" not in s["site_text_excerpt"]
    assert len(s["site_text_excerpt"]) <= 1800

def test_summary_thin_blocked_none():
    assert summarize_site({"u": "<html><body>Hi</body></html>"})["site_status"] == "thin"
    assert summarize_site({"u": "<html><body>Just a moment... checking your browser</body></html>"})["site_status"] == "blocked_or_parked"
    assert summarize_site({})["site_status"] == "none"

def _profile(tmp_path, body):
    p = tmp_path / "o.txt"; p.write_text(body, encoding="utf-8")
    return load_offer_profile(str(p))

def test_parser(tmp_path):
    pr = _profile(tmp_path, "# c\n=== SENDER ===\nYOUR_NAME: Sam\nCOMPANY_NAME: Acme\nTONE: [ fill here ]\n=== OFFER ===\nOFFER_NAME: Bots\nWHAT_I_SELL: chatbots\n that book\nMATCH_KEYWORDS: dentist, clinic; spa\nMATCH_KEYWRODS: x\n=== OFFER ===\nOFFER_NAME: [ fill here ]\nWHAT_I_SELL: [ fill here ]\n")
    assert pr.ok and len(pr.offers) == 1
    assert pr.offers[0]["WHAT_I_SELL"] == "chatbots that book"
    assert pr.offers[0]["MATCH_KEYWORDS"] == ["dentist", "clinic", "spa"]
    assert "TONE" not in pr.sender
    assert any("did you mean MATCH_KEYWORDS" in p for p in pr.problems)

def test_missing_file(tmp_path):
    assert not load_offer_profile(str(tmp_path / "none.txt")).ok

OFFERS = [{"OFFER_NAME": "Bots", "MATCH_CATEGORIES": ["dentist"], "MATCH_KEYWORDS": ["implant"], "SKIP_IF_KEYWORDS": ["chain"]}]

def _row(**kw):
    r = {"title": "Smile Dental", "category": "Dentist", "website": "https://a.com", "site_status": "ok",
         "emails": "a@a.com", "email_confidence_primary": "High", "quality_score": 70,
         "site_text_excerpt": "implants and more"}
    r.update(kw); return r

def test_eligibility():
    assert is_eligible(_row())[0]
    assert "no website" in is_eligible(_row(website=""))[1]
    assert "website text not readable" in is_eligible(_row(site_status="thin"))[1]
    assert not is_eligible(_row(quality_score=10), 40)[0]
    assert "no contact route" in is_eligible(_row(emails="", email_confidence_primary="", phone=""))[1]

def test_matching_and_skip():
    o, s, _ = match_offer(_row(), OFFERS)
    assert o and s >= 7
    assert match_offer(_row(title="Big Chain Dental"), OFFERS)[0] is None
    assert match_offer(_row(category="Plumber", site_text_excerpt="pipes"), OFFERS)[0] is None
    assert match_offer(_row(), [{"OFFER_NAME": "Any"}])[0]["OFFER_NAME"] == "Any"

def test_annotate(tmp_path):
    pr = _profile(tmp_path, "=== OFFER ===\nWHAT_I_SELL: x\nMATCH_CATEGORIES: dentist\n")
    r = annotate_row(_row(), pr, 40)
    assert r["pitch_status"] == "eligible" and r["pitch_offer"]
    assert annotate_row(_row(), None)["pitch_status"].startswith("skipped")


def test_dedupe_and_attr_leak():
    block = "Call Us Appointment Proudly Serving Patients in Two Prime locations. "
    html = "<html><body><p>" + block * 8 + ("Real unique content about implants and braces here. " * 3) + '</p><p>class="home wp-singular page" > Skip</p></body></html>'
    s = summarize_site({"u": html})
    assert s["site_text_excerpt"].count("Proudly Serving") == 1
    assert 'class="' not in s["site_text_excerpt"]


def test_thin_site_with_good_metadata_is_usable():
    thin = _row(site_status="thin", site_headings="Dental Care For All Ages | Book An Appointment | " * 4)
    assert is_eligible(thin)[0]
    o, s, why = match_offer(thin, OFFERS)
    assert o is not None
    pr_row = annotate_row(dict(thin), type("P", (), {"ok": True, "offers": OFFERS})(), 40)
    assert pr_row["pitch_status"] == "eligible" and "[limited site info]" in pr_row["pitch_match_reason"]
    assert not is_eligible(_row(site_status="thin", site_title="Home"))[0]


def test_unreachable_reason_shown():
    ok, reasons = is_eligible(_row(site_status="none", site_fetch_note="HTTP 403"))
    assert not ok and "website not reachable (HTTP 403)" in reasons


def test_template_placeholders_removed():
    html = "<html><body><h1>{{ fTitle }}</h1><h2>Our implants</h2><p>{{ t.name }} " + " ".join(f"Real sentence {i} about dental care and treatments." for i in range(30)) + "</p></body></html>"
    s = summarize_site({"u": html})
    assert "{{" not in s["site_headings"] and "{{" not in s["site_text_excerpt"]
    assert s["site_headings"] == "Our implants"
