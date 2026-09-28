"""
scoring.py — 0-100 lead quality score.

This is a transparent, additive heuristic, not a black box: each signal
adds points, and you can see exactly why a lead scored what it did. It's
meant to help you sort/filter leads, not as an absolute truth about a
business.
"""

SOCIAL_FIELDS = ["facebook", "instagram", "linkedin", "whatsapp", "x_twitter", "youtube", "tiktok"]


def score_business(row: dict) -> int:
    score = 0

    if row.get("website"):
        score += 20
    if row.get("phone"):
        score += 10
    if row.get("title"):
        score += 5  # business confidently identified at all

    confidences = [row.get("email_confidence_primary"), row.get("email_confidence_extra")]
    if "High" in confidences:
        score += 30
    elif "Medium" in confidences:
        score += 20
    elif "Low" in confidences:
        score += 8
    # nothing found at all -> 0 points for the email portion

    social_count = sum(1 for f in SOCIAL_FIELDS if row.get(f))
    score += min(social_count * 4, 15)  # capped so socials alone can't dominate

    try:
        rating = float(row.get("review_rating") or 0)
        review_count = int(float(row.get("review_count") or 0))
        if rating >= 4.0 and review_count >= 5:
            score += 10
        elif review_count > 0:
            score += 4
    except (ValueError, TypeError):
        pass

    if row.get("source") == "multiple":
        score += 10  # confirmed by more than one independent source

    return max(0, min(100, score))
