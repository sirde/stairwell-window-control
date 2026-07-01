"""RainViewer radar nowcast: "is it raining on/near us right now (and incoming)?"

Forecast models can't resolve a single convective cell overhead — proven on
2026-06-29, when even Open-Meteo's minutely_15 showed 0 mm during an active
thunderstorm 5 km away. Weather radar can. This samples RainViewer's free global
radar mosaic at the residence's location:

- the latest frame answers "raining near us now",
- the last few frames give an approaching/receding trend,
- the nowcast frames (when RainViewer publishes them) give a short ETA.

Observe-first, like the forecast model: the signal is displayed and logged so its
reliability can be judged before it ever drives a window. Needs Pillow to decode
the PNG tiles; if it's missing the module disables itself gracefully.
"""
import io
import logging
import math
import threading
import time

import requests

import config
import db

try:
    from PIL import Image, ImageDraw
except Exception:  # Pillow not installed — radar disabled, app still runs.
    Image = None
    ImageDraw = None

log = logging.getLogger(__name__)

MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"
# Carto light basemap: free, permissive for low volume, and a pale base that
# keeps the radar overlay legible. {s} is a subdomain (a-d).
BASEMAP_URL = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
TILE_SIZE = 256
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

_latest: dict | None = None
_latest_lock = threading.Lock()

# Cached composited PNG for the hover preview (regenerated lazily).
_image_cache: dict = {"png": None, "ts": 0.0}
_image_lock = threading.Lock()


def available() -> bool:
    return Image is not None


def latest() -> dict | None:
    with _latest_lock:
        return dict(_latest) if _latest else None


def _set_latest(summary: dict) -> None:
    global _latest
    with _latest_lock:
        _latest = summary


# --- Web-Mercator helpers (global pixel space at a given zoom) ----------------

def _global_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = (2 ** z) * TILE_SIZE
    gx = (lon + 180) / 360 * n
    gy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return gx, gy


def _px_latlon(gx: float, gy: float, z: int) -> tuple[float, float]:
    n = (2 ** z) * TILE_SIZE
    lon = gx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / n))))
    return lat, lon


def _km_per_px(lat: float, z: int) -> float:
    return 156.543 * math.cos(math.radians(lat)) / (2 ** z)


def _haversine(a: float, b: float, c: float, d: float) -> float:
    R = 6371.0
    p = math.radians
    h = (math.sin(p(c - a) / 2) ** 2
         + math.cos(p(a)) * math.cos(p(c)) * math.sin(p(d - b) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def _bearing(a: float, b: float, c: float, d: float) -> str:
    p = math.radians
    y = math.sin(p(d - b)) * math.cos(p(c))
    x = (math.cos(p(a)) * math.sin(p(c))
         - math.sin(p(a)) * math.cos(p(c)) * math.cos(p(d - b)))
    return _COMPASS[round(((math.degrees(math.atan2(y, x)) + 360) % 360) / 45) % 8]


# --- Sampling -----------------------------------------------------------------

def _tile(host: str, path: str, z: int, xt: int, yt: int):
    url = f"{host}{path}/{TILE_SIZE}/{z}/{xt}/{yt}/2/0_0.png"
    raw = requests.get(url, timeout=10).content
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    # Guard against a "not supported" placeholder being read as rain: a real
    # radar tile is either transparent (no echo) or saturated colour (echo);
    # the placeholder has opaque-ish pixels that are grey (low saturation).
    if img.getextrema()[3][1] > 0 and img.convert("RGB").convert("HSV").getextrema()[1][1] < 120:
        raise RuntimeError("placeholder radar tile (no real echo)")
    return img.load()


def _nearest_echo(host: str, path: str, lat: float, lon: float,
                  z: int, radius_km: float):
    """Nearest radar echo to (lat,lon) within radius. (km, lat, lon, alpha) | None."""
    gx, gy = _global_px(lat, lon, z)
    r = int(radius_km / _km_per_px(lat, z)) + 1
    cx, cy = int(gx), int(gy)
    tiles: dict = {}
    best = None
    for gyy in range(cy - r, cy + r + 1):
        for gxx in range(cx - r, cx + r + 1):
            key = (gxx // TILE_SIZE, gyy // TILE_SIZE)
            pix = tiles.get(key)
            if pix is None:
                try:
                    pix = _tile(host, path, z, key[0], key[1])
                except Exception:
                    pix = False
                tiles[key] = pix
            if pix is False:
                continue
            if pix[gxx % TILE_SIZE, gyy % TILE_SIZE][3] > 0:
                la, lo = _px_latlon(gxx + 0.5, gyy + 0.5, z)
                dist = _haversine(lat, lon, la, lo)
                if dist <= radius_km and (best is None or dist < best[0]):
                    alpha = pix[gxx % TILE_SIZE, gyy % TILE_SIZE][3]
                    best = (dist, la, lo, alpha)
    return best


def fetch_radar() -> dict:
    if Image is None:
        raise RuntimeError("Pillow not available")
    lat, lon = config.WEATHER_LAT, config.WEATHER_LON
    z, radius = config.RADAR_ZOOM, config.RADAR_RAIN_RADIUS_KM

    rv = requests.get(MAPS_URL, timeout=10).json()
    host = rv["host"]
    past = rv["radar"]["past"]
    nowcast = rv["radar"].get("nowcast", [])
    latest_frame = past[-1]

    cur = _nearest_echo(host, latest_frame["path"], lat, lon, z, radius)

    # Approaching/receding from the nearest-echo distance over recent frames.
    series = [(_nearest_echo(host, f["path"], lat, lon, z, radius) or (None,))[0]
              for f in past[-3:]]
    have = [s for s in series if s is not None]
    approaching = (len(have) >= 2 and series[0] is not None
                   and series[-1] is not None and series[-1] < series[0] - 1)

    # ETA from nowcast frames (if RainViewer is publishing them).
    eta_min = None
    for f in nowcast:
        e = _nearest_echo(host, f["path"], lat, lon, z, radius)
        if e is not None:
            eta_min = (f["time"] - latest_frame["time"]) // 60
            break

    summary = {
        "rain_near": cur is not None,
        "nearest_km": round(cur[0], 1) if cur else None,
        "direction": _bearing(lat, lon, cur[1], cur[2]) if cur else None,
        "intensity": cur[3] if cur else 0,
        "approaching": approaching,
        "eta_min": eta_min,
        "nowcast_available": bool(nowcast),
        "frame_age_min": (rv["generated"] - latest_frame["time"]) // 60,
        "radius_km": radius,
        "fetched_at": db.iso_utc_now(),
    }
    return summary


# --- Hover preview image: OSM base + radar overlay + residence marker --------

def _base_tile(z: int, xt: int, yt: int):
    # Tile servers occasionally return a small error/placeholder as HTTP 200.
    # Real tiles here are tens of KB, so reject tiny ones and retry; on
    # persistent failure raise so the tile is skipped (the neutral canvas shows
    # through rather than an opaque placeholder box).
    headers = {"User-Agent": "extracteur/1.0 (building window control)"}
    last = None
    for attempt in range(3):
        url = BASEMAP_URL.format(s="abcd"[attempt % 4], z=z, x=xt, y=yt)
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200 and len(r.content) > 3000:
            return Image.open(io.BytesIO(r.content)).convert("RGBA")
        last = (r.status_code, len(r.content))
        time.sleep(0.3)
    raise RuntimeError(f"base tile {z}/{xt}/{yt} unusable after retries: {last}")


def _radar_tile_image(host: str, path: str, z: int, xt: int, yt: int):
    # RainViewer's mosaic caps at RADAR_TILE_MAX_ZOOM and returns a "not
    # supported" placeholder above it. For a higher display zoom, fetch the
    # parent tile at the max zoom and upscale/crop the matching quadrant.
    src_z = min(z, config.RADAR_TILE_MAX_ZOOM)
    shift = z - src_z
    sx, sy = xt >> shift, yt >> shift
    raw = requests.get(f"{host}{path}/{TILE_SIZE}/{src_z}/{sx}/{sy}/2/1_1.png", timeout=10).content
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    if shift == 0:
        return img
    factor = 1 << shift
    big = img.resize((TILE_SIZE * factor, TILE_SIZE * factor), Image.BILINEAR)
    ox = (xt - (sx << shift)) * TILE_SIZE
    oy = (yt - (sy << shift)) * TILE_SIZE
    return big.crop((ox, oy, ox + TILE_SIZE, oy + TILE_SIZE))


def compose_image() -> bytes:
    """A small map centred on the residence: OSM base, latest radar, marker+radius."""
    if Image is None:
        raise RuntimeError("Pillow not available")
    z = config.RADAR_IMAGE_ZOOM
    size = config.RADAR_IMAGE_SIZE
    lat, lon = config.WEATHER_LAT, config.WEATHER_LON

    gx, gy = _global_px(lat, lon, z)
    left, top = int(gx) - size // 2, int(gy) - size // 2
    rv = requests.get(MAPS_URL, timeout=10).json()
    host, path = rv["host"], rv["radar"]["past"][-1]["path"]

    canvas = Image.new("RGBA", (size, size), (233, 235, 239, 255))
    for xt in range(left // TILE_SIZE, (left + size) // TILE_SIZE + 1):
        for yt in range(top // TILE_SIZE, (top + size) // TILE_SIZE + 1):
            ox, oy = xt * TILE_SIZE - left, yt * TILE_SIZE - top
            for fetch in (lambda: _base_tile(z, xt, yt),
                          lambda: _radar_tile_image(host, path, z, xt, yt)):
                try:
                    canvas.alpha_composite(fetch(), (ox, oy))
                except Exception:
                    pass

    draw = ImageDraw.Draw(canvas)
    cx, cy = size // 2, size // 2
    rp = int(config.RADAR_RAIN_RADIUS_KM / _km_per_px(lat, z))
    draw.ellipse([cx - rp, cy - rp, cx + rp, cy + rp], outline=(40, 80, 220, 220), width=2)
    draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(220, 30, 30, 255),
                 outline=(255, 255, 255, 255))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def cached_image() -> bytes:
    now = time.monotonic()
    with _image_lock:
        if (_image_cache["png"] is None
                or now - _image_cache["ts"] > config.RADAR_IMAGE_TTL):
            _image_cache["png"] = compose_image()
            _image_cache["ts"] = now
        return _image_cache["png"]


def _poll_loop(stop_event: threading.Event, on_update) -> None:
    while not stop_event.is_set():
        try:
            summary = fetch_radar()
            _set_latest(summary)
            db.record_radar(
                rain_near=summary["rain_near"],
                nearest_km=summary["nearest_km"],
                approaching=summary["approaching"],
                eta_min=summary["eta_min"],
                raw=summary,
            )
            log.info("Radar: rain_near=%s nearest=%s km %s approaching=%s eta=%s "
                     "(nowcast=%s, frame %smin old)",
                     summary["rain_near"], summary["nearest_km"],
                     summary["direction"], summary["approaching"],
                     summary["eta_min"], summary["nowcast_available"],
                     summary["frame_age_min"])
            if on_update is not None:
                try:
                    on_update()
                except Exception:
                    log.exception("Radar on_update callback failed")
        except requests.RequestException as e:
            log.warning("Radar fetch failed: %s", e)
        except Exception:
            log.exception("Radar poll error")
        stop_event.wait(config.RADAR_POLL_SECONDS)


def start_poller(on_update=None) -> threading.Event:
    """Start the poller. on_update() is called after each successful poll so the
    close advisory can be re-evaluated promptly when radar detects rain."""
    stop_event = threading.Event()
    threading.Thread(target=_poll_loop, args=(stop_event, on_update),
                     name="radar-poller", daemon=True).start()
    log.info("Radar poller started (every %ss, %s km radius, zoom %s)",
             config.RADAR_POLL_SECONDS, config.RADAR_RAIN_RADIUS_KM, config.RADAR_ZOOM)
    return stop_event


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    import json
    print(json.dumps(fetch_radar(), indent=2, ensure_ascii=False))
