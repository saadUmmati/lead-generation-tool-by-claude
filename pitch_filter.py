"""
pitch_filter.py — decides which businesses deserve a pitch and which offer fits.

No network, no AI. Pure rules so the result is explainable.
"""

import re

from scoring import SOCIAL_FIELDS

CATEGORY_EXACT = 6
CATEGORY_SUBSTR = 4
KEYWORD_CAP = 8
KEYWORD_THRESHOLD = 3
W_NAME_TITLE = 3
W_HEAD_DESC = 2
W_EXCERPT = 1


def _norm(text) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in t.split()]
    return " ".join(words)


def contact_route(row: dict) -> str:
    """Best available way to reach the business ('' if none)."""
    if (row.get("emails") or row.get("extra_emails")) and row.get("email_confidence_primary") != "Invalid":
        return "email"
    if row.get("extra_emails"):
        return "email"
    if row.get("phone"):
        return "phone"
    for f in SOCIAL_FIELDS:
        if row.get(f):
            return f
    return ""


def is_eligible(row: dict, min_score: int = 40):
    """Returns (eligible: bool, reasons: list[str])."""
    reasons = []
    if not row.get("website"):
        reasons.append("no website")
    elif row.get("site_status") != "ok":
        reasons.append("website text not readable")
    if not contact_route(row):
        reasons.append("no contact route")
    try:
        score = int(float(row.get("quality_score") or 0))
    except (TypeError, ValueError):
        score = 0
    if score < min_score:
        reasons.append(f"score {score} below minimum {min_score}")
    return (not reasons), reasons


def _count(needle: str, hay: str) -> bool:
    return bool(needle) and f" {needle} " in f" {hay} "


def match_offer(row: dict, offers: list):
    """Returns (offer | None, score, reason)."""
    if not offers:
        return None, 0, "no offers loaded"

    name = _norm(row.get("title"))
    domain = _norm(re.sub(r"^https?://(www\.)?", "", str(row.get("website") or "")).split("/")[0])
    cat = _norm(row.get("category"))
    s_title = _norm(row.get("site_title"))
    s_desc = _norm(row.get("site_description"))
    s_head = _norm(row.get("site_headings"))
    s_text = _norm(row.get("site_text_excerpt"))
    skip_hay = f" {name} {domain} {s_title} {s_desc} "

    best = (None, 0, "")
    for offer in offers:
        skip_hit = next((k for k in offer.get("SKIP_IF_KEYWORDS", [])
                         if _norm(k) and f" {_norm(k)} " in skip_hay), None)
        if skip_hit:
            continue

        cats = [_norm(c) for c in offer.get("MATCH_CATEGORIES", []) if _norm(c)]
        kws = [_norm(k) for k in offer.get("MATCH_KEYWORDS", []) if _norm(k)]

        if not cats and not kws:
            score, why = 1, "offer has no match criteria (applies to everyone)"
        else:
            score, parts = 0, []
            for c in cats:
                if cat and c == cat:
                    score += CATEGORY_EXACT
                    parts.append(f"category '{c}'")
                    break
                if cat and (c in cat or cat in c):
                    score += CATEGORY_SUBSTR
                    parts.append(f"category ~'{c}'")
                    break
            kw_pts = 0
            for k in kws:
                if _count(k, f"{name} {s_title}"):
                    kw_pts += W_NAME_TITLE
                    parts.append(f"'{k}' in name/title")
                elif _count(k, f"{s_head} {s_desc}"):
                    kw_pts += W_HEAD_DESC
                    parts.append(f"'{k}' in headings")
                elif _count(k, s_text):
                    kw_pts += W_EXCERPT
                    parts.append(f"'{k}' in site text")
            score += min(kw_pts, KEYWORD_CAP)
            if score < KEYWORD_THRESHOLD:
                continue
            why = ", ".join(parts[:4])

        if score > best[1]:
            best = (offer, score, why)

    if best[0] is None:
        return None, 0, "no offer matched this business"
    return best


def annotate_row(row: dict, profile, min_score: int = 40) -> dict:
    """Sets pitch_status / pitch_offer / pitch_match_score / pitch_match_reason."""
    row.setdefault("pitch_status", "")
    row["pitch_offer"] = ""
    row["pitch_match_score"] = ""
    row["pitch_match_reason"] = ""

    if profile is None or not getattr(profile, "ok", False):
        row["pitch_status"] = "skipped: no offer form filled"
        return row

    eligible, reasons = is_eligible(row, min_score)
    if not eligible:
        row["pitch_status"] = "skipped: " + "; ".join(reasons)
        return row

    offer, score, why = match_offer(row, profile.offers)
    if offer is None:
        row["pitch_status"] = "skipped: " + why
        return row

    row["pitch_status"] = "eligible"
    row["pitch_offer"] = offer.get("OFFER_NAME", "")
    row["pitch_match_score"] = score
    row["pitch_match_reason"] = why
    return row
