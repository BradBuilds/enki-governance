"""
Track analysis — GPS noise removal + road-snapped routing.

Approach: cluster ALL points by proximity first, then determine
which clusters are stops vs drive-throughs based on dwell time.
Only the transitions between clusters get sent to OSRM.
"""

import math
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


def analyze_device_track(
    tenant_id: str,
    device_id: str,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    stop_threshold_minutes: float = 2.0,
    stop_radius_meters: float = 100.0,
    dsn: Optional[str] = None,
) -> dict:
    points = _get_positions(tenant_id, device_id, after, before, dsn)
    if not points:
        return _empty_result(device_id, after, before)

    # ── Step 1: Drop teleports ──
    clean = _drop_teleports(points, max_speed_kmh=150)
    if len(clean) < 2:
        return _empty_result(device_id, after, before)

    # ── Step 2: Cluster consecutive nearby points ──
    clusters = _cluster_consecutive(clean, radius_m=300)

    # ── Step 3: Classify clusters ──
    # Stop clusters → single centroid (collapse bounce)
    # Movement clusters (1-2 points, brief pass-throughs) → keep as route guides
    stops = []
    waypoints = []

    for cluster in clusters:
        centroid_lat = sum(p["lat"] for p in cluster) / len(cluster)
        centroid_lon = sum(p["lon"] for p in cluster) / len(cluster)
        duration_min = (cluster[-1]["timestamp"] - cluster[0]["timestamp"]).total_seconds() / 60.0

        is_stop = len(cluster) >= 3 and duration_min >= stop_threshold_minutes

        if is_stop:
            stops.append({
                "lat": round(centroid_lat, 6),
                "lon": round(centroid_lon, 6),
                "arrived": cluster[0]["timestamp"].isoformat(),
                "departed": cluster[-1]["timestamp"].isoformat(),
                "duration_minutes": round(duration_min, 1),
                "label": "",
                "point_count": len(cluster),
            })
            # Single centroid for stop
            waypoints.append({
                "lat": centroid_lat, "lon": centroid_lon,
                "timestamp": cluster[0]["timestamp"],
                "accuracy": None, "label": "", "is_stop": True,
            })
        else:
            # Movement cluster — keep first point as route guide
            # This tells OSRM which roads you actually took
            waypoints.append(cluster[0])

    # ── Step 3b: Merge first and last stop if same location (home→out→home) ──
    if len(stops) >= 2 and len(waypoints) >= 2:
        first_wp = waypoints[0]
        last_wp = waypoints[-1]
        dist_start_end = _haversine(first_wp["lat"], first_wp["lon"], last_wp["lat"], last_wp["lon"])
        if dist_start_end < 500:  # Start and end within 500m = same place
            # Merge: keep last stop (longer dwell), combine duration
            stops[0]["duration_minutes"] += stops[-1]["duration_minutes"]
            stops[0]["point_count"] += stops[-1]["point_count"]
            stops[0]["departed"] = stops[-1]["departed"]
            stops.pop()  # Remove last (it's same as first)
            # Use first waypoint's coords for both start and end
            waypoints[-1] = dict(waypoints[-1], lat=first_wp["lat"], lon=first_wp["lon"])

    # ── Step 3c: Round-trip detection ──
    # If start ≈ end (within 500m), it's a round trip.
    # Use outbound movement waypoints reversed for the return to get same roads.
    if len(waypoints) >= 3 and len(stops) >= 1:
        first_wp = waypoints[0]
        last_wp = waypoints[-1]
        if _haversine(first_wp["lat"], first_wp["lon"], last_wp["lat"], last_wp["lon"]) < 500:
            # Find the farthest stop from home (the destination)
            dest_stop = max(stops, key=lambda s: _haversine(
                first_wp["lat"], first_wp["lon"], s["lat"], s["lon"]))

            # Split waypoints into outbound (before destination) and return (after)
            dest_idx = None
            for wi, wp in enumerate(waypoints):
                if abs(wp["lat"] - dest_stop["lat"]) < 0.001 and abs(wp["lon"] - dest_stop["lon"]) < 0.001:
                    dest_idx = wi
                    break

            if dest_idx is not None:
                # Clean round trip: home → destination → home
                # No intermediate waypoints — let OSRM find the road
                home_wp = waypoints[0]
                dest_wp = waypoints[dest_idx]
                waypoints = [home_wp, dest_wp, home_wp]

    if len(waypoints) < 2:
        waypoints = [clean[0], clean[-1]]

    # ── Step 4: Road-snap ──
    route = [[p["lon"], p["lat"]] for p in waypoints]
    road_snapped = False
    matched_legs = []
    try:
        from storage.signals.map_matching import match_to_roads
        match_result = match_to_roads(waypoints)
        if match_result and match_result["matched_route"]:
            route = match_result["matched_route"]
            matched_legs = match_result.get("legs", [])
            road_snapped = True
    except Exception:
        pass

    # ── Step 4b: Tag legs as outbound/return ──
    # Simple split: first half of legs = outbound, second half = return
    if matched_legs:
        mid = len(matched_legs) // 2
        for li, leg in enumerate(matched_legs):
            leg["direction"] = "outbound" if li < mid else "return"

    # ── Step 5: Stats ──
    if road_snapped and matched_legs:
        total_dist = sum(leg["distance_m"] for leg in matched_legs)
    else:
        total_dist = sum(
            _haversine(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                       waypoints[i]["lat"], waypoints[i]["lon"])
            for i in range(1, len(waypoints))
        )

    first_ts = clean[0]["timestamp"]
    last_ts = clean[-1]["timestamp"]
    total_hours = (last_ts - first_ts).total_seconds() / 3600.0
    stop_hours = sum(s["duration_minutes"] for s in stops) / 60.0

    return {
        "device_id": device_id,
        "road_snapped": road_snapped,
        "period": {"after": first_ts.isoformat(), "before": last_ts.isoformat()},
        "points": [{"lat": p["lat"], "lon": p["lon"], "timestamp": p["timestamp"].isoformat(),
                     "label": p.get("label", "")} for p in waypoints],
        "route": route,
        "legs": matched_legs,
        "stops": stops,
        "segments": [],
        "stats": {
            "total_points": len(points),
            "clean_points": len(waypoints),
            "total_distance_km": round(total_dist / 1000, 2),
            "total_duration_hours": round(total_hours, 2),
            "stop_count": len(stops),
            "total_stop_duration_hours": round(stop_hours, 2),
            "moving_duration_hours": round(max(0, total_hours - stop_hours), 2),
            "road_snapped": road_snapped,
        },
    }


def detect_trips(
    tenant_id: str,
    device_id: str,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    gap_hours: float = 4.0,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Detect discrete trips from GPS data.

    A trip boundary is defined as a gap of >= gap_hours between GPS points.
    Each trip gets:
      - start_time, end_time, duration_hours
      - start_location, end_location (reverse geocoded)
      - stops (with addresses, dwell times)
      - distance_km, point_count
      - route (road-snapped if OSRM available)

    Returns list of trip dicts, newest first.
    """
    from datetime import timedelta

    points = _get_positions(tenant_id, device_id, after, before, dsn)
    if not points:
        return []

    clean = _drop_teleports(points, max_speed_kmh=150)
    if len(clean) < 2:
        return []

    # Split into trips by time gaps
    trip_segments: list[list[dict]] = []
    current_segment: list[dict] = [clean[0]]

    for i in range(1, len(clean)):
        gap = (clean[i]["timestamp"] - clean[i - 1]["timestamp"]).total_seconds() / 3600.0
        if gap >= gap_hours:
            if len(current_segment) >= 2:
                trip_segments.append(current_segment)
            current_segment = [clean[i]]
        else:
            current_segment.append(clean[i])

    if len(current_segment) >= 2:
        trip_segments.append(current_segment)

    # Reverse geocode helper (cached to avoid duplicate lookups)
    from core.extraction.gazetteer import reverse_geocode
    _geo_cache: dict[str, dict | None] = {}

    def _geocode(lat: float, lon: float) -> dict:
        key = f"{round(lat, 3)},{round(lon, 3)}"
        if key not in _geo_cache:
            try:
                _geo_cache[key] = reverse_geocode(lat, lon, use_nominatim=True)
            except Exception:
                _geo_cache[key] = None
        return _geo_cache.get(key) or {"name": f"{lat:.4f}, {lon:.4f}", "type": "coordinate"}

    # Build trip objects
    trips = []
    for segment in trip_segments:
        first_ts = segment[0]["timestamp"]
        last_ts = segment[-1]["timestamp"]
        duration_hours = (last_ts - first_ts).total_seconds() / 3600.0

        # Skip very short trips (< 5 minutes)
        if duration_hours < 5 / 60:
            continue

        start_geo = _geocode(segment[0]["lat"], segment[0]["lon"])
        end_geo = _geocode(segment[-1]["lat"], segment[-1]["lon"])

        # Run full analysis for this segment
        analysis = analyze_device_track(
            tenant_id, device_id,
            after=first_ts - timedelta(seconds=1),
            before=last_ts + timedelta(seconds=1),
            dsn=dsn,
        )

        # Enrich each stop with reverse geocoding
        enriched_stops = []
        for stop in analysis.get("stops", []):
            stop_geo = _geocode(stop["lat"], stop["lon"])
            enriched_stops.append({
                **stop,
                "name": stop_geo.get("name", ""),
                "city": stop_geo.get("city"),
                "country": stop_geo.get("country"),
                "country_code": stop_geo.get("country_code"),
                "address": stop_geo.get("address"),
            })

        trip = {
            "trip_id": f"trip_{first_ts.strftime('%Y%m%d_%H%M')}_{device_id}",
            "device_id": device_id,
            "start_time": first_ts.isoformat(),
            "end_time": last_ts.isoformat(),
            "duration_hours": round(duration_hours, 2),
            "start_location": {
                "lat": segment[0]["lat"],
                "lon": segment[0]["lon"],
                "name": start_geo.get("name", ""),
                "city": start_geo.get("city"),
                "country": start_geo.get("country"),
                "country_code": start_geo.get("country_code"),
            },
            "end_location": {
                "lat": segment[-1]["lat"],
                "lon": segment[-1]["lon"],
                "name": end_geo.get("name", ""),
                "city": end_geo.get("city"),
                "country": end_geo.get("country"),
                "country_code": end_geo.get("country_code"),
            },
            "stops": enriched_stops,
            "stop_count": len(enriched_stops),
            "distance_km": analysis.get("stats", {}).get("total_distance_km", 0),
            "point_count": len(segment),
            "road_snapped": analysis.get("road_snapped", False),
            "route": analysis.get("route", []),
            "legs": analysis.get("legs", []),
            "stats": analysis.get("stats", {}),
        }
        trips.append(trip)

    # Newest first
    trips.sort(key=lambda t: t["start_time"], reverse=True)
    return trips


def _drop_teleports(points, max_speed_kmh=150):
    """Remove points that require impossible speed from the previous point."""
    if not points:
        return []
    result = [points[0]]
    for i in range(1, len(points)):
        prev = result[-1]
        dist = _haversine(prev["lat"], prev["lon"], points[i]["lat"], points[i]["lon"])
        dt = (points[i]["timestamp"] - prev["timestamp"]).total_seconds()
        speed = (dist / max(dt, 1)) * 3.6
        if speed <= max_speed_kmh:
            result.append(points[i])
    return result


def _cluster_consecutive(points, radius_m=200):
    """
    Group consecutive points into clusters.
    A new cluster starts when a point is > radius_m from the current cluster centroid.
    """
    if not points:
        return []

    clusters = []
    current = [points[0]]
    c_lat = points[0]["lat"]
    c_lon = points[0]["lon"]

    for i in range(1, len(points)):
        dist = _haversine(c_lat, c_lon, points[i]["lat"], points[i]["lon"])
        if dist <= radius_m:
            current.append(points[i])
            c_lat = sum(p["lat"] for p in current) / len(current)
            c_lon = sum(p["lon"] for p in current) / len(current)
        else:
            clusters.append(current)
            current = [points[i]]
            c_lat = points[i]["lat"]
            c_lon = points[i]["lon"]

    if current:
        clusters.append(current)

    return clusters


def _empty_result(device_id, after, before):
    return {
        "device_id": device_id, "road_snapped": False,
        "period": {"after": str(after or ""), "before": str(before or "")},
        "points": [], "route": [], "legs": [], "stops": [], "segments": [],
        "stats": {"total_points": 0, "clean_points": 0, "total_distance_km": 0,
                   "total_duration_hours": 0, "stop_count": 0,
                   "total_stop_duration_hours": 0, "moving_duration_hours": 0,
                   "road_snapped": False},
    }


def _get_positions(tenant_id, device_id, after, before, dsn):
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT latitude, longitude, timestamp,
                       payload->>'label' as label,
                       payload->>'accuracy' as accuracy
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                  AND latitude IS NOT NULL AND longitude IS NOT NULL
            """
            params = [tenant_id, device_id]
            if after:
                sql += " AND timestamp >= %s"
                params.append(after)
            if before:
                sql += " AND timestamp <= %s"
                params.append(before)
            sql += " ORDER BY timestamp ASC LIMIT 5000"
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [{"lat": float(r[0]), "lon": float(r[1]), "timestamp": r[2],
             "label": r[3] or "", "accuracy": float(r[4]) if r[4] else None} for r in rows]


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
