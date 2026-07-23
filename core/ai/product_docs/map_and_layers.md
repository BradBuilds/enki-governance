# The map and its layers

The Map is a 3D globe with toggleable layers, grouped in the Layers panel:

- **Intelligence** — your signals, tracks, geofences, knowledge entities,
  annotations, and geotagged news (GDELT).
- **Tracking** — live aircraft (ADS-B) and vessels (AIS).
- **Infrastructure** — ports, airports, traffic/OSM cameras, power plants, cell
  towers.
- **Space & Astronomy** — satellites (from CelesTrak TLEs, propagated live),
  fireballs, space weather.
- **Environmental** — earthquakes, weather stations.
- **Celestial & Weather** — day/night terminator, GOES infrared/visible, NEXRAD
  radar, precipitation.
- **Sentinel-2 Imagery (ESA)** — True/False color, NDVI, SWIR, moisture. High
  resolution: it only renders when you zoom into a region, not at the globe view.
- **Reference** — countries, capitals, rivers.

Each live layer shows a status in the menu: a count when loaded, a "0" when it
loaded but nothing is in view, or a warning if it failed. When a Sentinel or
weather overlay is on, a legend (bottom-left) explains what the colors mean.

# Satellites

Satellite positions are computed in your browser from Two-Line Element sets
(CelesTrak) using the standard SGP4 model, so they show true current positions.
At the whole-globe view you see the navigation and geostationary rings; zoom in
to see more (including Starlink).

# Layers that need setup

Only **Sentinel-2 imagery** needs a key (a free Sentinel Hub instance ID, set in
Settings → Satellite Imagery). Everything else uses free public sources.
