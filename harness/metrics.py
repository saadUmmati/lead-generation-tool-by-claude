"""
metrics.py - pure functions behind the measurement harness (stdlib only, no network).

Ground truth comes from a HUMAN who opened each business's website and wrote down the
email addresses actually published there (see README.md). Everything here compares the
tool's output with that answer key, so "more emails" can be measured instead of assumed.
"""

import math
import re
from urllib.parse import urlparse

STATUSES = ("HAS_EMAIL", "NONE_PUBLISHED", "SOCIAL_ONLY", "UNREACHABLE")
# HAS_EMAIL       an address is published on the business's own site
# NONE_PUBLISHED  site loads, no email anywhere (contact form only, phone only, ...)
# SOCIAL_ONLY     the only email is on a social profile, not on the site
# UNREACHABLE     the human could not load the site either -> excluded from scoring

_EMAIL_SPLIT = re.compile(r"[;,\s|]+")


def norm_domain(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return ""
    if "//" not in u:
        u = "//" + u
    host = urlparse(u).netloc.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse_emails(s: str) -> set:
    return {e.strip(".,;:<>()").lower() for e in _EMAIL_SPLIT.split(s or "") if "@" in e}


def wilson(k: int, n: int, z: float = 1.96):
    """95% Wilson interval for a proportion k/n. With ~50 businesses the interval is wide:
    that width is the honest size of what this sample can and cannot show."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(gained: int, lost: int) -> float:
    """Two-sided exact sign test for paired before/after outcomes (businesses the new
    version found that the old one missed vs the reverse). Small p = the change is
    unlikely to be luck."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cohen_kappa(a: list, b: list) -> float:
    """Agreement between two human labellers beyond chance (1 = perfect, 0 = chance)."""
    n = len(a)
    if n == 0 or n != len(b):
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    cats = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def miss_bucket(result_row: dict, where_found: str = "") -> str:
    """Why did the tool miss an address that a human found? Uses what the tool recorded about
    the fetch, plus the human's note on where the address actually sits."""
    note = (result_row.get("site_fetch_note") or "").lower()
    status = (result_row.get("site_status") or "").lower()
    if note.startswith(("http 403", "http 429", "http 401")):
        return "blocked by site (HTTP %s)" % note.split()[1] if len(note.split()) > 1 else "blocked by site"
    if "timeout" in note or "stalled" in note:
        return "timeout / stalled"
    if "ssl" in note or "connection" in note:
        return "connection / SSL error"
    if note:
        return "fetch failed: " + note[:30]
    if status == "blocked_or_parked":
        return "bot-protection or parked page"
    if status == "thin":
        return "page had almost no text (likely script-built)"
    if status == "none":
        return "site not fetched (no reason recorded)"
    wf = (where_found or "").strip().lower()
    if wf in ("image", "pdf", "js popup", "javascript", "contact form page"):
        return "page read, address was in: " + wf
    return "page read, address not extracted (extraction gap)"


def _status(row: dict) -> str:
    s = (row.get("label_status") or "").strip().upper().replace(" ", "_")
    return s if s in STATUSES else ""


def evaluate(labels: list, results: list) -> dict:
    """labels: rows from the filled labels CSV. results: rows from the tool's output CSV
    (fields 'website', 'emails', 'extra_emails', optionally site_status / site_fetch_note)."""
    by_domain = {}
    for r in results:
        d = norm_domain(r.get("website", ""))
        if d and d not in by_domain:
            by_domain[d] = r

    per, skipped = [], []
    for lab in labels:
        st = _status(lab)
        d = norm_domain(lab.get("website", ""))
        if not st:
            skipped.append((lab.get("title", d), "not labelled yet"))
            continue
        if st == "UNREACHABLE":
            skipped.append((lab.get("title", d), "UNREACHABLE"))
            continue
        res = by_domain.get(d)
        if res is None:
            skipped.append((lab.get("title", d), "not in results file"))
            continue
        truth = parse_emails(lab.get("true_emails", ""))
        found = parse_emails(res.get("emails", "")) | parse_emails(res.get("extra_emails", ""))
        if st == "HAS_EMAIL" and not truth:
            skipped.append((lab.get("title", d), "HAS_EMAIL but true_emails empty"))
            continue
        if st != "HAS_EMAIL":
            truth = set()
        per.append({"domain": d, "title": lab.get("title", d), "status": st, "truth": truth, "found": found,
                    "hit": bool(truth & found), "result": res, "where_found": lab.get("where_found", "")})

    has = [p for p in per if p["status"] == "HAS_EMAIL"]
    hits = sum(p["hit"] for p in has)
    true_total = sum(len(p["truth"]) for p in has)
    true_found = sum(len(p["truth"] & p["found"]) for p in has)
    reported = sum(len(p["found"]) for p in per)
    correct = sum(len(p["found"] & p["truth"]) for p in per)
    review = [(p["title"], sorted(p["found"] - p["truth"])) for p in per if p["found"] - p["truth"]]
    fp_rows = [p for p in per if p["status"] != "HAS_EMAIL"]
    fp_hit = sum(1 for p in fp_rows if p["found"])

    misses = {}
    for p in has:
        if not p["hit"]:
            b = miss_bucket(p["result"], p["where_found"])
            misses.setdefault(b, []).append(p["title"])

    return {
        "n_scored": len(per), "n_has_email": len(has), "n_skipped": len(skipped), "skipped": skipped,
        "business_recall": {"k": hits, "n": len(has), "value": hits / len(has) if has else None, "ci95": wilson(hits, len(has))},
        "email_recall": {"k": true_found, "n": true_total, "value": true_found / true_total if true_total else None},
        "strict_precision": {"k": correct, "n": reported, "value": correct / reported if reported else None, "ci95": wilson(correct, reported)},
        "no_email_rows_with_output": {"k": fp_hit, "n": len(fp_rows)},
        "review_needed": review,
        "miss_reasons": {k: sorted(v) for k, v in sorted(misses.items(), key=lambda kv: -len(kv[1]))},
        "hit_by_domain": {p["domain"]: p["hit"] for p in has},
        "unlabelled_or_excluded": len(skipped),
    }


def compare(new: dict, old: dict) -> dict:
    """Paired comparison on businesses scored in both runs."""
    a, b = new["hit_by_domain"], old["hit_by_domain"]
    both = set(a) & set(b)
    gained = sorted(d for d in both if a[d] and not b[d])
    lost = sorted(d for d in both if b[d] and not a[d])
    return {"n_paired": len(both), "gained": gained, "lost": lost, "p_value": mcnemar_exact(len(gained), len(lost))}


def inter_rater(labels: list) -> dict:
    """If a second person filled label_status_2 / true_emails_2, report how well they agree."""
    pairs = [(_status(r), (r.get("label_status_2") or "").strip().upper().replace(" ", "_"), r) for r in labels]
    pairs = [(x, y, r) for x, y, r in pairs if x and y in STATUSES]
    if not pairs:
        return {"n": 0}
    sa, sb = [p[0] for p in pairs], [p[1] for p in pairs]
    email_agree = sum(parse_emails(r.get("true_emails", "")) == parse_emails(r.get("true_emails_2", "")) for _, _, r in pairs)
    return {"n": len(pairs), "status_agreement": sum(x == y for x, y in zip(sa, sb)) / len(pairs),
            "kappa": cohen_kappa(sa, sb), "email_set_agreement": email_agree / len(pairs)}
