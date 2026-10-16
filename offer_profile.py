"""
offer_profile.py — reads my_offer.txt (the plain-text form the user fills in).

Format:
    # comment
    === SENDER ===          (once)
    KEY: value
    === OFFER ===           (repeatable)
    KEY: value
    continuation lines (not matching KEY:) are appended to the previous field.

`[ fill here ]` counts as empty. Never raises on bad content: problems are
collected in profile.problems and `profile.ok` says whether anything usable
was loaded.
"""

import difflib
import re
from dataclasses import dataclass, field

SENDER_KEYS = [
    "YOUR_NAME", "YOUR_ROLE", "COMPANY_NAME", "COMPANY_WEBSITE", "REPLY_TO_EMAIL",
    "PHONE_OR_WHATSAPP", "COMPANY_ONE_LINER", "TONE", "PITCH_LANGUAGE",
    "OPT_OUT_LINE", "POSTAL_ADDRESS",
]
OFFER_KEYS = [
    "OFFER_NAME", "WHAT_I_SELL", "TARGET_CUSTOMERS", "MATCH_CATEGORIES", "MATCH_KEYWORDS", "SKIP_IF_KEYWORDS",
    "PROBLEM_I_SOLVE", "OUTCOME_FOR_THEM", "PROOF_POINTS", "HOOK", "CALL_TO_ACTION",
    "PRICE_HINT", "DONT_SAY", "EXTRA_NOTES",
]
LIST_KEYS = {"MATCH_CATEGORIES", "MATCH_KEYWORDS", "SKIP_IF_KEYWORDS"}

_SECTION_RE = re.compile(r"^===\s*([A-Za-z _]+?)\s*===\s*$")
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ ]*?)\s*:\s*(.*)$")
_PLACEHOLDER_RE = re.compile(r"^\[\s*fill here\s*\]$", re.I)
_PLACEHOLDER_DOMAINS = (".example", "example.com", "example.org")


@dataclass
class OfferProfile:
    sender: dict = field(default_factory=dict)
    offers: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.offers)


def _is_empty(v: str) -> bool:
    return not v.strip() or bool(_PLACEHOLDER_RE.match(v.strip()))


def _split_list(v: str) -> list:
    return [p.strip() for p in re.split(r"[,;\n]", v) if p.strip()]


def _norm_key(k: str) -> str:
    return re.sub(r"\s+", "_", k.strip().upper())


def load_offer_profile(path: str) -> OfferProfile:
    prof = OfferProfile()
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        prof.problems.append(f"{path} not found")
        return prof
    except OSError as e:
        prof.problems.append(f"could not read {path}: {e}")
        return prof

    section = None            # "SENDER" / "OFFER" / None
    current = None            # dict of the block being filled
    last_key = None
    blocks = []               # (section, dict, lineno)

    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if line.lstrip().startswith("#") or not line.strip():
            continue
        m = _SECTION_RE.match(line.strip())
        if m:
            section = _norm_key(m.group(1))
            if section not in ("SENDER", "OFFER"):
                prof.problems.append(f"line {n}: unknown section '{m.group(1)}'")
                section, current = None, None
                continue
            current = {}
            last_key = None
            blocks.append((section, current, n))
            continue
        if current is None:
            prof.problems.append(f"line {n}: text outside any section ignored")
            continue
        km = _KEY_RE.match(line.strip())
        valid = SENDER_KEYS if section == "SENDER" else OFFER_KEYS
        if km and km.group(1).strip().replace(" ", "_").isupper():
            key = _norm_key(km.group(1))
            if key in valid:
                current[key] = km.group(2).strip()
                last_key = key
            else:
                close = difflib.get_close_matches(key, valid, n=1, cutoff=0.7)
                hint = f" - did you mean {close[0]}?" if close else ""
                prof.problems.append(f"line {n}: unknown field {key}{hint}")
                last_key = None
        elif last_key:
            current[last_key] = (current[last_key] + " " + line.strip()).strip()
        else:
            prof.problems.append(f"line {n}: could not understand '{line.strip()[:40]}'")

    sender_blocks = [b for b in blocks if b[0] == "SENDER"]
    if len(sender_blocks) > 1:
        prof.problems.append("more than one SENDER section; using the first")
    if sender_blocks:
        prof.sender = {k: v for k, v in sender_blocks[0][1].items() if not _is_empty(v)}
    else:
        prof.problems.append("no === SENDER === section found")

    for _sec, block, n in [b for b in blocks if b[0] == "OFFER"]:
        cleaned = {k: v for k, v in block.items() if not _is_empty(v)}
        if not cleaned:
            continue  # untouched template block
        for k in LIST_KEYS:
            if k in cleaned:
                cleaned[k] = _split_list(cleaned[k])
        if not cleaned.get("WHAT_I_SELL"):
            prof.problems.append(f"offer at line {n} has no WHAT_I_SELL - skipped")
            continue
        cleaned.setdefault("OFFER_NAME", f"Offer {len(prof.offers) + 1}")
        prof.offers.append(cleaned)

    site = prof.sender.get("COMPANY_WEBSITE", "").lower()
    if site and any(d in site for d in _PLACEHOLDER_DOMAINS):
        prof.problems.append("COMPANY_WEBSITE looks like a placeholder (.example) - replace it with your real site")
    for key in ("YOUR_NAME", "COMPANY_NAME"):
        if key not in prof.sender:
            prof.problems.append(f"SENDER {key} is empty - pitches will sign off without it")
    if not prof.offers:
        prof.problems.append("no filled-in OFFER section found")
    return prof


def render_offer_file(sender: dict, offers: list) -> str:
    """Builds my_offer.txt text from form values (the reverse of load_offer_profile)."""
    def val(v):
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        return " ".join(str(v or "").split())

    out = ["# Written by the Lead Finder 'one-run' panel. You can edit this file by hand too.", "",
           "=== SENDER ==="]
    for k in SENDER_KEYS:
        if val(sender.get(k)):
            out.append(f"{k}: {val(sender[k])}")
    for offer in offers:
        out += ["", "=== OFFER ==="]
        for k in OFFER_KEYS:
            if val(offer.get(k)):
                out.append(f"{k}: {val(offer[k])}")
    return "\n".join(out) + "\n"
