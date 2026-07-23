"""
Pre-configured connector recipes — toggle-on data sources.

Each recipe is a pre-filled connector config. User provides minimal params
(location, optional API key) and the connector is created ready to poll.
"""

RECIPES = [
    # ─── Weather & Environment (no key needed) ──────────────────────────
    {
        "recipe_id": "noaa_weather",
        "name": "NOAA Weather",
        "description": "Current conditions and forecasts from the National Weather Service. US coverage.",
        "icon": "cloud-sun",
        "category": "weather",
        "requires_key": False,
        "user_params": [
            {"name": "latitude", "type": "float", "label": "Latitude", "required": True, "placeholder": "38.89"},
            {"name": "longitude", "type": "float", "label": "Longitude", "required": True, "placeholder": "-77.03"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "10 minutes",
            "source_config": {
                "url": "https://api.weather.gov/points/{latitude},{longitude}",
                "method": "GET",
                "headers": {"User-Agent": "(Enki Governance, admin@example.com)", "Accept": "application/geo+json"},
                "response_path": "properties",
            },
        },
        "signal_type_hint": "signal.environmental.weather",
        "card_preset": {
            "display_name": "Weather Observation",
            "icon": "cloud-sun",
        },
    },
    {
        "recipe_id": "nws_alerts",
        "name": "NWS Severe Weather Alerts",
        "description": "Tornado, flood, storm, and other severe weather alerts. US coverage.",
        "icon": "alert-triangle",
        "category": "weather",
        "requires_key": False,
        "user_params": [
            {"name": "state", "type": "string", "label": "State Code (e.g. DC, VA, TX)", "required": True, "placeholder": "DC"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "1 minute",
            "source_config": {
                "url": "https://api.weather.gov/alerts/active?area={state}",
                "method": "GET",
                "headers": {"User-Agent": "(Enki Governance, admin@example.com)", "Accept": "application/geo+json"},
                "response_path": "features",
                "id_field": "properties.id",
            },
        },
        "signal_type_hint": "signal.environmental.weather_alert",
        "card_preset": {
            "display_name": "Weather Alert",
            "icon": "alert-triangle",
        },
    },
    {
        "recipe_id": "usgs_earthquakes",
        "name": "USGS Earthquakes",
        "description": "Real-time seismic events worldwide. Includes magnitude, depth, and coordinates.",
        "icon": "activity",
        "category": "weather",
        "requires_key": False,
        "user_params": [
            {"name": "min_magnitude", "type": "float", "label": "Minimum Magnitude", "required": False, "default": "2.5", "placeholder": "2.5"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "2 minutes",
            "source_config": {
                "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_hour.geojson",
                "method": "GET",
                "headers": {},
                "response_path": "features",
                "id_field": "id",
            },
        },
        "signal_type_hint": "signal.environmental.seismic",
        "card_preset": {
            "display_name": "Earthquake",
            "icon": "activity",
        },
    },

    # ─── Tracking & Aviation (no key needed) ─────────────────────────────
    {
        "recipe_id": "adsb_aircraft",
        "name": "Aircraft Tracker (adsb.lol)",
        "description": "Real-time aircraft positions in your area. Open data from volunteer feeders.",
        "icon": "plane",
        "category": "tracking",
        "requires_key": False,
        "user_params": [
            {"name": "latitude", "type": "float", "label": "Center Latitude", "required": True, "placeholder": "38.89"},
            {"name": "longitude", "type": "float", "label": "Center Longitude", "required": True, "placeholder": "-77.03"},
            {"name": "radius_nm", "type": "int", "label": "Radius (nautical miles)", "required": False, "default": "50", "placeholder": "50"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "30 seconds",
            "source_config": {
                "url": "https://api.adsb.lol/v2/lat/{latitude}/lon/{longitude}/dist/{radius_nm}",
                "method": "GET",
                "headers": {},
                "response_path": "ac",
                "id_field": "hex",
            },
        },
        "signal_type_hint": "signal.geospatial.aircraft_position",
        "card_preset": {
            "display_name": "Aircraft Position",
            "icon": "plane",
        },
    },

    # ─── News & OSINT (no key needed) ────────────────────────────────────
    {
        "recipe_id": "gdelt_events",
        "name": "GDELT Global Events",
        "description": "World events from 65 languages, updated every 15 minutes. Filter by keyword.",
        "icon": "globe",
        "category": "news",
        "requires_key": False,
        "user_params": [
            {"name": "keywords", "type": "string", "label": "Keywords (e.g. cybersecurity, earthquake)", "required": True, "placeholder": "cybersecurity"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "url": "https://api.gdeltproject.org/api/v2/doc/doc?query={keywords}&mode=artlist&maxrecords=50&format=json&timespan=15min",
                "method": "GET",
                "headers": {},
                "response_path": "articles",
                "id_field": "url",
            },
        },
        "signal_type_hint": "signal.content.news",
        "card_preset": {
            "display_name": "News Event",
            "icon": "globe",
        },
    },
    {
        "recipe_id": "custom_rss",
        "name": "RSS Feed",
        "description": "Subscribe to any RSS or Atom feed. News, blogs, government alerts, anything.",
        "icon": "rss",
        "category": "news",
        "requires_key": False,
        "user_params": [
            {"name": "feed_url", "type": "string", "label": "Feed URL", "required": True, "placeholder": "https://example.com/feed.xml"},
            {"name": "poll_minutes", "type": "int", "label": "Poll every N minutes", "required": False, "default": "15", "placeholder": "15"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "{poll_minutes} minutes",
            "source_config": {
                "url": "{feed_url}",
            },
        },
        "signal_type_hint": "signal.content.rss_entry",
        "card_preset": {
            "display_name": "RSS Entry",
            "icon": "rss",
        },
    },

    # ─── Air Quality (free key) ──────────────────────────────────────────
    {
        "recipe_id": "openaq",
        "name": "OpenAQ Air Quality",
        "description": "Real-time air quality measurements from sensors worldwide. PM2.5, ozone, NO2, and more.",
        "icon": "wind",
        "category": "weather",
        "requires_key": True,
        "key_url": "https://explore.openaq.org/register",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (free at explore.openaq.org)", "required": True},
            {"name": "latitude", "type": "float", "label": "Latitude", "required": True, "placeholder": "38.89"},
            {"name": "longitude", "type": "float", "label": "Longitude", "required": True, "placeholder": "-77.03"},
            {"name": "radius_m", "type": "int", "label": "Radius (meters)", "required": False, "default": "25000", "placeholder": "25000"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "30 minutes",
            "source_config": {
                "url": "https://api.openaq.org/v3/locations?coordinates={latitude},{longitude}&radius={radius_m}&limit=20",
                "method": "GET",
                "headers": {"X-API-Key": "{api_key}"},
                "response_path": "results",
                "id_field": "id",
            },
        },
        "signal_type_hint": "signal.environmental.air_quality",
        "card_preset": {
            "display_name": "Air Quality",
            "icon": "wind",
        },
    },

    # ─── Security Enrichment (free key) ──────────────────────────────────
    {
        "recipe_id": "abuseipdb",
        "name": "AbuseIPDB Reputation",
        "description": "IP address reputation lookup. Enriches signals containing IP observables.",
        "icon": "shield",
        "category": "security",
        "requires_key": True,
        "key_url": "https://www.abuseipdb.com/register",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (free at abuseipdb.com)", "required": True},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "manual",
            "source_config": {
                "url": "https://api.abuseipdb.com/api/v2/blacklist?confidenceMinimum=90&limit=100",
                "method": "GET",
                "headers": {"Key": "{api_key}", "Accept": "application/json"},
                "response_path": "data",
                "id_field": "ipAddress",
            },
        },
        "signal_type_hint": "signal.security.threat_feed",
        "card_preset": {
            "display_name": "IP Reputation",
            "icon": "shield",
        },
    },

    # ─── IoT Sensors ─────────────────────────────────────────────────────
    {
        "recipe_id": "openweathermap",
        "name": "OpenWeatherMap",
        "description": "Global weather data. Temperature, humidity, wind, pressure. Requires free API key.",
        "icon": "thermometer",
        "category": "weather",
        "requires_key": True,
        "key_url": "https://openweathermap.org/appid",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (free at openweathermap.org)", "required": True},
            {"name": "latitude", "type": "float", "label": "Latitude", "required": True, "placeholder": "38.89"},
            {"name": "longitude", "type": "float", "label": "Longitude", "required": True, "placeholder": "-77.03"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "10 minutes",
            "source_config": {
                "url": "https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={api_key}&units=imperial",
                "method": "GET",
                "headers": {},
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.environmental.temperature",
        "card_preset": {
            "display_name": "Weather",
            "icon": "thermometer",
        },
    },

    # ─── Aviation Weather (aviationweather.gov — no key) ─────────────────
    {
        "recipe_id": "aviation_metar",
        "name": "Aviation METAR",
        "description": "Current airport weather observations (METAR). Temperature, wind, visibility, ceiling. No key required.",
        "icon": "plane",
        "category": "aviation",
        "requires_key": False,
        "user_params": [
            {"name": "station_ids", "type": "string", "label": "ICAO Station IDs (comma-separated, e.g. KJFK,KLAX)", "required": True, "placeholder": "KJFK,KLAX"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://aviationweather.gov/api/data/metar?ids={station_ids}&format=json&hours=1",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "metar_id",
            },
        },
        "signal_type_hint": "signal.environmental.aviation_weather",
        "card_preset": {
            "display_name": "METAR Observation",
            "icon": "plane",
        },
    },
    {
        "recipe_id": "aviation_taf",
        "name": "Aviation TAF",
        "description": "Terminal aerodrome forecasts. 24-30hr airport weather forecasts used by pilots.",
        "icon": "plane",
        "category": "aviation",
        "requires_key": False,
        "user_params": [
            {"name": "station_ids", "type": "string", "label": "ICAO Station IDs (comma-separated)", "required": True, "placeholder": "KJFK,KLAX"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "url": "https://aviationweather.gov/api/data/taf?ids={station_ids}&format=json",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "taf_id",
            },
        },
        "signal_type_hint": "signal.environmental.aviation_weather",
        "card_preset": {
            "display_name": "TAF Forecast",
            "icon": "plane",
        },
    },
    {
        "recipe_id": "aviation_pirep",
        "name": "Pilot Reports (PIREPs)",
        "description": "Turbulence, icing, and weather reports from pilots in flight. Filter by area.",
        "icon": "plane",
        "category": "aviation",
        "requires_key": False,
        "user_params": [
            {"name": "min_lat", "type": "float", "label": "South Latitude", "required": True, "placeholder": "25.0"},
            {"name": "min_lon", "type": "float", "label": "West Longitude", "required": True, "placeholder": "-130.0"},
            {"name": "max_lat", "type": "float", "label": "North Latitude", "required": True, "placeholder": "50.0"},
            {"name": "max_lon", "type": "float", "label": "East Longitude", "required": True, "placeholder": "-60.0"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://aviationweather.gov/api/data/pirep?bbox={min_lat},{min_lon},{max_lat},{max_lon}&format=json",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "pirepId",
            },
        },
        "signal_type_hint": "signal.environmental.aviation_weather",
        "card_preset": {
            "display_name": "Pilot Report",
            "icon": "plane",
        },
    },
    {
        "recipe_id": "aviation_sigmet",
        "name": "SIGMETs & AIRMETs",
        "description": "Significant meteorological advisories — severe turbulence, volcanic ash, thunderstorms, icing.",
        "icon": "alert-triangle",
        "category": "aviation",
        "requires_key": False,
        "user_params": [
            {"name": "hazard", "type": "string", "label": "Hazard filter (optional: conv, turb, ice, ifr, mt_obs, ash, ss)", "required": False, "default": "", "placeholder": "conv"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://aviationweather.gov/api/data/airsigmet?format=json&hazard={hazard}",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "airSigmetId",
            },
        },
        "signal_type_hint": "signal.environmental.weather_alert",
        "card_preset": {
            "display_name": "Aviation Advisory",
            "icon": "alert-triangle",
        },
    },
    {
        "recipe_id": "aviation_intl_sigmet",
        "name": "International SIGMETs",
        "description": "Worldwide significant weather advisories for international airspace.",
        "icon": "alert-triangle",
        "category": "aviation",
        "requires_key": False,
        "user_params": [
            {"name": "hazard", "type": "string", "label": "Hazard filter (optional: conv, turb, ice, ifr, ash, ss)", "required": False, "default": "", "placeholder": ""},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "10 minutes",
            "source_config": {
                "url": "https://aviationweather.gov/api/data/isigmet?format=json&hazard={hazard}",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "isigmetId",
            },
        },
        "signal_type_hint": "signal.environmental.weather_alert",
        "card_preset": {
            "display_name": "Intl SIGMET",
            "icon": "alert-triangle",
        },
    },

    # ─── Natural Events & Earth Science (no key) ────────────────────────
    {
        "recipe_id": "nasa_eonet",
        "name": "NASA Natural Events",
        "description": "Wildfires, volcanic eruptions, severe storms, floods tracked by NASA EONET.",
        "icon": "flame",
        "category": "weather",
        "requires_key": False,
        "user_params": [
            {"name": "days", "type": "int", "label": "Lookback (days)", "required": False, "default": "7", "placeholder": "7"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "30 minutes",
            "source_config": {
                "url": "https://eonet.gsfc.nasa.gov/api/v3/events?days={days}&status=open",
                "method": "GET",
                "headers": {},
                "response_path": "events",
                "id_field": "id",
            },
        },
        "signal_type_hint": "signal.environmental.weather_alert",
        "card_preset": {
            "display_name": "Natural Event",
            "icon": "flame",
        },
    },
    {
        "recipe_id": "noaa_tides",
        "name": "NOAA Tides & Currents",
        "description": "Tide predictions, water levels, and current data from NOAA coastal stations.",
        "icon": "waves",
        "category": "weather",
        "requires_key": False,
        "user_params": [
            {"name": "station_id", "type": "string", "label": "Station ID (find at tidesandcurrents.noaa.gov)", "required": True, "placeholder": "8518750"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "url": "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station={station_id}&product=water_level&datum=MLLW&units=english&time_zone=gmt&application=enki&format=json&date=latest",
                "method": "GET",
                "headers": {},
                "response_path": "data",
                "id_field": "t",
            },
        },
        "signal_type_hint": "signal.environmental.water_level",
        "card_preset": {
            "display_name": "Water Level",
            "icon": "waves",
        },
    },

    # ─── OSINT & News (no key) ──────────────────────────────────────────
    {
        "recipe_id": "hackernews_top",
        "name": "Hacker News",
        "description": "Top stories from Hacker News via Algolia search. Full titles, URLs, and scores.",
        "icon": "terminal",
        "category": "news",
        "requires_key": False,
        "user_params": [
            {"name": "hitsPerPage", "type": "int", "label": "Number of stories", "required": False, "default": "30", "placeholder": "30"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={hitsPerPage}",
                "method": "GET",
                "headers": {},
                "response_path": "hits",
                "id_field": "objectID",
            },
        },
        "signal_type_hint": "signal.content.news",
        "card_preset": {
            "display_name": "Hacker News",
            "icon": "terminal",
        },
    },
    {
        "recipe_id": "reddit_rss",
        "name": "Reddit Subreddit",
        "description": "Subscribe to any subreddit via RSS. OSINT, news, niche topics.",
        "icon": "rss",
        "category": "news",
        "requires_key": False,
        "user_params": [
            {"name": "subreddit", "type": "string", "label": "Subreddit name (without r/)", "required": True, "placeholder": "netsec"},
            {"name": "sort", "type": "string", "label": "Sort (hot, new, top)", "required": False, "default": "hot", "placeholder": "hot"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "10 minutes",
            "source_config": {
                "url": "https://www.reddit.com/r/{subreddit}/{sort}.rss",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Reddit Post",
            "icon": "rss",
        },
    },
    {
        "recipe_id": "github_releases",
        "name": "GitHub Repo Releases",
        "description": "Track releases and tags from any GitHub repository via RSS.",
        "icon": "rss",
        "category": "news",
        "requires_key": False,
        "user_params": [
            {"name": "owner", "type": "string", "label": "Owner", "required": True, "placeholder": "anthropics"},
            {"name": "repo", "type": "string", "label": "Repository", "required": True, "placeholder": "claude-code"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "30 minutes",
            "source_config": {
                "url": "https://github.com/{owner}/{repo}/releases.atom",
            },
        },
        "signal_type_hint": "signal.content.rss_entry",
        "card_preset": {
            "display_name": "GitHub Release",
            "icon": "rss",
        },
    },

    # ─── Maritime Tracking (no key) ─────────────────────────────────────
    {
        "recipe_id": "ais_vessel_tracker",
        "name": "AIS Vessel Tracker",
        "description": "Maritime vessel positions from AIS. Track ships in coastal areas via public AIS feeds.",
        "icon": "anchor",
        "category": "tracking",
        "requires_key": False,
        "user_params": [
            {"name": "latitude", "type": "float", "label": "Center Latitude", "required": True, "placeholder": "38.89"},
            {"name": "longitude", "type": "float", "label": "Center Longitude", "required": True, "placeholder": "-77.03"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "2 minutes",
            "source_config": {
                "url": "https://meri.digitraffic.fi/api/ais/v1/locations?latitude={latitude}&longitude={longitude}&radius=50&from=0",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "features",
                "id_field": "mmsi",
            },
        },
        "signal_type_hint": "signal.geospatial.vessel_position",
        "card_preset": {
            "display_name": "Vessel Position",
            "icon": "anchor",
        },
    },

    # ─── Security & Threat Intelligence (key required) ──────────────────
    {
        "recipe_id": "virustotal",
        "name": "VirusTotal",
        "description": "File and URL reputation lookups. Enriches signals with malware scan results.",
        "icon": "shield",
        "category": "security",
        "requires_key": True,
        "key_url": "https://www.virustotal.com/gui/join-us",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (free at virustotal.com)", "required": True},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "manual",
            "source_config": {
                "url": "https://www.virustotal.com/api/v3/popular_threat_categories",
                "method": "GET",
                "headers": {"x-apikey": "{api_key}", "Accept": "application/json"},
                "response_path": "data",
                "id_field": "",
            },
        },
        "signal_type_hint": "signal.security.threat_feed",
        "card_preset": {
            "display_name": "VirusTotal",
            "icon": "shield",
        },
    },
    {
        "recipe_id": "shodan_monitor",
        "name": "Shodan Internet Monitor",
        "description": "Monitor your public IPs for exposed services. Alerts on new open ports.",
        "icon": "shield",
        "category": "security",
        "requires_key": True,
        "key_url": "https://account.shodan.io/register",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (free at shodan.io)", "required": True},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "1 hour",
            "source_config": {
                "url": "https://api.shodan.io/shodan/alert/info?key={api_key}",
                "method": "GET",
                "headers": {"Accept": "application/json"},
                "response_path": "",
                "id_field": "id",
            },
        },
        "signal_type_hint": "signal.security.network_anomaly",
        "card_preset": {
            "display_name": "Shodan Alert",
            "icon": "shield",
        },
    },
    {
        "recipe_id": "haveibeenpwned",
        "name": "Have I Been Pwned",
        "description": "Check if accounts appear in data breaches. Monitors email domains.",
        "icon": "shield-alert",
        "category": "security",
        "requires_key": True,
        "key_url": "https://haveibeenpwned.com/API/Key",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "API Key (paid, $3.50/mo)", "required": True},
            {"name": "domain", "type": "string", "label": "Domain to monitor", "required": True, "placeholder": "example.com"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "6 hours",
            "source_config": {
                "url": "https://haveibeenpwned.com/api/v3/breaches?domain={domain}",
                "method": "GET",
                "headers": {"hibp-api-key": "{api_key}", "Accept": "application/json"},
                "response_path": "",
                "id_field": "Name",
            },
        },
        "signal_type_hint": "signal.security.threat_feed",
        "card_preset": {
            "display_name": "Breach Alert",
            "icon": "shield-alert",
        },
    },

    # ─── Financial ────────────────────────────────────────────────────────
    {
        "recipe_id": "coingecko",
        "name": "CoinGecko Crypto Prices",
        "description": "Track cryptocurrency prices. Bitcoin, Ethereum, and 10,000+ coins. Free, no API key needed.",
        "icon": "dollar-sign",
        "category": "financial",
        "requires_key": False,
        "user_params": [
            {"name": "coin_ids", "type": "string", "label": "Coin IDs (comma-separated)", "required": True, "placeholder": "bitcoin,ethereum,solana"},
            {"name": "currency", "type": "string", "label": "Currency", "required": False, "default": "usd", "placeholder": "usd"},
        ],
        "connector_config": {
            "source_type": "coingecko_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "coin_ids": "{coin_ids}",
                "vs_currency": "{currency}",
            },
        },
        "signal_type_hint": "signal.financial.balance_update",
        "card_preset": {"display_name": "Crypto Price", "icon": "dollar-sign"},
    },
    {
        "recipe_id": "plaid_transactions",
        "name": "Plaid Bank Transactions",
        "description": "Real-time transactions from 12,000+ financial institutions. Automatic categorization with merchant details.",
        "icon": "landmark",
        "category": "financial",
        "requires_key": True,
        "key_url": "https://plaid.com/",
        "user_params": [
            {"name": "client_id", "type": "string", "label": "Plaid Client ID", "required": True},
            {"name": "secret", "type": "string", "label": "Plaid Secret", "required": True},
            {"name": "access_token", "type": "string", "label": "Access Token (from Plaid Link)", "required": True},
            {"name": "environment", "type": "string", "label": "Environment", "required": False, "default": "production", "placeholder": "sandbox|development|production"},
            {"name": "account_ids", "type": "string", "label": "Account IDs (comma-separated, leave blank for all)", "required": False},
        ],
        "connector_config": {
            "source_type": "plaid_api",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "client_id": "$secret:plaid-client-id",
                "secret": "$secret:plaid-secret",
                "access_token": "{access_token}",
                "environment": "{environment}",
                "account_ids": "{account_ids}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {"display_name": "Bank Transaction", "icon": "landmark"},
    },
    {
        "recipe_id": "quickbooks_online",
        "name": "QuickBooks Online",
        "description": "Sync transactions and expenses from QuickBooks Online. Requires OAuth2 app registration.",
        "icon": "file-text",
        "category": "financial",
        "requires_key": True,
        "key_url": "https://developer.intuit.com/",
        "user_params": [
            {"name": "access_token", "type": "string", "label": "OAuth2 Access Token", "required": True},
            {"name": "refresh_token", "type": "string", "label": "Refresh Token", "required": True},
            {"name": "company_id", "type": "string", "label": "Company ID (Realm ID)", "required": True},
            {"name": "environment", "type": "string", "label": "Environment", "required": False, "default": "production", "placeholder": "sandbox|production"},
        ],
        "connector_config": {
            "source_type": "quickbooks_api",
            "connector_type": "pull",
            "poll_interval": "1 hour",
            "source_config": {
                "access_token": "$secret:qbo-access-token",
                "refresh_token": "$secret:qbo-refresh-token",
                "company_id": "{company_id}",
                "environment": "{environment}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {"display_name": "QuickBooks Transaction", "icon": "file-text"},
    },
    {
        "recipe_id": "stripe_payments",
        "name": "Stripe Payments",
        "description": "Payment events, charges, refunds, disputes, and payouts from Stripe.",
        "icon": "credit-card",
        "category": "financial",
        "requires_key": True,
        "key_url": "https://dashboard.stripe.com/apikeys",
        "user_params": [
            {"name": "api_key", "type": "string", "label": "Stripe Secret Key (sk_...)", "required": True},
            {"name": "event_types", "type": "string", "label": "Event Types (comma-separated)", "required": False, "default": "charge.succeeded,charge.refunded,payout.paid,dispute.created"},
        ],
        "connector_config": {
            "source_type": "stripe_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "api_key": "$secret:stripe-api-key",
                "event_types": "{event_types}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {"display_name": "Stripe Payment", "icon": "credit-card"},
    },
    {
        "recipe_id": "bank_statement_email",
        "name": "Bank Statements via Email",
        "description": "Monitor an email inbox for bank statement attachments (CSV, OFX, PDF). Auto-ingests when received.",
        "icon": "mail",
        "category": "financial",
        "requires_key": True,
        "key_url": "https://myaccount.google.com/apppasswords",
        "user_params": [
            {"name": "host", "type": "string", "label": "IMAP Host", "required": True, "default": "imap.gmail.com"},
            {"name": "username", "type": "string", "label": "Email Address", "required": True, "placeholder": "you@gmail.com"},
            {"name": "password", "type": "string", "label": "App Password", "required": True},
            {"name": "folder", "type": "string", "label": "Folder to Monitor", "required": False, "default": "INBOX"},
            {"name": "subject_filter", "type": "string", "label": "Subject Filter", "required": False, "placeholder": "statement,bank,transaction"},
        ],
        "connector_config": {
            "source_type": "imap",
            "connector_type": "pull",
            "poll_interval": "1 hour",
            "source_config": {
                "host": "{host}",
                "port": 993,
                "username": "{username}",
                "password": "$secret:bank-email-password",
                "use_ssl": True,
                "folder": "{folder}",
                "search_criteria": "UNSEEN",
                "attachment_types": ["csv", "ofx", "qbo", "qfx", "pdf"],
                "subject_filter": "{subject_filter}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {"display_name": "Bank Statement", "icon": "mail"},
    },

    # ─── Email (IMAP connector) ─────────────────────────────────────────
    {
        "recipe_id": "gmail_imap",
        "name": "Gmail",
        "description": "Pull emails from Gmail via IMAP. Requires app-specific password.",
        "icon": "mail",
        "category": "email",
        "requires_key": True,
        "key_url": "https://myaccount.google.com/apppasswords",
        "user_params": [
            {"name": "username", "type": "string", "label": "Gmail address", "required": True, "placeholder": "you@gmail.com"},
            {"name": "password", "type": "string", "label": "App password (not your Gmail password)", "required": True},
            {"name": "folder", "type": "string", "label": "Folder", "required": False, "default": "INBOX", "placeholder": "INBOX"},
        ],
        "connector_config": {
            "source_type": "imap",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "host": "imap.gmail.com",
                "port": 993,
                "username": "{username}",
                "password": "{password}",
                "use_ssl": True,
                "folder": "{folder}",
                "search_criteria": "UNSEEN",
                "max_messages": 50,
            },
        },
        "signal_type_hint": "signal.communications.email",
        "card_preset": {
            "display_name": "Email",
            "icon": "mail",
        },
    },
    {
        "recipe_id": "outlook_imap",
        "name": "Outlook / Office 365",
        "description": "Pull emails from Outlook or Office 365 via IMAP.",
        "icon": "mail",
        "category": "email",
        "requires_key": True,
        "key_url": "https://support.microsoft.com/en-us/account-billing/using-app-passwords-with-apps-that-don-t-support-two-step-verification-5896ed9b-4263-e681-128a-a6f2979a7944",
        "user_params": [
            {"name": "username", "type": "string", "label": "Email address", "required": True, "placeholder": "you@outlook.com"},
            {"name": "password", "type": "string", "label": "App password", "required": True},
            {"name": "folder", "type": "string", "label": "Folder", "required": False, "default": "INBOX", "placeholder": "INBOX"},
        ],
        "connector_config": {
            "source_type": "imap",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "host": "outlook.office365.com",
                "port": 993,
                "username": "{username}",
                "password": "{password}",
                "use_ssl": True,
                "folder": "{folder}",
                "search_criteria": "UNSEEN",
                "max_messages": 50,
            },
        },
        "signal_type_hint": "signal.communications.email",
        "card_preset": {
            "display_name": "Email",
            "icon": "mail",
        },
    },

    # ─── MQTT (IoT) ─────────────────────────────────────────────────────
    {
        "recipe_id": "mqtt_home_assistant",
        "name": "Home Assistant MQTT",
        "description": "Subscribe to Home Assistant MQTT topics. Sensors, switches, lights, climate.",
        "icon": "activity",
        "category": "iot",
        "requires_key": False,
        "user_params": [
            {"name": "broker", "type": "string", "label": "MQTT Broker Host", "required": True, "placeholder": "192.168.1.100"},
            {"name": "port", "type": "int", "label": "Port", "required": False, "default": "1883", "placeholder": "1883"},
            {"name": "username", "type": "string", "label": "Username (if required)", "required": False, "default": ""},
            {"name": "password", "type": "string", "label": "Password (if required)", "required": False, "default": ""},
        ],
        "connector_config": {
            "source_type": "mqtt",
            "connector_type": "pull",
            "poll_interval": "30 seconds",
            "source_config": {
                "broker": "{broker}",
                "port": "{port}",
                "topics": ["homeassistant/#"],
                "username": "{username}",
                "password": "{password}",
                "use_tls": False,
                "qos": 1,
                "max_messages": 100,
                "timeout": 10,
            },
        },
        "signal_type_hint": "signal.infrastructure.sensor_reading",
        "card_preset": {
            "display_name": "IoT Sensor",
            "icon": "activity",
        },
    },
    {
        "recipe_id": "mqtt_generic",
        "name": "MQTT Broker",
        "description": "Connect to any MQTT broker. Subscribe to topics for IoT data, telemetry, events.",
        "icon": "activity",
        "category": "iot",
        "requires_key": False,
        "user_params": [
            {"name": "broker", "type": "string", "label": "Broker Host", "required": True, "placeholder": "mqtt.example.com"},
            {"name": "port", "type": "int", "label": "Port", "required": False, "default": "1883", "placeholder": "1883"},
            {"name": "topics", "type": "string", "label": "Topics (comma-separated, supports wildcards)", "required": True, "placeholder": "sensors/#,gps/+/position"},
            {"name": "username", "type": "string", "label": "Username", "required": False, "default": ""},
            {"name": "password", "type": "string", "label": "Password", "required": False, "default": ""},
        ],
        "connector_config": {
            "source_type": "mqtt",
            "connector_type": "pull",
            "poll_interval": "30 seconds",
            "source_config": {
                "broker": "{broker}",
                "port": "{port}",
                "topics": ["{topics}"],
                "username": "{username}",
                "password": "{password}",
                "use_tls": False,
                "qos": 1,
                "max_messages": 100,
                "timeout": 10,
            },
        },
        "signal_type_hint": "signal.infrastructure.sensor_reading",
        "card_preset": {
            "display_name": "MQTT Message",
            "icon": "activity",
        },
    },

    # ─── Database Connectors ────────────────────────────────────────────
    {
        "recipe_id": "postgres_query",
        "name": "PostgreSQL Query",
        "description": "Poll a PostgreSQL database on a schedule. Read-only. Great for logs, transactions, events.",
        "icon": "database",
        "category": "database",
        "requires_key": True,
        "user_params": [
            {"name": "host", "type": "string", "label": "Host", "required": True, "placeholder": "db.example.com"},
            {"name": "port", "type": "int", "label": "Port", "required": False, "default": "5432", "placeholder": "5432"},
            {"name": "database", "type": "string", "label": "Database", "required": True, "placeholder": "mydb"},
            {"name": "username", "type": "string", "label": "Username", "required": True, "placeholder": "readonly_user"},
            {"name": "password", "type": "string", "label": "Password", "required": True},
            {"name": "query", "type": "string", "label": "SQL Query (use :watermark for cursor)", "required": True, "placeholder": "SELECT * FROM events WHERE id > :watermark ORDER BY id LIMIT 500"},
            {"name": "watermark_field", "type": "string", "label": "Watermark field", "required": False, "default": "id", "placeholder": "id"},
        ],
        "connector_config": {
            "source_type": "database_query",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "db_type": "postgresql",
                "host": "{host}",
                "port": "{port}",
                "database": "{database}",
                "username": "{username}",
                "password": "{password}",
                "query": "{query}",
                "watermark_field": "{watermark_field}",
            },
        },
        "signal_type_hint": "",
        "card_preset": {
            "display_name": "Database Record",
            "icon": "database",
        },
    },
    {
        "recipe_id": "mysql_query",
        "name": "MySQL Query",
        "description": "Poll a MySQL database on a schedule. Read-only.",
        "icon": "database",
        "category": "database",
        "requires_key": True,
        "user_params": [
            {"name": "host", "type": "string", "label": "Host", "required": True, "placeholder": "db.example.com"},
            {"name": "port", "type": "int", "label": "Port", "required": False, "default": "3306", "placeholder": "3306"},
            {"name": "database", "type": "string", "label": "Database", "required": True, "placeholder": "mydb"},
            {"name": "username", "type": "string", "label": "Username", "required": True, "placeholder": "readonly_user"},
            {"name": "password", "type": "string", "label": "Password", "required": True},
            {"name": "query", "type": "string", "label": "SQL Query (use :watermark for cursor)", "required": True, "placeholder": "SELECT * FROM logs WHERE id > :watermark ORDER BY id LIMIT 500"},
            {"name": "watermark_field", "type": "string", "label": "Watermark field", "required": False, "default": "id", "placeholder": "id"},
        ],
        "connector_config": {
            "source_type": "database_query",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "db_type": "mysql",
                "host": "{host}",
                "port": "{port}",
                "database": "{database}",
                "username": "{username}",
                "password": "{password}",
                "query": "{query}",
                "watermark_field": "{watermark_field}",
            },
        },
        "signal_type_hint": "",
        "card_preset": {
            "display_name": "Database Record",
            "icon": "database",
        },
    },

    # ─── GPS Tracking ───────────────────────────────────────────────────
    {
        "recipe_id": "traccar_server",
        "name": "Traccar GPS Server",
        "description": "Pull vehicle and asset positions from a Traccar GPS tracking server.",
        "icon": "map-pin",
        "category": "tracking",
        "requires_key": True,
        "user_params": [
            {"name": "api_url", "type": "string", "label": "Traccar API URL", "required": True, "placeholder": "http://localhost:8082/api"},
            {"name": "username", "type": "string", "label": "Username", "required": True, "placeholder": "admin"},
            {"name": "password", "type": "string", "label": "Password", "required": True},
        ],
        "connector_config": {
            "source_type": "traccar_pull",
            "connector_type": "pull",
            "poll_interval": "30 seconds",
            "source_config": {
                "api_url": "{api_url}",
                "username": "{username}",
                "password": "{password}",
            },
        },
        "signal_type_hint": "signal.geospatial.position_report",
        "card_preset": {
            "display_name": "GPS Position",
            "icon": "map-pin",
        },
    },
    # ─── Social Media & OSINT ─────────────────────────────────────────
    {
        "recipe_id": "twitter_list_rss",
        "name": "Twitter/X List (via Nitter)",
        "description": "Monitor a Twitter/X list or user timeline via public RSS proxy. No API key needed.",
        "icon": "twitter",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "username", "type": "string", "label": "Twitter Username", "required": True, "placeholder": "username"},
            {"name": "nitter_instance", "type": "string", "label": "Nitter Instance", "required": False, "default": "nitter.net", "placeholder": "nitter.net"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://{nitter_instance}/{username}/rss",
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Tweet",
            "icon": "twitter",
        },
    },
    {
        "recipe_id": "reddit_subreddit",
        "name": "Reddit Subreddit",
        "description": "Monitor new posts in any subreddit. No API key needed — uses public JSON feed.",
        "icon": "message-square",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "subreddit", "type": "string", "label": "Subreddit Name", "required": True, "placeholder": "worldnews"},
            {"name": "sort", "type": "string", "label": "Sort By", "required": False, "default": "new", "placeholder": "new, hot, rising"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://www.reddit.com/r/{subreddit}/{sort}.json?limit=25",
                "method": "GET",
                "headers": {"User-Agent": "Enki Governance/1.0"},
                "response_path": "data.children",
                "id_field": "data.id",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Reddit Post",
            "icon": "message-square",
        },
    },
    {
        "recipe_id": "telegram_channel",
        "name": "Telegram Channel",
        "description": "Monitor a public Telegram channel via RSSTelegram proxy. No API key needed for public channels.",
        "icon": "send",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "channel", "type": "string", "label": "Channel Username", "required": True, "placeholder": "duaborern"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://rsshub.app/telegram/channel/{channel}",
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Telegram Post",
            "icon": "send",
        },
    },
    {
        "recipe_id": "youtube_channel",
        "name": "YouTube Channel",
        "description": "Monitor new video uploads from a YouTube channel. No API key needed — uses public RSS feed.",
        "icon": "youtube",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "channel_id", "type": "string", "label": "YouTube Channel ID", "required": True, "placeholder": "UCxxxxxxxxxxxxxxxxxxxxxx"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "15 minutes",
            "source_config": {
                "url": "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.content.video",
        "card_preset": {
            "display_name": "YouTube Video",
            "icon": "youtube",
        },
    },
    {
        "recipe_id": "mastodon_user",
        "name": "Mastodon User Feed",
        "description": "Monitor a Mastodon user's public posts via Atom feed. No API key needed.",
        "icon": "at-sign",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "instance", "type": "string", "label": "Instance Domain", "required": True, "placeholder": "mastodon.social"},
            {"name": "username", "type": "string", "label": "Username", "required": True, "placeholder": "Gargron"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://{instance}/@{username}.rss",
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Mastodon Post",
            "icon": "at-sign",
        },
    },
    {
        "recipe_id": "bluesky_user",
        "name": "Bluesky User Feed",
        "description": "Monitor a Bluesky user's public posts via AT Protocol API. No API key needed.",
        "icon": "cloud",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "handle", "type": "string", "label": "Bluesky Handle", "required": True, "placeholder": "user.bsky.social"},
        ],
        "connector_config": {
            "source_type": "http_api",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={handle}&limit=25",
                "method": "GET",
                "headers": {},
                "response_path": "feed",
                "id_field": "post.uri",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "Bluesky Post",
            "icon": "cloud",
        },
    },
    {
        "recipe_id": "rss_generic_social",
        "name": "Custom Social RSS",
        "description": "Monitor any social feed available via RSS/Atom. Works with blogs, forums, news aggregators.",
        "icon": "rss",
        "category": "social",
        "requires_key": False,
        "user_params": [
            {"name": "feed_url", "type": "string", "label": "RSS Feed URL", "required": True, "placeholder": "https://example.com/feed.xml"},
            {"name": "label", "type": "string", "label": "Source Label", "required": False, "default": "Social Feed", "placeholder": "My Feed"},
        ],
        "connector_config": {
            "source_type": "rss_feed",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "url": "{feed_url}",
                "response_path": "",
            },
        },
        "signal_type_hint": "signal.content.social_post",
        "card_preset": {
            "display_name": "{label}",
            "icon": "rss",
        },
    },

    # ─── Financial / Bookkeeping ────────────────────────────────────────
    {
        "recipe_id": "bank_statement_upload",
        "name": "Bank Statement Upload",
        "description": "Upload bank statements (CSV, OFX/QBO, PDF). Each transaction becomes a financial signal with AI categorization and cross-domain correlation.",
        "icon": "landmark",
        "category": "financial",
        "requires_key": False,
        "user_params": [
            {"name": "account_label", "type": "string", "label": "Account Label", "required": True, "placeholder": "Chase Checking"},
        ],
        "connector_config": {
            "source_type": "file_upload",
            "connector_type": "push",
            "source_config": {
                "accepted_formats": ["csv", "ofx", "qbo", "qfx", "pdf"],
                "parser_hint": "bank_statement",
                "label": "{account_label}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {
            "display_name": "Bank Transaction",
            "icon": "dollar-sign",
        },
    },
    {
        "recipe_id": "bank_statement_watch",
        "name": "Bank Statement Watch Folder",
        "description": "Monitor a folder for new bank statement files. Drop CSV, OFX, or PDF statements and they auto-ingest.",
        "icon": "folder-open",
        "category": "financial",
        "requires_key": False,
        "user_params": [
            {"name": "watch_path", "type": "string", "label": "Folder Path", "required": True, "placeholder": "/data/bank-statements"},
            {"name": "account_label", "type": "string", "label": "Account Label", "required": False, "default": "Bank Account", "placeholder": "Chase Checking"},
        ],
        "connector_config": {
            "source_type": "file_watch",
            "connector_type": "watch",
            "source_config": {
                "path": "{watch_path}",
                "patterns": ["*.csv", "*.ofx", "*.qbo", "*.qfx", "*.pdf"],
                "parser_hint": "bank_statement",
                "label": "{account_label}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {
            "display_name": "Bank Transaction",
            "icon": "dollar-sign",
        },
    },
    {
        "recipe_id": "bank_statement_email",
        "name": "Bank Statement Email Forward",
        "description": "Forward bank statement emails to Enki. Attachments (CSV, OFX, PDF) are auto-parsed into financial signals.",
        "icon": "mail",
        "category": "financial",
        "requires_key": False,
        "user_params": [
            {"name": "imap_server", "type": "string", "label": "IMAP Server", "required": True, "placeholder": "imap.gmail.com"},
            {"name": "email", "type": "string", "label": "Email Address", "required": True, "placeholder": "you@example.com"},
            {"name": "password", "type": "string", "label": "App Password", "required": True, "placeholder": "xxxx xxxx xxxx xxxx"},
            {"name": "folder", "type": "string", "label": "Mail Folder", "required": False, "default": "INBOX", "placeholder": "INBOX"},
        ],
        "connector_config": {
            "source_type": "imap",
            "connector_type": "pull",
            "poll_interval": "5 minutes",
            "source_config": {
                "server": "{imap_server}",
                "username": "{email}",
                "password": "{password}",
                "folder": "{folder}",
                "attachment_filter": ["*.csv", "*.ofx", "*.qbo", "*.qfx", "*.pdf"],
                "parser_hint": "bank_statement",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {
            "display_name": "Bank Transaction",
            "icon": "dollar-sign",
        },
    },
    {
        "recipe_id": "quickbooks_csv_import",
        "name": "QuickBooks Export Import",
        "description": "Import QuickBooks Desktop or Online CSV/IIF exports. Maps QB fields to financial signals automatically.",
        "icon": "file-spreadsheet",
        "category": "financial",
        "requires_key": False,
        "user_params": [
            {"name": "account_label", "type": "string", "label": "Account Label", "required": False, "default": "QuickBooks", "placeholder": "QuickBooks Business"},
        ],
        "connector_config": {
            "source_type": "file_upload",
            "connector_type": "push",
            "source_config": {
                "accepted_formats": ["csv", "iif", "qbo"],
                "parser_hint": "bank_statement",
                "label": "{account_label}",
            },
        },
        "signal_type_hint": "signal.financial.transaction",
        "card_preset": {
            "display_name": "QuickBooks Transaction",
            "icon": "receipt",
        },
    },
]


def get_all_recipes() -> list:
    """Return all available recipes."""
    return RECIPES


def get_recipe(recipe_id: str) -> dict | None:
    """Get a single recipe by ID."""
    for r in RECIPES:
        if r["recipe_id"] == recipe_id:
            return r
    return None


def instantiate_recipe(recipe_id: str, user_params: dict) -> dict | None:
    """Fill in a recipe's template with user-provided params. Returns connector config.

    URL parameters are automatically URL-encoded to prevent broken URLs
    (e.g., "ukraine conflict" → "ukraine%20conflict").
    """
    from urllib.parse import quote

    recipe = get_recipe(recipe_id)
    if not recipe:
        return None

    config = recipe["connector_config"].copy()
    source_config = config.get("source_config", {}).copy()

    # URL-encode params that will be interpolated into URLs
    url_safe_params = {}
    for k, v in user_params.items():
        s = str(v)
        # URL-encode if the value contains characters that break URLs
        # but preserve full URLs (feed_url, api_url) as-is
        if k in ("feed_url", "api_url", "url", "imap_server", "broker", "host"):
            url_safe_params[k] = s
        else:
            url_safe_params[k] = quote(s, safe="")
    # Keep original params for non-URL fields (poll_interval, labels, etc.)
    raw_params = {k: str(v) for k, v in user_params.items()}

    # Interpolate URL-safe params into source_config (contains URLs)
    def _interpolate(val, params):
        if isinstance(val, str):
            for k, v in params.items():
                val = val.replace(f"{{{k}}}", v)
            return val
        if isinstance(val, dict):
            return {k: _interpolate(v, params) for k, v in val.items()}
        if isinstance(val, list):
            return [_interpolate(v, params) for v in val]
        return val

    config["source_config"] = _interpolate(source_config, url_safe_params)
    # Use raw params for non-URL fields like poll_interval
    config["poll_interval"] = _interpolate(config.get("poll_interval", "5 minutes"), raw_params)

    return {
        "recipe_id": recipe_id,
        "name": recipe["name"],
        "icon": recipe["icon"],
        "signal_type_hint": recipe.get("signal_type_hint"),
        "card_preset": recipe.get("card_preset"),
        "connector_config": config,
    }
