"""
Enrichment card display configuration.

Defines how each enrichment source's data renders in the UI.
Each source has a display name, icon, and field definitions
with labels, units, and formatting hints.

The frontend reads this to dynamically build the location info card
without hardcoding field names for each source.
"""

ENRICHMENT_CARD_CONFIG = {
    "elevation": {
        "display_name": "Elevation",
        "icon": "mountain",
        "fields": [
            {"key": "elevation_ft", "label": "Elevation", "unit": "ft", "format": "number"},
            {"key": "elevation_m", "label": "Elevation", "unit": "m", "format": "number"},
        ],
    },
    "elevation_global": {
        "display_name": "Elevation",
        "icon": "mountain",
        "fields": [
            {"key": "elevation_m", "label": "Elevation", "unit": "m", "format": "number"},
            {"key": "elevation_ft", "label": "Elevation", "unit": "ft", "format": "number"},
        ],
    },
    "address": {
        "display_name": "Address",
        "icon": "map-pin",
        "fields": [
            {"key": "display_name", "label": "Address", "format": "text"},
            {"key": "road", "label": "Road", "format": "text"},
            {"key": "city", "label": "City", "format": "text"},
            {"key": "state", "label": "State", "format": "text"},
            {"key": "postcode", "label": "Postal Code", "format": "text"},
            {"key": "country", "label": "Country", "format": "text"},
        ],
    },
    "census": {
        "display_name": "Census Data",
        "icon": "users",
        "fields": [
            {"key": "state_name", "label": "State", "format": "text"},
            {"key": "county_name", "label": "County", "format": "text"},
            {"key": "tract", "label": "Census Tract", "format": "mono"},
            {"key": "block", "label": "Block", "format": "mono"},
            {"key": "geoid", "label": "GEOID", "format": "mono"},
        ],
    },
    "fcc": {
        "display_name": "FCC Area",
        "icon": "radio",
        "fields": [
            {"key": "county_name", "label": "County", "format": "text"},
            {"key": "state_code", "label": "State", "format": "text"},
        ],
    },
    "weather": {
        "display_name": "NWS Weather",
        "icon": "cloud-sun",
        "fields": [
            {"key": "forecast_office", "label": "Forecast Office", "format": "text"},
            {"key": "timezone", "label": "Timezone", "format": "text"},
            {"key": "active_alerts", "label": "Active Alerts", "format": "number"},
            {"key": "alert_headlines", "label": "Alerts", "format": "list"},
        ],
    },
    "weather_global": {
        "display_name": "Current Weather",
        "icon": "thermometer",
        "priority": 1,
        "fields": [
            {"key": "conditions", "label": "Conditions", "format": "text"},
            {"key": "temperature_f", "label": "Temperature", "unit": "°F", "format": "number"},
            {"key": "temperature_c", "label": "Temperature", "unit": "°C", "format": "number"},
            {"key": "humidity_pct", "label": "Humidity", "unit": "%", "format": "number"},
            {"key": "wind_speed_kmh", "label": "Wind", "unit": "km/h", "format": "number"},
            {"key": "cloud_cover_pct", "label": "Cloud Cover", "unit": "%", "format": "number"},
            {"key": "pressure_hpa", "label": "Pressure", "unit": "hPa", "format": "number"},
            {"key": "timezone", "label": "Timezone", "format": "text"},
        ],
    },
    "air_quality": {
        "display_name": "Air Quality",
        "icon": "wind",
        "fields": [
            {"key": "us_aqi", "label": "AQI (US)", "format": "number"},
            {"key": "aqi_description", "label": "Air Quality", "format": "badge"},
            {"key": "pm2_5", "label": "PM2.5", "unit": "µg/m³", "format": "number"},
            {"key": "pm10", "label": "PM10", "unit": "µg/m³", "format": "number"},
            {"key": "ozone", "label": "Ozone", "unit": "µg/m³", "format": "number"},
            {"key": "nitrogen_dioxide", "label": "NO₂", "unit": "µg/m³", "format": "number"},
        ],
    },
    "marine": {
        "display_name": "Marine Conditions",
        "icon": "waves",
        "fields": [
            {"key": "wave_height_m", "label": "Wave Height", "unit": "m", "format": "number"},
            {"key": "wave_direction_deg", "label": "Wave Direction", "unit": "°", "format": "number"},
            {"key": "wave_period_s", "label": "Wave Period", "unit": "s", "format": "number"},
        ],
    },
    "flood_zone": {
        "display_name": "Flood Risk",
        "icon": "droplets",
        "fields": [
            {"key": "flood_zone", "label": "Zone", "format": "badge"},
            {"key": "flood_description", "label": "Risk Level", "format": "text"},
            {"key": "special_flood_hazard", "label": "Special Hazard Area", "format": "boolean"},
        ],
    },
    "country": {
        "display_name": "Country Info",
        "icon": "globe",
        "fields": [
            {"key": "country", "label": "Country", "format": "text"},
            {"key": "capital", "label": "Capital", "format": "text"},
            {"key": "population", "label": "Population", "format": "number_comma"},
            {"key": "area_km2", "label": "Area", "unit": "km²", "format": "number_comma"},
            {"key": "region", "label": "Region", "format": "text"},
            {"key": "subregion", "label": "Subregion", "format": "text"},
            {"key": "languages", "label": "Languages", "format": "list"},
            {"key": "timezones", "label": "Timezones", "format": "list"},
        ],
    },
    "wikidata": {
        "display_name": "Nearby Places",
        "icon": "landmark",
        "fields": [
            {"key": "nearby_entities", "label": "Entities", "format": "entity_list"},
        ],
    },
    "disasters": {
        "display_name": "Active Disasters",
        "icon": "alert-triangle",
        "fields": [
            {"key": "active_disasters", "label": "Disasters", "format": "disaster_list"},
        ],
    },
}


def get_card_config() -> dict:
    """Return the full card config for the frontend."""
    return ENRICHMENT_CARD_CONFIG
