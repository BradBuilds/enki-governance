"""
OSRM Map Matching — snaps GPS traces to road network.

Takes noisy GPS points and returns road-snapped coordinates
with per-leg timing, speed, and street names.
"""

import json as _json
import os
import urllib.request
import urllib.parse
from typing import Optional


OSRM_URL = os.environ.get("OSRM_URL", "http://localhost:5001")


def match_to_roads(
    points: list[dict],
    max_gap_seconds: float = 120,
) -> Optional[dict]:
    """
    Snap a GPS trace to roads using OSRM Map Matching.

    Args:
        points: List of {"lat": float, "lon": float, "timestamp": str, ...}
                Must be time-ordered.
        max_gap_seconds: Split into sub-traces if gap exceeds this.

    Returns:
        {
            "matched_route": [[lon, lat], ...],  # GeoJSON-ready road-snapped polyline
            "confidence": float,                  # 0-1 match confidence
            "legs": [                             # per-segment data
                {
                    "distance_m": float,
                    "duration_s": float,
                    "speed_kmh": float,
                    "street": str,
                    "geometry": [[lon, lat], ...],
                }
            ],
            "total_distance_m": float,
            "total_duration_s": float,
        }
    """
    if len(points) < 2:
        return None

    # Build OSRM match request
    # Format: /match/v1/driving/lon,lat;lon,lat;...
    coords_str = ";".join(f"{p['lon']},{p['lat']}" for p in points)

    # Timestamps help OSRM resolve ambiguity at intersections
    timestamps = []
    for p in points:
        ts = p.get("timestamp")
        if hasattr(ts, "timestamp"):
            timestamps.append(str(int(ts.timestamp())))
        elif isinstance(ts, str):
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                timestamps.append(str(int(dt.timestamp())))
            except (ValueError, TypeError):
                timestamps.append("")
        else:
            timestamps.append("")

    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "annotations": "true",
        "gaps": "ignore",
    }

    # Add timestamps if available
    if all(timestamps):
        params["timestamps"] = ";".join(timestamps)

    # Add radiuses (GPS accuracy) — generous to handle WiFi-based locations
    radiuses = []
    for p in points:
        acc = p.get("accuracy")
        if acc and float(acc) > 0:
            radiuses.append(str(min(int(float(acc)), 100)))
        else:
            radiuses.append("50")
    params["radiuses"] = ";".join(radiuses)

    try:
        qs = urllib.parse.urlencode(params)
        url = f"{OSRM_URL}/match/v1/driving/{coords_str}?{qs}"

        # If few waypoints (cleaned clusters), use route API instead of match
        # Route gives exact driving directions; match tries to fit a GPS trace
        if len(points) <= 25:
            route_params = {
                "overview": "full",
                "geometries": "geojson",
                "steps": "true",
                "annotations": "true",
            }
            route_qs = urllib.parse.urlencode(route_params)
            url = f"{OSRM_URL}/route/v1/driving/{coords_str}?{route_qs}"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            data = _json.loads(resp.read())

        # Handle both route and match responses
        if data.get("code") != "Ok":
            return None

        # Route API returns "routes", match API returns "matchings"
        source_list = data.get("routes") or data.get("matchings")
        if not source_list:
            return None

        # Combine all routes/matchings into one route
        all_coords = []
        all_legs = []
        total_dist = 0
        total_dur = 0
        confidence_sum = 0

        for matching in source_list:
            confidence_sum += matching.get("confidence", 0)

            # Full matched geometry
            geom = matching.get("geometry", {})
            coords = geom.get("coordinates", [])
            all_coords.extend(coords)

            # Per-leg data (between waypoints)
            for leg in matching.get("legs", []):
                leg_dist = leg.get("distance", 0)
                leg_dur = leg.get("duration", 0)
                speed = (leg_dist / leg_dur * 3.6) if leg_dur > 0 else 0

                # Get street name from steps
                street = ""
                leg_geom = []
                for step in leg.get("steps", []):
                    if step.get("name") and not street:
                        street = step["name"]
                    step_coords = step.get("geometry", {}).get("coordinates", [])
                    leg_geom.extend(step_coords)

                all_legs.append({
                    "distance_m": round(leg_dist, 1),
                    "duration_s": round(leg_dur, 1),
                    "speed_kmh": round(speed, 1),
                    "street": street,
                    "geometry": leg_geom,
                })
                total_dist += leg_dist
                total_dur += leg_dur

        avg_confidence = confidence_sum / len(source_list) if source_list else 0

        return {
            "matched_route": all_coords,
            "confidence": round(avg_confidence, 3),
            "legs": all_legs,
            "total_distance_m": round(total_dist, 1),
            "total_duration_s": round(total_dur, 1),
        }

    except (urllib.error.URLError, OSError, _json.JSONDecodeError):
        return None
