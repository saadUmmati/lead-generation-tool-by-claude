"""
validate.py — lightweight, free, local email validation.

Checks performed (no paid API, no SMTP handshake — you asked specifically
for the fast/safe syntax+MX check rather than a live mailbox connection):
  - syntax:      does it look like a real email address
  - mx_found:    does the domain have a valid MX (or A-record fallback)
  - disposable:  is the domain a known disposable/temp-mail provider
  - role_based:  is the local part a generic role account (info@, admin@…)
  - confidence:  High / Medium / Low / Invalid, combining the above
"""

import re
from functools import lru_cache
from typing import Optional

try:
    import dns.resolver
    _HAS_DNS = True
except ImportError:
    _HAS_DNS = False

EMAIL_SYNTAX_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

ROLE_PREFIXES = {
    "info", "admin", "administrator", "contact", "sales", "support", "office",
    "hello", "help", "service", "enquiries", "inquiries", "marketing", "hr",
    "jobs", "careers", "webmaster", "postmaster", "no-reply", "noreply",
    "billing", "accounts", "press", "media", "team",
}

# Small curated list of common disposable/temp-mail domains. There is no
# free, complete, always-current list of these - this catches the common
# ones people actually run into, nothing more.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "fakeinbox.com", "getnada.com", "dispostable.com", "sharklasers.com",
    "maildrop.cc", "mintemail.com", "moakt.com", "mailnesia.com",
    "spamgourmet.com", "mytemp.email", "emailondeck.com", "33mail.com",
}


@lru_cache(maxsize=4096)
def _has_mx(domain: str) -> Optional[bool]:
    """True/False, or None if we genuinely couldn't check (no dnspython, or a
    transient DNS error) — callers must treat None as 'unknown', not
    'invalid'; that distinction matters for the confidence rating below."""
    if not _HAS_DNS:
        return None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        return len(answers) > 0
    except dns.resolver.NoAnswer:
        # some domains accept mail via a bare A record with no explicit MX
        try:
            dns.resolver.resolve(domain, "A", lifetime=5.0)
            return True
        except Exception:
            return False
    except dns.resolver.NXDOMAIN:
        return False
    except Exception:
        return None


def validate_email(email: str) -> dict:
    email = (email or "").strip().lower()
    result = {
        "syntax_valid": False,
        "mx_found": None,
        "disposable": False,
        "role_based": False,
        "confidence": "Invalid",
    }
    if not email or not EMAIL_SYNTAX_RE.match(email):
        return result

    result["syntax_valid"] = True
    local, _, domain = email.partition("@")
    result["role_based"] = local in ROLE_PREFIXES
    result["disposable"] = domain in DISPOSABLE_DOMAINS
    result["mx_found"] = _has_mx(domain)

    if result["disposable"]:
        result["confidence"] = "Low"
    elif result["mx_found"] is False:
        result["confidence"] = "Invalid"
    elif result["mx_found"] is True and not result["role_based"]:
        result["confidence"] = "High"
    elif result["mx_found"] is True and result["role_based"]:
        result["confidence"] = "Medium"
    else:  # MX unknown (DNS check unavailable/failed) - don't over-claim
        result["confidence"] = "Medium" if not result["role_based"] else "Low"

    return result


def best_confidence(emails: str) -> str:
    """Given a ';' or ',' separated string of emails, validate all and
    return the single best confidence label found (for a summary column)."""
    if not emails:
        return ""
    order = {"High": 3, "Medium": 2, "Low": 1, "Invalid": 0}
    best = None
    for raw in re.split(r"[;,]", emails):
        raw = raw.strip()
        if not raw:
            continue
        conf = validate_email(raw)["confidence"]
        if best is None or order[conf] > order[best]:
            best = conf
    return best or ""
