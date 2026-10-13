"""
pitch_runner.py — writes pitches for the eligible rows of one finished search.

Pure orchestration (no web framework): main.py supplies the rows, a flush
callback that saves the CSV, and a log callback. Progress is saved after every
single pitch, so Stop, a daily-limit pause or a crash never loses finished work.
"""

import asyncio

from gemini_client import GeminiError
from pitch_filter import annotate_row, match_offer
from pitch_writer import PitchError, write_pitch

DONE_STATUSES = ("written", "edited")
MAX_CONSECUTIVE_FAILURES = 5


def preview(rows: list, profile, min_score: int) -> dict:
    """Counts who would get a pitch at this minimum score (no changes made)."""
    eligible = written = 0
    for r in rows:
        if r.get("pitch_status") in DONE_STATUSES:
            written += 1
            continue
        probe = dict(r)
        annotate_row(probe, profile, min_score)
        if probe["pitch_status"] == "eligible":
            eligible += 1
    return {"total": len(rows), "eligible": eligible, "already_written": written}


def pick_offer(row: dict, profile) -> dict:
    offer, _score, _why = match_offer(row, profile.offers)
    return offer or profile.offers[0]


async def write_one(client, row: dict, profile) -> None:
    """(Re)writes the pitch for a single row, whatever its status."""
    offer = pick_offer(row, profile)
    row.update(await write_pitch(client, row, offer, profile.sender))
    row["pitch_offer"] = offer.get("OFFER_NAME", row.get("pitch_offer", ""))
    row["pitch_status"] = "written"


async def run(rows: list, profile, client, state: dict, min_score: int, flush, log) -> None:
    """state: dict the caller exposes to the UI (phase, total, done, failed, message)."""
    for r in rows:
        if r.get("pitch_status") not in DONE_STATUSES:
            annotate_row(r, profile, min_score)
    targets = [r for r in rows if r.get("pitch_status") == "eligible"]
    state.update(phase="writing", total=len(targets), done=0, failed=0, message="")
    flush()
    if not targets:
        state.update(phase="done", message="Nobody qualifies at this minimum score.")
        log("✉ No businesses qualify for a pitch at this minimum score.")
        return
    log(f"✉ Writing pitches for {len(targets)} businesses…")
    consecutive = 0
    try:
        for r in targets:
            name = r.get("title") or "a business"
            try:
                await write_one(client, r, profile)
                state["done"] += 1
                consecutive = 0
                log(f"   ✓ {name}: pitch ready ({r.get('pitch_confidence', '?')} confidence, offer '{r.get('pitch_offer', '')}')")
            except GeminiError as e:
                if e.kind in ("daily_limit", "rate_limit"):
                    state.update(phase="paused", message=e.message)
                    log(f"⏸ Paused: {e.message} Everything written so far is saved. Click 'Write pitches' again later to continue.")
                    flush()
                    return
                if e.kind in ("auth", "model_unavailable", "bad_request", "network"):
                    state.update(phase="failed", message=e.message)
                    log(f"❌ {e.message}")
                    flush()
                    return
                _mark_failed(r, e.message, state, log, name)
                consecutive += 1
            except PitchError as e:
                _mark_failed(r, str(e), state, log, name)
                consecutive += 1
            flush()
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                state.update(phase="paused", message="Several pitches in a row failed; stopped to avoid wasting your free quota.")
                log("⏸ Several pitches in a row failed - paused. Check the log lines above.")
                return
        state.update(phase="done", message="")
        log(f"🎉 Pitches done: {state['done']} written, {state['failed']} failed.")
    except asyncio.CancelledError:
        state.update(phase="stopped", message="Stopped by request.")
        log("⏹ Pitch writing stopped - everything written so far is saved.")
        flush()
        raise


def _mark_failed(row, message, state, log, name):
    row["pitch_status"] = "failed"
    row["pitch_error"] = message
    state["failed"] += 1
    log(f"   ✗ {name}: {message}")
