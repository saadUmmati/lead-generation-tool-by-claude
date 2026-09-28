"""
osm_source.py — optional secondary discovery source using OpenStreetMap:
Nominatim for geocoding the location, Overpass API for the actual business
search within that area.

Both are free public services with usage policies. This module makes a
small, polite number of requests per search (one geocode + one Overpass
query) and identifies itself with a descriptive User-Agent, per each
service's usage policy. If either is unreachable, slow, or rate-limits us,
we log why and simply continue without OSM data rather than retrying
aggressively or working around the limit.
"""

from typing import Optional
import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "LeadFinder/1.0 (personal self-hosted tool; contact: local use only)"

# Common business-type -> OSM tag mapping. This is not exhaustive - OSM's
# tagging scheme doesn't cleanly cover every business category (e.g. generic
# "manufacturer" or "construction company" have no dedicated tag). Unmapped
# types are skipped for OSM rather than guessed at unreliably.
TAG_MAP: dict[str, list[tuple[str, str]]] = {
    "dentist": [("amenity", "dentist")],
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "coffee shop": [("amenity", "cafe")],
    "hotel": [("tourism", "hotel")],
    "lawyer": [("office", "lawyer")],
    "law firm": [("office", "lawyer")],
    "plumber": [("craft", "plumber")],
    "electrician": [("craft", "electrician")],
    "hairdresser": [("shop", "hairdresser")],
    "hair salon": [("shop", "hairdresser")],
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "school": [("amenity", "school")],
    "university": [("amenity", "university")],
    "hospital": [("amenity", "hospital")],
    "clinic": [("amenity", "clinic")],
    "pharmacy": [("amenity", "pharmacy")],
    "bank": [("amenity", "bank")],
    "real estate": [("office", "estate_agent")],
    "real estate agency": [("office", "estate_agent")],
    "accounting": [("office", "accountant")],
    "accountant": [("office", "accountant")],
    "insurance": [("office", "insurance")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "supermarket")],
    "bakery": [("shop", "bakery")],
    "car repair": [("shop", "car_repair")],
    "mechanic": [("shop", "car_repair")],
    "software company": [("office", "it"), ("office", "company")],
    "marketing agency": [("office", "advertising_agency")],
    "veterinarian": [("amenity", "veterinary")],
    "vet": [("amenity", "veterinary")],
}


def resolve_tags(business_type: str) -> Optional[list[tuple[str, str]]]:
    key = business_type.strip().lower()
    if key in TAG_MAP:
        return TAG_MAP[key]
    for known_key, tags in TAG_MAP.items():
        if known_key in key or key in known_key:
            return tags
    return None


async def geocode_location(client: httpx.AsyncClient, location: str) -> Optional[dict]:
    """Returns {'lat', 'lon', 'boundingbox': [south, north, west, east]} or None."""
    try:
        resp = await client.get(
            NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        item = data[0]
        bbox = item.get("boundingbox")  # Nominatim order: [south, north, west, east], as strings
        return {
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "boundingbox": [float(x) for x in bbox] if bbox else None,
        }
    except Exception:
        return None


def build_overpass_query(tags: list[tuple[str, str]], bbox: list[float]) -> str:
    south, north, west, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    clauses = []
    for key, value in tags:
        clauses.append(f'node["{key}"="{value}"]({bbox_str});')
        clauses.append(f'way["{key}"="{value}"]({bbox_str});')
    return f"[out:json][timeout:25];({''.join(clauses)});out center 200;"


def row_from_osm_element(el: dict) -> dict:
    tags = el.get("tags", {})
    addr_parts = [
        tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
        tags.get("addr:city", ""), tags.get("addr:postcode", ""),
    ]
    address = " ".join(p for p in addr_parts if p).strip()
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    return {
        "title": tags.get("name", ""),
        "category": tags.get("amenity") or tags.get("shop") or tags.get("office") or tags.get("craft") or "",
        "address": address,
        "phone": tags.get("phone") or tags.get("contact:phone") or "",
        "website": tags.get("website") or tags.get("contact:website") or "",
        "latitude": lat,
        "longitude": lon,
        "source": "openstreetmap",
    }


async def search_osm(client: httpx.AsyncClient, business_type: str, location: str) -> tuple[list[dict], str]:
    """Returns (rows, note) - note explains any limitation/failure, meant to
    be logged so it's never a silent gap in coverage."""
    geo = await geocode_location(client, location)
    if not geo or not geo.get("boundingbox"):
        return [], f'Could not geocode "{location}" via OpenStreetMap — skipping this source.'

    tags = resolve_tags(business_type)
    if not tags:
        return [], f'No OpenStreetMap tag mapping for "{business_type}" — skipping this source (Google Maps coverage still applies).'

    query = build_overpass_query(tags, geo["boundingbox"])
    try:
        resp = await client.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=40,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [], f"OpenStreetMap/Overpass request failed ({e}) — skipping this source."

    rows = [row_from_osm_element(el) for el in data.get("elements", [])]
    rows = [r for r in rows if r["title"]]
    return rows, f"OpenStreetMap contributed {len(rows)} additional businesses."
