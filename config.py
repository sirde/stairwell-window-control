import os


# Fail fast with one clear message if a required secret is missing, instead of a
# raw KeyError from the first bare os.environ[...] below. These are the only vars
# without a code default — everything else falls back to defaults.env / code.
_REQUIRED = (
    "FLASK_SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD_HASH",
    "RELAY_IP_BUILDING_1", "RELAY_IP_BUILDING_3", "RELAY_IP_BUILDING_5",
    "EQUIPPED_BUILDINGS",
)
_missing = [k for k in _REQUIRED if k not in os.environ]
if _missing:
    raise SystemExit(
        "Missing required environment variable(s): " + ", ".join(_missing)
        + "\nSet them in extracteur.env (copy extracteur.env.example). "
        "Non-secret tunables default via defaults.env / code."
    )


# Window structure per building
BUILDINGS = {
    "building_1": ["A"],
    "building_3": ["A", "B", "D", "E"],
    "building_5": ["A", "B", "D", "E"]
}

# IP address of the Modbus-TCP relay module for each building.
module_address = {
    "building_1": os.environ["RELAY_IP_BUILDING_1"],
    "building_3": os.environ["RELAY_IP_BUILDING_3"],
    "building_5": os.environ["RELAY_IP_BUILDING_5"],
}

# Buildings that actually have the relay module wired up. Others are shown
# in the UI but disabled. Set via EQUIPPED_BUILDINGS, comma-separated.
EQUIPPED_BUILDINGS = {
    b.strip()
    for b in os.environ["EQUIPPED_BUILDINGS"].split(",")
    if b.strip()
}

# --- Weather (Open-Meteo, no API key) ----------------------------------------
# Location of the residence. Falls back to the older OPENWEATHER_* names so an
# existing Pi env keeps working after the provider switch.
WEATHER_LAT = float(os.environ.get("WEATHER_LAT",
                    os.environ.get("OPENWEATHER_LAT", "46.3833")))
WEATHER_LON = float(os.environ.get("WEATHER_LON",
                    os.environ.get("OPENWEATHER_LON", "6.2333")))

# How often the background poller fetches the forecast.
WEATHER_POLL_SECONDS = int(os.environ.get("WEATHER_POLL_SECONDS", "600"))

# Preferred Open-Meteo models, tried in order. CH1 (1 km) is the model family
# behind MeteoSwiss warnings and resolves Alpine convection the global blend
# misses; we fall back through CH2 to the default best-match if a model has no
# data for the location.
WEATHER_MODELS = [m.strip() for m in os.environ.get(
    "WEATHER_MODELS", "meteoswiss_icon_ch1,meteoswiss_icon_ch2").split(",")
    if m.strip()]

# Two-tier horizon: imminent rain/wind that warrants closing *now*, and a longer
# thunderstorm "watch" because storms are usually hours out (gusts at the grid
# point under-call them, so we trigger on the model's thunderstorm code instead).
WEATHER_LOOKAHEAD_HOURS = int(os.environ.get("WEATHER_LOOKAHEAD_HOURS", "3"))
WEATHER_WATCH_HOURS = int(os.environ.get("WEATHER_WATCH_HOURS", "12"))

# Advise closing when the forecast crosses either threshold within the lookahead.
RAIN_PROB_THRESHOLD = int(os.environ.get("RAIN_PROB_THRESHOLD", "60"))        # %
WIND_GUST_THRESHOLD_KMH = float(os.environ.get("WIND_GUST_THRESHOLD_KMH", "40"))

# CAPE (convective fuel) shown for context and flagged above this value (J/kg).
CAPE_THRESHOLD = float(os.environ.get("CAPE_THRESHOLD", "1000"))

# --- Night-airing time gate (auto-open) --------------------------------------
# Auto-open (cooling) is limited to the cool part of the day: the whole night
# plus a grace period past sunrise. Our roof/windows face south, so they barely
# heat in the first hours after dawn (the sun is still low in the NE), and the
# coldest outdoor air of the day is right after sunrise — so we keep venting this
# many hours past sunrise before the day's solar gain makes importing outside air
# counter-productive. The night side of the window comes from Open-Meteo's
# is_day flag; this only extends the morning cutoff.
MORNING_VENT_GRACE_HOURS = float(os.environ.get("MORNING_VENT_GRACE_HOURS", "2"))

# Graceful degradation when the forecast can't be fetched: fall back to the LAST
# known state. A "touchy" last reading (wind / rain / storm risk) gets a short
# grace period before a precautionary close — being blind during risk is the
# dangerous case. A calm last reading (no wind, no rain) tolerates a much longer
# outage before reacting.
WEATHER_STALE_RISKY_SECONDS = int(os.environ.get("WEATHER_STALE_RISKY_SECONDS", "600"))   # 10 min
WEATHER_STALE_CALM_SECONDS = int(os.environ.get("WEATHER_STALE_CALM_SECONDS", "5400"))    # 90 min

# --- Radar nowcast (RainViewer, free, no key) --------------------------------
# Observe-only: detects rain on/near the residence that forecasts miss. Needs
# Pillow; disable here or it self-disables if Pillow is absent.
RADAR_ENABLED = os.environ.get("RADAR_ENABLED", "1") == "1"
RADAR_POLL_SECONDS = int(os.environ.get("RADAR_POLL_SECONDS", "300"))
# RainViewer's global mosaic only serves up to zoom 7; higher returns a
# "Zoom Level Not Supported" placeholder whose grey text would be misread as
# rain. Keep sampling at 7.
RADAR_ZOOM = int(os.environ.get("RADAR_ZOOM", "7"))
# Echo within this radius of the residence counts as "rain near us" (km).
RADAR_RAIN_RADIUS_KM = float(os.environ.get("RADAR_RAIN_RADIUS_KM", "10"))
# Hover preview image (base map + radar overlay), composited server-side.
RADAR_IMAGE_ZOOM = int(os.environ.get("RADAR_IMAGE_ZOOM", "9"))
# RainViewer's global mosaic only serves tiles up to this zoom (above it returns
# a "not supported" placeholder); higher display zooms upscale from here.
RADAR_TILE_MAX_ZOOM = int(os.environ.get("RADAR_TILE_MAX_ZOOM", "7"))
RADAR_IMAGE_SIZE = int(os.environ.get("RADAR_IMAGE_SIZE", "384"))
RADAR_IMAGE_TTL = int(os.environ.get("RADAR_IMAGE_TTL", "120"))

# --- Notifications (ntfy.sh) --------------------------------------------------
# Push alerts via ntfy.sh (free, no account; self-hostable). Residents subscribe
# by adding the topic in the ntfy app — there is no server-side subscriber list.
# The topic name is the only access control on the public server, so pick an
# unguessable one (anyone who knows it can read AND publish). NTFY_TOKEN locks
# publishing on a reserved/self-hosted topic.
NTFY_ENABLED = os.environ.get("NTFY_ENABLED", "1") == "1"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "") or None

# Public URL of this dashboard, used as the "tap to open" target on a
# notification (and shown on the subscribe page). Optional.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Manual open/close from the dashboard also pushes a (low-priority) notification.
# Off by default so an admin tapping windows doesn't ping everyone; the valuable
# pushes are the weather-driven and failure ones.
NOTIFY_MANUAL_ACTIONS = os.environ.get("NOTIFY_MANUAL_ACTIONS", "0") == "1"

# --- App-down watchdog (healthchecks.io heartbeat) ---------------------------
# The app pings this URL on a timer; a missed ping makes the external monitor
# raise the alarm (point that monitor's alert at the same ntfy topic for one
# unified stream). Outbound-only, so it needs no inbound exposure. Disabled when
# the URL is empty.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")
HEALTHCHECK_INTERVAL_SECONDS = int(os.environ.get("HEALTHCHECK_INTERVAL_SECONDS", "300"))

# --- Persistence / display ---------------------------------------------------
# SQLite database for the event history and weather snapshots. In Docker this
# points into the bind-mounted data/ directory (see docker-compose.yml).
DB_FILE = os.environ.get("DB_FILE", "extracteur.db")

# Timezone used only for rendering user-facing timestamps (storage stays UTC).
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "Europe/Paris")