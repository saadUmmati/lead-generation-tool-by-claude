"""
grid.py — splits a bounding box into a grid of sub-search points, for wide
area coverage where a single Google Maps query wouldn't reach far enough.

Honest tradeoff, stated plainly so it isn't a surprise later: gosom runs one
job at a time by default, so N grid cells means roughly N times the total
scraping time of a single search. Worth it for genuinely large areas
(a whole metro region), not worth it for a single small city.
"""

import math


def build_grid(boundingbox: list[float], cell_km: float) -> list[tuple[float, float]]:
    """boundingbox = [south, north, west, east] in degrees (Nominatim's order).
    Returns a list of (lat, lon) grid cell centers covering that box."""
    south, north, west, east = boundingbox
    cell_km = max(cell_km, 1.0)

    lat_span_km = (north - south) * 111.0  # ~111 km per degree of latitude
    mid_lat = (north + south) / 2
    lon_span_km = (east - west) * 111.0 * math.cos(math.radians(mid_lat))

    rows = max(1, math.ceil(lat_span_km / cell_km))
    cols = max(1, math.ceil(lon_span_km / cell_km))

    points = []
    for i in range(rows):
        for j in range(cols):
            lat = south + (north - south) * (i + 0.5) / rows
            lon = west + (east - west) * (j + 0.5) / cols
            points.append((lat, lon))
    return points
