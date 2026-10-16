import csv, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "harness"))
from metrics import (norm_domain, parse_emails, wilson, mcnemar_exact, cohen_kappa, evaluate, compare, inter_rater, miss_bucket)


def test_domain_and_email_parsing():
    assert norm_domain("https://www.Grand.co.uk/contact?x=1") == "grand.co.uk"
    assert norm_domain("grand.co.uk") == "grand.co.uk" and norm_domain("") == ""
    assert parse_emails("A@x.com; b@y.com , nope, C@X.com.") == {"a@x.com", "b@y.com", "c@x.com"}


def test_wilson_and_mcnemar_known_values():
    lo, hi = wilson(40, 50)
    assert 0.66 < lo < 0.68 and 0.88 < hi < 0.90          # standard Wilson 40/50 -> ~0.670-0.887
    assert wilson(0, 0) == (0.0, 0.0)
    assert abs(mcnemar_exact(10, 0) - 0.001953125) < 1e-9  # 2 * (1/1024)
    assert mcnemar_exact(5, 5) == 1.0 and mcnemar_exact(0, 0) == 1.0


def test_kappa():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    k = cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"])
    assert abs(k) < 1e-9


LABELS = [
    {"title": "A", "website": "https://a.com", "label_status": "HAS_EMAIL", "true_emails": "info@a.com; sales@a.com", "where_found": "footer"},
    {"title": "B", "website": "b.com", "label_status": "HAS_EMAIL", "true_emails": "hi@b.com", "where_found": "image"},
    {"title": "C", "website": "c.com", "label_status": "NONE_PUBLISHED", "true_emails": ""},
    {"title": "D", "website": "d.com", "label_status": "HAS_EMAIL", "true_emails": "x@d.com", "where_found": "homepage"},
    {"title": "E", "website": "e.com", "label_status": "UNREACHABLE"},
    {"title": "F", "website": "f.com", "label_status": ""},
    {"title": "G", "website": "g.com", "label_status": "HAS_EMAIL", "true_emails": "g@g.com"},
]
RESULTS = [
    {"website": "https://www.a.com/", "emails": "INFO@a.com", "extra_emails": "wrong@a.com"},
    {"website": "b.com", "emails": "", "extra_emails": "", "site_status": "ok"},
    {"website": "c.com", "emails": "", "extra_emails": "ghost@c.com"},
    {"website": "d.com", "emails": "", "extra_emails": "", "site_fetch_note": "HTTP 403"},
    {"website": "e.com", "emails": "e@e.com"},
]


def test_evaluate_counts():
    r = evaluate(LABELS, RESULTS)
    assert r["n_scored"] == 4 and r["n_has_email"] == 3          # A, B, D scored as has-email; C scored; E,F,G skipped
    assert r["business_recall"]["k"] == 1 and r["business_recall"]["n"] == 3
    assert r["email_recall"]["k"] == 1 and r["email_recall"]["n"] == 4   # truth: info,sales,hi,x
    assert r["strict_precision"]["k"] == 1 and r["strict_precision"]["n"] == 3  # info ok; wrong@a, ghost@c wrong
    assert r["no_email_rows_with_output"] == {"k": 1, "n": 1}
    assert ("A", ["wrong@a.com"]) in r["review_needed"] and ("C", ["ghost@c.com"]) in r["review_needed"]
    reasons = r["miss_reasons"]
    assert "blocked by site (HTTP 403)" in reasons and reasons["blocked by site (HTTP 403)"] == ["D"]
    assert any("image" in k for k in reasons)                       # B: page read, address in an image
    skipped = dict(r["skipped"])
    assert skipped["E"] == "UNREACHABLE" and skipped["F"] == "not labelled yet" and skipped["G"] == "not in results file"


def test_compare_paired_regressions():
    old = evaluate(LABELS, RESULTS)
    better = [dict(x) for x in RESULTS]
    better[1]["extra_emails"] = "hi@b.com"          # gains B
    better[0]["emails"] = ""; better[0]["extra_emails"] = ""   # loses A
    new = evaluate(LABELS, better)
    c = compare(new, old)
    assert c["n_paired"] == 3 and c["gained"] == ["b.com"] and c["lost"] == ["a.com"] and c["p_value"] == 1.0


def test_inter_rater():
    rows = [dict(LABELS[0], label_status_2="HAS_EMAIL", true_emails_2="info@a.com;sales@a.com"),
            dict(LABELS[1], label_status_2="NONE_PUBLISHED", true_emails_2=""),
            dict(LABELS[2], label_status_2="NONE_PUBLISHED", true_emails_2=""),
            dict(LABELS[3], label_status_2="HAS_EMAIL", true_emails_2="x@d.com")]
    ir = inter_rater(rows)
    assert ir["n"] == 4 and ir["status_agreement"] == 0.75 and ir["email_set_agreement"] == 0.75


def test_miss_buckets():
    assert miss_bucket({"site_status": "thin"}).startswith("page had almost no text")
    assert miss_bucket({"site_status": "blocked_or_parked"}).startswith("bot-protection")
    assert miss_bucket({"site_status": "ok"}) == "page read, address not extracted (extraction gap)"
    assert miss_bucket({"site_fetch_note": "timeout"}) == "timeout / stalled"
    assert miss_bucket({"site_status": "none"}).startswith("site not fetched")


def test_make_sheet_and_end_to_end(tmp_path):
    import subprocess
    src = tmp_path / "res.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "website", "google_maps_link", "emails"])
        w.writeheader()
        for i in range(30):
            w.writerow({"title": f"B{i}", "website": f"https://site{i % 25}.com", "emails": f"secret{i}@x.com"})
    out = tmp_path / "work"
    h = os.path.join(os.path.dirname(__file__), "..", "harness")
    subprocess.run([sys.executable, os.path.join(h, "make_label_sheet.py"), "--results", str(src), "--n", "10", "--out", str(out)], check=True, capture_output=True)
    rows = list(csv.DictReader(open(out / "labels.csv", encoding="utf-8")))
    assert len(rows) == 10 and len({r["website"] for r in rows}) == 10
    assert "secret" not in open(out / "labels.csv", encoding="utf-8").read()   # answer key is blind to the tool's output
    for r in rows:
        r["label_status"], r["true_emails"] = "HAS_EMAIL", "a@" + r["website"].split("//")[1]
    with open(out / "labels.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "website", "extra_emails"]); w.writeheader()
        for i, r in enumerate(rows):
            w.writerow({"title": r["title"], "website": r["website"], "extra_emails": r["true_emails"] if i < 7 else ""})
    p = subprocess.run([sys.executable, os.path.join(h, "evaluate.py"), "--labels", str(out / "labels.csv"), "--results", str(out / "results.csv"),
                        "--save", str(out / "run.json")], check=True, capture_output=True, text=True)
    assert "BUSINESS RECALL  70%  (7/10" in p.stdout and "STRICT PRECISION 100%" in p.stdout
    p2 = subprocess.run([sys.executable, os.path.join(h, "evaluate.py"), "--labels", str(out / "labels.csv"), "--results", str(out / "results.csv"),
                         "--baseline", str(out / "run.json")], check=True, capture_output=True, text=True)
    assert "gained 0, lost 0" in p2.stdout


def test_only_missing_mode(tmp_path):
    import subprocess
    src = tmp_path / "res.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "website", "emails", "extra_emails"])
        w.writeheader()
        for i in range(6):
            w.writerow({"title": f"B{i}", "website": f"https://s{i}.com", "emails": "x@y.com" if i < 3 else "", "extra_emails": ""})
    h = os.path.join(os.path.dirname(__file__), "..", "harness")
    subprocess.run([sys.executable, os.path.join(h, "make_label_sheet.py"), "--results", str(src), "--n", "10", "--only-missing", "--out", str(tmp_path / "w")], check=True, capture_output=True)
    rows = list(csv.DictReader(open(tmp_path / "w" / "labels.csv", encoding="utf-8")))
    assert sorted(r["title"] for r in rows) == ["B3", "B4", "B5"]
