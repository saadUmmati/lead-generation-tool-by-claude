"""
Lead Finder backend.

Owns the entire pipeline under a single job ID that WE generate (not
gosom's), so gosom's quirks (inconsistent JSON casing, single-job
concurrency, no mid-job cancellation) are fully encapsulated here and never
leak into the status API. Requires google-maps-scraper.exe running in -web
mode: google-maps-scraper.exe -web -addr :8080 -data-folder ...

Pipeline: [Google Maps discovery, optionally gridded for wide areas]
          + [OpenStreetMap, optional secondary source]
          -> deduplicate -> per-business website/social crawl
          -> email validation (syntax/MX/disposable/role)
          -> quality scoring -> CSV (+ Excel/JSON on demand)

Stop works at any phase - whatever has been discovered/enriched so far is
always saved before the job ends.
"""

import asyncio
import csv
import io
import json
import os
import time
import uuid
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from enrich import enrich_one_bounded, CONCURRENCY
from site_summary import SUMMARY_FIELDS
from offer_profile import load_offer_profile
from pitch_filter import annotate_row
from validate import best_confidence
from scoring import score_business
from dedup import dedupe_rows
from grid import build_grid
from js_render import BrowserPool
import osm_source

GOSOM_BASE = os.environ.get("GOSOM_BASE", "http://localhost:8080")
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = FastAPI(title="Lead Finder")

BUILD_TAG = "2026-10-12-d1"
print("=" * 60)
print(f"  LEAD FINDER BACKEND STARTING - build {BUILD_TAG}")
print("=" * 60)


@app.get("/api/version")
async def version():
    return {"build": BUILD_TAG}


@app.middleware("http")
async def no_cache_api(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


# ---- single source of truth for every job we run ----
# jobs[job_id] = {
#   "phase": "discovering"|"enriching"|"done"|"stopped"|"failed",
#   "logs": [...], "created_at": ts,
#   "discovery_done": int, "discovery_total": int,   (grid cell progress)
#   "enrich_done": int, "enrich_total": int,
#   "error": str | None,
# }
jobs: dict[str, dict] = {}
job_tasks: dict[str, asyncio.Task] = {}

FIELDNAMES = [
    "title", "category", "address", "phone", "website",
    "emails", "email_confidence_primary", "extra_emails", "email_sources", "email_confidence_extra",
    "facebook", "instagram", "linkedin", "whatsapp", "x_twitter", "youtube", "tiktok",
    "quality_score", "source", "review_rating", "review_count",
    "latitude", "longitude", "google_maps_link",
    "pitch_status", "pitch_offer", "pitch_match_score", "pitch_match_reason",
] + SUMMARY_FIELDS

OFFER_PATH = os.path.join(os.path.dirname(__file__), "..", "my_offer.txt")


def add_log(job_id: str, message: str) -> None:
    jobs[job_id]["logs"].append(message)


class SearchRequest(BaseModel):
    business_type: str
    location: str
    max_results: int = 60
    extract_emails: bool = True
    language: str = "en"
    fast_mode: bool = False
    zoom: int | None = None
    radius: int | None = None
    depth_override: int | None = None
    max_time_override: int | None = None
    use_osm: bool = False
    wide_area_grid: bool = False
    grid_cell_km: float = 10.0
    use_js_fallback: bool = False
    min_pitch_score: int = 40


def estimate_depth(max_results: int) -> int:
    if max_results <= 20:
        return 1
    if max_results <= 60:
        return 3
    if max_results <= 120:
        return 6
    return 10


def estimate_max_time(max_results: int) -> int:
    return max(300, max_results * 6)


# ---------------------------------------------------------------- gosom ----

async def _submit_gosom_job(client: httpx.AsyncClient, keyword: str, req: SearchRequest,
                             lat: float | None = None, lon: float | None = None,
                             radius: int | None = None) -> str:
    payload = {
        "name": keyword,
        "keywords": [keyword],
        "lang": req.language,
        "depth": req.depth_override or estimate_depth(req.max_results),
        "email": req.extract_emails,
        "max_time": req.max_time_override or estimate_max_time(req.max_results),
        "fast_mode": req.fast_mode,
    }
    if lat is not None:
        payload["lat"] = str(lat)
    if lon is not None:
        payload["lon"] = str(lon)
    if radius is not None:
        payload["radius"] = radius
    elif req.radius is not None:
        payload["radius"] = req.radius
    if req.zoom is not None:
        payload["zoom"] = req.zoom

    resp = await client.post(f"{GOSOM_BASE}/api/v1/jobs", json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"gosom rejected the job (status {resp.status_code}): {resp.text}")
    data = resp.json()
    gosom_id = data.get("id") or data.get("ID")
    if not gosom_id:
        raise RuntimeError(f"gosom didn't return a job id: {data}")
    return gosom_id


async def _wait_for_gosom_job(client: httpx.AsyncClient, gosom_id: str, job_id: str) -> bytes:
    """Polls a gosom job until it finishes, then returns its raw CSV bytes.
    Handles gosom's inconsistent field casing (capitalized on GET, lowercase
    documented in its spec) in one place, permanently."""
    logged_working = False
    while True:
        resp = await client.get(f"{GOSOM_BASE}/api/v1/jobs/{gosom_id}", timeout=30)
        resp.raise_for_status()
        job = resp.json()
        status = (job.get("status") or job.get("Status") or "").lower()

        if status == "working" and not logged_working:
            logged_working = True
            add_log(job_id, "⏳ Google Maps scraper is running…")
        elif status == "ok":
            dl = await client.get(f"{GOSOM_BASE}/api/v1/jobs/{gosom_id}/download", timeout=60)
            dl.raise_for_status()
            return dl.content
        elif status == "failed":
            raise RuntimeError(f"gosom job {gosom_id} failed")

        await asyncio.sleep(3)


def _parse_csv_bytes(content: bytes) -> list[dict]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    for row in rows:
        row.setdefault("source", "google_maps")
        if row.get("link"):
            row["google_maps_link"] = row["link"]
        if row.get("emails"):
            row["email_confidence_primary"] = best_confidence(row["emails"])
    return rows


# ------------------------------------------------------------ orchestrator --

def _write_final_csv(job_id: str, rows: list[dict]) -> None:
    """Writes (or overwrites) the job's CSV from whatever rows exist right
    now. Called after every meaningful step so a Stop never loses work,
    regardless of which phase it happens in."""
    path = os.path.join(DOWNLOAD_DIR, f"{job_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


async def _run_pipeline(job_id: str, req: SearchRequest) -> None:
    state = jobs[job_id]
    rows: list[dict] = []

    try:
        # ---- discovery ----
        state["phase"] = "discovering"
        async with httpx.AsyncClient() as client:
            if req.wide_area_grid:
                geo = await osm_source.geocode_location(client, req.location)
                if not geo or not geo.get("boundingbox"):
                    add_log(job_id, f'⚠ Could not geocode "{req.location}" for a grid search — falling back to a single search.')
                    keyword = f"{req.business_type.strip()} in {req.location.strip()}"
                    gosom_id = await _submit_gosom_job(client, keyword, req)
                    add_log(job_id, f'🔍 Searching Google Maps for "{keyword}"…')
                    csv_bytes = await _wait_for_gosom_job(client, gosom_id, job_id)
                    rows.extend(_parse_csv_bytes(csv_bytes))
                else:
                    points = build_grid(geo["boundingbox"], req.grid_cell_km)
                    state["discovery_total"] = len(points)
                    state["discovery_done"] = 0
                    add_log(job_id, (
                        f"🗺 Wide-area grid: {len(points)} cells to scan sequentially "
                        f"(gosom runs one job at a time by default, so this takes "
                        f"roughly {len(points)}x longer than a single search)."
                    ))
                    radius_m = int(req.grid_cell_km * 550)  # slight overlap between cells
                    for i, (lat, lon) in enumerate(points, start=1):
                        keyword = f"{req.business_type.strip()} in {req.location.strip()}"
                        add_log(job_id, f"🔍 Grid cell {i}/{len(points)}: scanning around ({lat:.3f}, {lon:.3f})…")
                        gosom_id = await _submit_gosom_job(client, keyword, req, lat=lat, lon=lon, radius=radius_m)
                        csv_bytes = await _wait_for_gosom_job(client, gosom_id, job_id)
                        cell_rows = _parse_csv_bytes(csv_bytes)
                        rows.extend(cell_rows)
                        state["discovery_done"] = i
                        add_log(job_id, f"   → cell {i}/{len(points)}: {len(cell_rows)} businesses found.")
            else:
                keyword = f"{req.business_type.strip()} in {req.location.strip()}"
                add_log(job_id, f'🔍 Searching Google Maps for "{keyword}"…')
                gosom_id = await _submit_gosom_job(client, keyword, req)
                csv_bytes = await _wait_for_gosom_job(client, gosom_id, job_id)
                rows.extend(_parse_csv_bytes(csv_bytes))

            add_log(job_id, f"✅ Google Maps discovery complete: {len(rows)} businesses found.")

            if req.use_osm:
                add_log(job_id, "🗺 Checking OpenStreetMap for additional coverage…")
                osm_rows, note = await osm_source.search_osm(client, req.business_type, req.location)
                add_log(job_id, f"   → {note}")
                rows.extend(osm_rows)

        rows = dedupe_rows(rows)
        add_log(job_id, f"🧹 After deduplication: {len(rows)} unique businesses.")
        _write_final_csv(job_id, rows)  # save discovery-only results immediately, before enrichment

        # ---- enrichment (website + social crawl) ----
        state["phase"] = "enriching"
        state["enrich_total"] = len(rows)
        state["enrich_done"] = 0
        add_log(job_id, f"🌐 Crawling websites & socials for {len(rows)} businesses…")

        browser_pool = None
        if req.use_js_fallback:
            browser_pool = BrowserPool()
            started, note = await browser_pool.start()
            add_log(job_id, f"🧭 {note}")
            if not started:
                browser_pool = None

        profile = load_offer_profile(OFFER_PATH)
        if profile.ok:
            add_log(job_id, f"📝 Offer form loaded: {len(profile.offers)} offer(s).")
            for prob in profile.problems:
                add_log(job_id, f"   ⚠ my_offer.txt: {prob}")
        else:
            add_log(job_id, "📝 Pitch matching skipped - my_offer.txt is missing or not filled in.")

        write_lock = asyncio.Lock()
        sem = asyncio.Semaphore(CONCURRENCY)
        done_count = 0
        child_tasks: list[asyncio.Task] = []

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0)"}
            ) as client:

                async def _process(row: dict) -> None:
                    nonlocal done_count
                    name = row.get("title") or row.get("name") or "a business"
                    add_log(job_id, f"🔎 Checking {name} — website & social links…")
                    extra = await enrich_one_bounded(client, sem, row, browser_pool=browser_pool)
                    row.update(extra)
                    if row.get("extra_emails"):
                        row["email_confidence_extra"] = best_confidence(row["extra_emails"])
                    row["quality_score"] = score_business(row)
                    annotate_row(row, profile, req.min_pitch_score)

                    async with write_lock:
                        done_count += 1
                        state["enrich_done"] = done_count
                        _write_final_csv(job_id, rows)  # re-flush after every completion - nothing lost on Stop

                    found = [p for p in ("facebook", "instagram", "linkedin", "whatsapp", "x_twitter", "youtube", "tiktok") if row.get(p)]
                    if row.get("extra_emails"):
                        found = ["extra email(s)"] + found
                    add_log(job_id, f"   → {name}: {'found ' + ', '.join(found) if found else 'nothing new found publicly'} (score {row['quality_score']})")

                child_tasks = [asyncio.create_task(_process(row)) for row in rows]
                await asyncio.gather(*child_tasks, return_exceptions=True)
        finally:
            if browser_pool:
                await browser_pool.stop()

        # rows without a website never went through _process (enrich_one_bounded
        # returns instantly for them, so they're already handled) - but rows
        # with no email at all still need a score:
        for row in rows:
            row.setdefault("quality_score", score_business(row))
            if not row.get("pitch_status"):
                annotate_row(row, profile, req.min_pitch_score)
        _write_final_csv(job_id, rows)
        if profile.ok:
            n_ok = sum(1 for r in rows if r.get("pitch_status") == "eligible")
            add_log(job_id, f"✉ {n_ok} of {len(rows)} businesses qualify for a pitch (min score {req.min_pitch_score}).")

        state["phase"] = "done"
        add_log(job_id, "🎉 All done — your results are ready to download.")

    except asyncio.CancelledError:
        try:
            for t in child_tasks:  # noqa: F821 - only exists once enrichment phase started
                t.cancel()
        except NameError:
            pass  # cancelled during discovery, before enrichment tasks existed - nothing to cancel
        _write_final_csv(job_id, rows)
        state["phase"] = "stopped"
        add_log(job_id, "⏹ Stopped by request — everything found so far is saved.")
    except Exception as e:
        _write_final_csv(job_id, rows)
        state["phase"] = "failed"
        state["error"] = str(e)
        add_log(job_id, f"❌ Error: {e}")


# --------------------------------------------------------------- endpoints --

@app.post("/api/search")
async def create_search(req: SearchRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "phase": "discovering",
        "logs": [],
        "created_at": time.time(),
        "discovery_done": 0,
        "discovery_total": 0,
        "enrich_done": 0,
        "enrich_total": 0,
        "error": None,
    }
    task = asyncio.create_task(_run_pipeline(job_id, req))
    job_tasks[job_id] = task
    return {"id": job_id}


@app.get("/api/search/{job_id}")
async def get_status(job_id: str):
    state = jobs.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown job id.")
    elapsed = time.time() - state["created_at"]
    return {
        "id": job_id,
        "status": state["phase"],
        "elapsed_seconds": round(elapsed),
        "discovery_done": state["discovery_done"],
        "discovery_total": state["discovery_total"],
        "enrichment_done": state["enrich_done"],
        "enrichment_total": state["enrich_total"],
        "error_detail": state.get("error"),
        "logs": state["logs"],
    }


@app.post("/api/search/{job_id}/stop")
async def stop_job(job_id: str):
    task = job_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return {"status": "stopping"}
    state = jobs.get(job_id)
    return {"status": state["phase"] if state else "unknown"}


def _rows_from_csv(job_id: str) -> list[dict]:
    path = os.path.join(DOWNLOAD_DIR, f"{job_id}.csv")
    if not os.path.exists(path):
        raise HTTPException(status_code=409, detail="No results ready to download yet.")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@app.get("/api/search/{job_id}/download")
async def download(job_id: str, format: str = "csv"):
    state = jobs.get(job_id)
    if not state or state["phase"] not in ("done", "stopped", "failed"):
        raise HTTPException(status_code=409, detail="Job isn't finished or stopped yet.")

    path = os.path.join(DOWNLOAD_DIR, f"{job_id}.csv")
    if not os.path.exists(path):
        raise HTTPException(status_code=409, detail="No results were produced.")

    if format == "csv":
        return FileResponse(path, filename="leads.csv", media_type="text/csv")

    rows = _rows_from_csv(job_id)

    if format == "json":
        payload = json.dumps(rows, indent=2)
        return StreamingResponse(
            io.BytesIO(payload.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=leads.json"},
        )

    if format == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        headers = list(rows[0].keys()) if rows else FIELDNAMES
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=leads.xlsx"},
        )

    raise HTTPException(status_code=400, detail="format must be csv, xlsx, or json")


# Serve the frontend last, so /api/* routes above take priority.
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
