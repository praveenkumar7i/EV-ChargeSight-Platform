# Map setup: Google Maps, public listings and their limits

The Map page shows **real, publicly listed EV charging stations in Bengaluru** (PUBLIC LISTING), grouped into six focus areas
(Electronic City, Hebbal, HSR Layout, Koramangala, Whitefield, Yelahanka) plus "Other Bengaluru". The SIMULATED demo sites and
the ESTIMATED proposed investment sites are separate, optional layers.

> A station on the map (or on Google Maps) does **not** mean it is working, available or connected to ChargeSight. Every public
> listing shows **STATUS UNKNOWN**. Live status would need an authorised operator API or OCPP feed (see INTEGRATION.md).

## 1. Basemap engines (automatic fallback)
| Situation | Basemap |
|---|---|
| `GOOGLE_MAPS_BROWSER_KEY` set and accepted | Google Maps: roadmap / satellite / hybrid / terrain, zoom, fullscreen, rotate and tilt (vector map with `GOOGLE_MAPS_MAP_ID`, or 45° imagery), optional photorealistic 3D |
| No key, key rejected, or script blocked | Leaflet + OpenStreetMap tiles (`tile.openstreetmap.org`, light academic use, attribution shown) |
| Offline (no CDN) | Coordinate plot; search, filters, list and details still work |

### Google Cloud steps (browser key)
1. Google Cloud Console → project → **Billing** (required even within free usage).
2. **APIs & Services → Library** → enable **Maps JavaScript API**.
3. **Credentials → Create credentials → API key** → *Application restrictions*: **Websites** → add `http://127.0.0.1:8000/*` and `http://localhost:8000/*`; *API restrictions*: **Maps JavaScript API** only.
4. Optional, for tilt/rotation of the **2D** map: **Map management → Create Map ID** (JavaScript, **Vector**) → `GOOGLE_MAPS_MAP_ID` in `.env`. Without it the 2D map stays flat (the page says so); the 3D view still tilts and rotates.
5. **Photorealistic 3D** (*3D view* button): uses `Map3DElement` from the `maps3d` library, which Google ships in the regular **weekly** channel since 23 Oct 2025 (release 3.62.11), so no channel switch is needed. It needs only the Maps JavaScript API on the same key (no Map ID), WebGL with hardware acceleration enabled in the browser, and Google 3D coverage for the area; where there is no 3D imagery the view stays flat satellite. Controls: drag to pan, right-drag/Ctrl-drag or the Tilt/⟲/⟳/North-up buttons for the camera, Reset camera to return.
6. Optional **address search** in the Map search box: enable the **Geocoding API** for the browser key. If it is not enabled the page reports “Geocoding API is not enabled for this browser key”; station, locality and “lat, lng” search still work.
7. **Street View** (button) uses the Maps JavaScript API's Street View service; it checks for imagery within 50 m first and says so when there is none.

Not tested here against Google: the development environment has no access to Google, so the Google-specific controls (map types, 3D, tilt/rotate, Street View, geocoding) were written against Google's current documentation and must be checked on your computer (see the README checklist). The rest of the Map works the same on the OpenStreetMap fallback and is covered by browser tests.

## 2. Public-listing sources
| Source | Key | Stored? | Licence / attribution | Notes |
|---|---|---|---|---|
| **Open Charge Map snapshot** (bundled, `sample_data/PUBLIC_ocm_bengaluru_snapshot.json`) | none | yes | CC BY 4.0, Open Charge Map contributors | Each record was copied from its OCM page on 23 Oct 2026; loaded at start, idempotent |
| **Open Charge Map API** (Refresh listings) | `OCM_API_KEY` (free, server-side) | yes | per record (usually CC BY 4.0) | Bengaluru bounding box, up to 1000 records |
| **OpenStreetMap / Overpass** (Refresh listings) | none (`OSM_ENABLED=true`) | yes | ODbL 1.0, © OpenStreetMap contributors | One polite request per refresh, back-off on 429/504, never polled in the background |
| **Google Places API (New)** (Google Places lookup) | `GOOGLE_PLACES_API_KEY` (server-side) | **no** | Google Maps Platform terms; "Google Maps" attribution | Google forbids caching Places content (place IDs excepted), so results are shown live and discarded. Billed: `evChargeOptions` triggers the Text Search Enterprise + Atmosphere SKU; up to 3 pages × 20 results per area |

Duplicates: the same provider id updates in place; a record from another provider within 60 m with an overlapping name (or 200 m with a near-identical name) is merged under "also listed by". Invalid or out-of-Bengaluru coordinates are rejected. Missing fields are shown as "unknown (not provided by the source)", never guessed.

## 3. Server-side Places key (separate from the browser key)
Create a second key restricted to **Places API (New)** with *Application restrictions*: **IP addresses** (or none for local use). It stays in `.env` and is never sent to the browser.

## 4. Health
The browser reports Google map success/failure to `/api/integrations/maps/status`; refresh and lookup results update the `osm`, `open_charge_map` and `google_places` cards on **Data integrations**.
