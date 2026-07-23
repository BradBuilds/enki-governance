"""
Simple gazetteer — maps location names in headlines to coordinates.

Used for non-GDELT sources (HN, Reddit, RSS) where geographic data
isn't provided by the API. Scans headlines + summaries for known
country/city names and returns the best match with coordinates.

~200 major locations covering countries, capitals, and geopolitically
significant cities. Fast (dict lookup, no API calls).
"""

import re

# Major locations: name → (latitude, longitude, type)
# Types: country, capital, city, region
_LOCATIONS: dict[str, tuple[float, float, str]] = {
    # Major countries
    "ukraine": (48.38, 31.17, "country"), "russia": (55.75, 37.62, "country"),
    "china": (35.86, 104.20, "country"), "taiwan": (23.70, 120.96, "country"),
    "iran": (32.43, 53.69, "country"), "iraq": (33.22, 43.68, "country"),
    "israel": (31.05, 34.85, "country"), "palestine": (31.95, 35.23, "country"),
    "gaza": (31.50, 34.47, "region"), "syria": (34.80, 38.99, "country"),
    "afghanistan": (33.94, 67.71, "country"), "pakistan": (30.38, 69.35, "country"),
    "india": (20.59, 78.96, "country"), "japan": (36.20, 138.25, "country"),
    "north korea": (40.34, 127.51, "country"), "south korea": (35.91, 127.77, "country"),
    "germany": (51.17, 10.45, "country"), "france": (46.23, 2.21, "country"),
    "united kingdom": (55.38, -3.44, "country"), "italy": (41.87, 12.57, "country"),
    "spain": (40.46, -3.75, "country"), "poland": (51.92, 19.15, "country"),
    "turkey": (38.96, 35.24, "country"), "saudi arabia": (23.89, 45.08, "country"),
    "brazil": (14.24, -51.93, "country"), "mexico": (23.63, -102.55, "country"),
    "canada": (56.13, -106.35, "country"), "australia": (-25.27, 133.78, "country"),
    "egypt": (26.82, 30.80, "country"), "nigeria": (9.08, 8.68, "country"),
    "south africa": (-30.56, 22.94, "country"), "ethiopia": (9.15, 40.49, "country"),
    "kenya": (-0.02, 37.91, "country"), "indonesia": (-0.79, 113.92, "country"),
    "philippines": (12.88, 121.77, "country"), "vietnam": (14.06, 108.28, "country"),
    "thailand": (15.87, 100.99, "country"), "myanmar": (21.91, 95.96, "country"),
    "yemen": (15.55, 48.52, "country"), "libya": (26.34, 17.23, "country"),
    "sudan": (12.86, 30.22, "country"), "somalia": (5.15, 46.20, "country"),
    "venezuela": (6.42, -66.59, "country"), "colombia": (4.57, -74.30, "country"),
    "argentina": (-38.42, -63.62, "country"), "chile": (-35.68, -71.54, "country"),
    "cuba": (21.52, -77.78, "country"), "haiti": (18.97, -72.29, "country"),

    # Major cities / capitals
    "kyiv": (50.45, 30.52, "capital"), "moscow": (55.76, 37.62, "capital"),
    "beijing": (39.90, 116.41, "capital"), "shanghai": (31.23, 121.47, "city"),
    "tokyo": (35.68, 139.69, "capital"), "seoul": (37.57, 126.98, "capital"),
    "new delhi": (28.61, 77.21, "capital"), "mumbai": (19.08, 72.88, "city"),
    "london": (51.51, -0.13, "capital"), "paris": (48.86, 2.35, "capital"),
    "berlin": (52.52, 13.40, "capital"), "rome": (41.90, 12.50, "capital"),
    "madrid": (40.42, -3.70, "capital"), "brussels": (50.85, 4.35, "capital"),
    "washington": (38.91, -77.04, "capital"), "new york": (40.71, -74.01, "city"),
    "los angeles": (34.05, -118.24, "city"), "san francisco": (37.77, -122.42, "city"),
    "chicago": (41.88, -87.63, "city"), "houston": (29.76, -95.37, "city"),
    "silicon valley": (37.39, -122.03, "region"), "pentagon": (38.87, -77.06, "city"),
    "ottawa": (45.42, -75.70, "capital"), "toronto": (43.65, -79.38, "city"),
    "mexico city": (19.43, -99.13, "capital"), "sao paulo": (-23.55, -46.63, "city"),
    "buenos aires": (-34.60, -58.38, "capital"), "bogota": (4.71, -74.07, "capital"),
    "cairo": (30.04, 31.24, "capital"), "lagos": (6.52, 3.38, "city"),
    "nairobi": (-1.29, 36.82, "capital"), "johannesburg": (-26.20, 28.05, "city"),
    "istanbul": (41.01, 28.98, "city"), "ankara": (39.93, 32.86, "capital"),
    "tehran": (35.69, 51.39, "capital"), "riyadh": (24.69, 46.72, "capital"),
    "dubai": (25.20, 55.27, "city"), "tel aviv": (32.09, 34.78, "city"),
    "jerusalem": (31.77, 35.23, "city"), "baghdad": (33.31, 44.37, "capital"),
    "kabul": (34.53, 69.17, "capital"), "islamabad": (33.69, 73.04, "capital"),
    "taipei": (25.03, 121.57, "capital"), "hong kong": (22.40, 114.11, "city"),
    "singapore": (1.35, 103.82, "capital"), "bangkok": (13.76, 100.50, "capital"),
    "hanoi": (21.03, 105.85, "capital"), "jakarta": (-6.21, 106.85, "capital"),
    "manila": (14.60, 120.98, "capital"), "sydney": (-33.87, 151.21, "city"),
    "canberra": (-35.28, 149.13, "capital"), "havana": (23.11, -82.37, "capital"),
    "caracas": (10.48, -66.90, "capital"), "lima": (-12.05, -77.04, "capital"),
    "santiago": (-33.45, -70.67, "capital"),

    # Geopolitically significant places
    "crimea": (44.95, 34.10, "region"), "donbas": (48.00, 37.80, "region"),
    "kherson": (46.64, 32.62, "city"), "zaporizhzhia": (47.84, 35.14, "city"),
    "mariupol": (47.10, 37.54, "city"), "bakhmut": (48.59, 38.00, "city"),
    "taiwan strait": (24.50, 119.50, "region"), "south china sea": (12.00, 114.00, "region"),
    "strait of hormuz": (26.57, 56.25, "region"), "suez canal": (30.70, 32.34, "region"),
    "nato": (50.88, 4.43, "region"), "european union": (50.85, 4.35, "region"),
    "kremlin": (55.75, 37.62, "city"), "white house": (38.90, -77.04, "city"),
    "wall street": (40.71, -74.01, "city"), "davos": (46.80, 9.84, "city"),
    "geneva": (46.20, 6.15, "city"), "the hague": (52.07, 4.30, "city"),
}

# Precompile patterns for multi-word locations (sorted longest first)
_PATTERNS = sorted(_LOCATIONS.keys(), key=len, reverse=True)


def extract_location(headline: str, summary: str = "") -> dict | None:
    """Extract the most significant location from text.

    Returns dict with lat, lon, name, type or None.
    Prefers countries/capitals over cities for ambiguity resolution.
    """
    text = f"{headline} {summary}".lower()

    best = None
    best_priority = -1

    for name in _PATTERNS:
        # Word boundary check to avoid partial matches
        if f" {name} " in f" {text} " or text.startswith(name + " ") or text.endswith(" " + name):
            lat, lon, loc_type = _LOCATIONS[name]
            # Priority: country > capital > region > city
            priority = {"country": 4, "capital": 3, "region": 2, "city": 1}.get(loc_type, 0)
            if priority > best_priority:
                best = {"name": name.title(), "latitude": lat, "longitude": lon, "type": loc_type}
                best_priority = priority

    return best


def reverse_geocode(lat: float, lon: float, use_nominatim: bool = True) -> dict | None:
    """Reverse geocode: lat/lon → nearest known location.

    First tries the local gazetteer (fast, ~200 major locations).
    If no match within 100km and use_nominatim=True, falls back to
    Nominatim/OpenStreetMap (1 req/s rate limit).

    Returns:
        {name, type, latitude, longitude, distance_km,
         address?, city?, state?, country?, country_code?}
    """
    import math

    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    # Local gazetteer lookup — nearest within 100km
    best_name = None
    best_dist = 100.0  # km threshold
    best_entry = None

    for name, (loc_lat, loc_lon, loc_type) in _LOCATIONS.items():
        dist = _haversine(lat, lon, loc_lat, loc_lon)
        if dist < best_dist:
            best_dist = dist
            best_name = name
            best_entry = (loc_lat, loc_lon, loc_type)

    if best_name and best_entry:
        result = {
            "name": best_name.title(),
            "type": best_entry[2],
            "latitude": best_entry[0],
            "longitude": best_entry[1],
            "distance_km": round(best_dist, 1),
            "source": "gazetteer",
        }
        # If it's a country match (>50km away), also try Nominatim for city-level
        if best_dist > 50 and use_nominatim:
            nom = _nominatim_reverse(lat, lon)
            if nom:
                result.update(nom)
                result["source"] = "nominatim"
        return result

    # Nominatim fallback for locations not in gazetteer
    if use_nominatim:
        nom = _nominatim_reverse(lat, lon)
        if nom:
            nom["source"] = "nominatim"
            return nom

    return None


def _nominatim_reverse(lat: float, lon: float) -> dict | None:
    """Reverse geocode via Nominatim/OpenStreetMap. Rate limited: 1 req/s."""
    try:
        import urllib.request
        import json
        import time

        url = (
            f"https://nominatim.openstreetmap.org/reverse"
            f"?format=jsonv2&lat={lat}&lon={lon}&zoom=14"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "EnkiCommandCenter/1.0 (admin@example.com)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        addr = data.get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet")
        state = addr.get("state")
        country = addr.get("country")
        country_code = addr.get("country_code", "").upper()

        name_parts = []
        if city:
            name_parts.append(city)
        if state and state != city:
            name_parts.append(state)
        if country:
            name_parts.append(country)

        return {
            "name": ", ".join(name_parts) if name_parts else data.get("display_name", f"{lat},{lon}"),
            "type": "address",
            "address": data.get("display_name"),
            "city": city,
            "state": state,
            "country": country,
            "country_code": country_code,
            "latitude": lat,
            "longitude": lon,
            "distance_km": 0,
        }
    except Exception:
        return None
