"""
dedup.py — cross-source deduplication.

Businesses can appear more than once: Google Maps sometimes returns the
same place twice for a broad query, and combining Google Maps + OpenStreetMap
will produce genuine overlaps. This merges matching records into one,
keeping the most complete data rather than blindly discarding the "extra"
copy - a fuller record from source A can fill in a gap left by source B.
"""

import re
from urllib.parse import urlparse

_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|co|corp|company)\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    name = (name or "").lower().strip()
    name = _PUNCT_RE.sub("", name)
    name = _SUFFIX_RE.sub("", name)
    return _SPACE_RE.sub(" ", name).strip()


def _normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _domain(website: str) -> str:
    if not website:
        return ""
    try:
        candidate = website if "://" in website else f"//{website}"
        netloc = urlparse(candidate).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _merge_rows(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if value and not merged.get(key):
            merged[key] = value
    if base.get("source") != extra.get("source"):
        merged["source"] = "multiple"
    return merged


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """Groups rows that are very likely the same business - matching
    domain, matching phone, or matching normalized name - and merges each
    group into one record. Matching on domain or phone alone is treated as
    confident; name matching alone is intentionally the last resort, since
    two different businesses can share a generic name."""
    groups: list[dict] = []

    for row in rows:
        name_key = _normalize_name(row.get("title") or row.get("name") or "")
        phone_key = _normalize_phone(row.get("phone") or "")
        domain_key = _domain(row.get("website") or "")

        match_idx = None
        for i, g in enumerate(groups):
            if domain_key and domain_key in g["domains"]:
                match_idx = i
                break
            if phone_key and phone_key in g["phones"]:
                match_idx = i
                break
            if name_key and name_key in g["names"]:
                match_idx = i
                break

        if match_idx is None:
            groups.append({
                "row": row,
                "names": {name_key} if name_key else set(),
                "phones": {phone_key} if phone_key else set(),
                "domains": {domain_key} if domain_key else set(),
            })
        else:
            g = groups[match_idx]
            g["row"] = _merge_rows(g["row"], row)
            if name_key:
                g["names"].add(name_key)
            if phone_key:
                g["phones"].add(phone_key)
            if domain_key:
                g["domains"].add(domain_key)

    return [g["row"] for g in groups]
