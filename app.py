import io
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

import config
import db
import notify
import radar
import weather
from modbus_tcp import send_window_command
from notifier import heartbeat

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)

USERNAME = os.environ["ADMIN_USERNAME"]
PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")

DISPLAY_TZ = ZoneInfo(config.DISPLAY_TZ)

# Human labels for the event list.
ACTION_FR = {"open": "Ouverture", "close": "Fermeture", "reset": "Réinitialisation"}
SOURCE_FR = {"manual": "Manuel", "advisory": "Conseil météo",
             "auto": "Automatique", "system": "Système"}
ACTOR_FR = {"startup": "Démarrage", "cooling": "Refroidissement",
            "stale": "Météo indisponible"}


@app.template_filter("localdt")
def localdt(iso_utc: str) -> str:
    """Render a stored UTC ISO timestamp as local d/m/y hh:mm (display only)."""
    if not iso_utc:
        return ""
    dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime("%d/%m/%Y %H:%M")


def window_label(building: str, window: str) -> str:
    """Human location for a notification, e.g. 'Bâtiment 1 · fenêtre A'."""
    return f"{building.replace('building_', 'Bâtiment ')} · fenêtre {window}"


def actor_label(actor: str | None) -> str:
    """Friendly French label for an event's actor (user / weather trigger)."""
    if not actor:
        return "—"
    if actor in ACTOR_FR:
        return ACTOR_FR[actor]
    parts = actor.split("+")
    if parts and all(p in weather.TRIGGER_FR for p in parts):
        return "Météo (" + "/".join(weather.TRIGGER_FR[p] for p in parts) + ")"
    return actor


def default_status() -> dict:
    return {b: {w: False for w in windows} for b, windows in config.BUILDINGS.items()}


def reset_status_on_startup() -> None:
    """Physical window state is unknown after a restart — assume all closed."""
    save_status(default_status())
    db.record_event(
        "reset", source="system", actor="startup",
        reason="Redémarrage de l'application — état réinitialisé (toutes fermées)",
    )
    log.info("Status reset to all-closed on startup")
    notify.send("app_started", "Application redémarrée",
                "L'application a redémarré — fenêtres réinitialisées à « fermées ».")


def load_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_status()

    # Reconcile with current config: add missing buildings/windows, drop stale.
    reconciled = default_status()
    for building, windows in reconciled.items():
        for window in windows:
            if building in data and window in data[building]:
                reconciled[building][window] = bool(data[building][window])
    return reconciled


def save_status(data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(STATUS_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, STATUS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@app.context_processor
def inject_auth():
    return {"logged_in": bool(session.get("logged_in"))}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == USERNAME and check_password_hash(PASSWORD_HASH, password):
            session.permanent = True
            session["logged_in"] = True
            session["user"] = username
            return redirect(url_for("home"))
        flash("Identifiants invalides", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/")
def home():
    return render_template(
        "home.html",
        window_opened=load_status(),
        buildings=config.BUILDINGS,
        equipped_buildings=config.EQUIPPED_BUILDINGS,
        weather=weather.current(),
        radar=radar.latest(),
        radar_radius_km=int(config.RADAR_RAIN_RADIUS_KM),
        automation=db.get_automation(),
        auto_open_min=db.AUTO_OPEN_TEMP_MIN,
        auto_open_max=db.AUTO_OPEN_TEMP_MAX,
    )


@app.route("/window_status")
def window_status():
    # Intentionally public: read-only, used by the dashboard auto-refresh.
    return jsonify(load_status())


@app.route("/weather.json")
def weather_json():
    # Public read-only, like /window_status. Used by the home weather panel.
    current = weather.current()
    return jsonify(current if current else {"available": False})


@app.route("/radar.json")
def radar_json():
    # Public read-only radar nowcast for the home panel.
    latest = radar.latest()
    return jsonify(latest if latest else {"available": False})


@app.route("/radar.png")
def radar_png():
    # Composited radar map (cached) for the hover preview.
    if not (config.RADAR_ENABLED and radar.available()):
        return "", 404
    try:
        png = radar.cached_image()
    except Exception:
        log.exception("Radar image compose failed")
        return "", 502
    return app.response_class(png, mimetype="image/png")


def _qr_available() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


@app.route("/notifications")
def notifications():
    # Public: residents subscribe without the admin password. Subscription
    # itself happens in the ntfy app — the server keeps no subscriber list.
    topic = config.NTFY_TOPIC
    return render_template(
        "notifications.html",
        ntfy_enabled=bool(config.NTFY_ENABLED and topic),
        ntfy_server=config.NTFY_SERVER,
        ntfy_topic=topic,
        subscribe_url=f"{config.NTFY_SERVER}/{topic}" if topic else None,
        qr_available=_qr_available(),
    )


@app.route("/notifications/qr.png")
def notifications_qr():
    """QR of the ntfy topic URL — scan it to subscribe in the app."""
    if not config.NTFY_TOPIC:
        return "", 404
    try:
        import qrcode
    except ImportError:
        return "", 404
    img = qrcode.make(f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return app.response_class(buf.getvalue(), mimetype="image/png")


@app.route("/healthz")
def healthz():
    # Liveness probe — public, for an external uptime monitor if used alongside
    # (or instead of) the outbound healthchecks.io heartbeat.
    return jsonify({"status": "ok"})


@app.route("/conditions")
@login_required
def conditions():
    wseries = []
    for r in db.recent_weather(limit=500):
        raw = json.loads(r["raw"]) if r.get("raw") else {}
        wseries.append({
            "ts": r["ts"], "temp": r["temp_c"], "wind": r["wind_gust_kmh"],
            "rain_prob": r["rain_prob"], "cape": raw.get("cape_max"),
            "advise": r["advise_close"], "caution": r.get("caution"),
        })
    rseries = [{"ts": r["ts"], "rain_near": r["rain_near"], "nearest_km": r["nearest_km"]}
               for r in db.recent_radar(limit=500)]
    wseries.reverse()           # recent_* return newest-first; charts want oldest-first
    rseries.reverse()
    return render_template(
        "conditions.html",
        wseries=wseries, rseries=rseries,
        wind_threshold=config.WIND_GUST_THRESHOLD_KMH,
        rain_threshold=config.RAIN_PROB_THRESHOLD,
        cape_threshold=config.CAPE_THRESHOLD,
        radar_radius=int(config.RADAR_RAIN_RADIUS_KM),
    )


@app.route("/settings/automation", methods=["POST"])
@login_required
def settings_automation():
    """Save automation settings — any subset of {open, close, threshold}."""
    data = request.get_json(silent=True) or {}
    kwargs = {}
    if "open_enabled" in data:
        kwargs["open_enabled"] = bool(data["open_enabled"])
    if "close_enabled" in data:
        kwargs["close_enabled"] = bool(data["close_enabled"])
    if "open_temp_c" in data:
        try:
            kwargs["open_temp_c"] = int(data["open_temp_c"])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Seuil invalide"}), 400
    if not kwargs:
        return jsonify({"success": False, "error": "Aucun réglage fourni"}), 400

    saved = db.set_automation(**kwargs)
    log.info("Automation settings updated by %s: %s", session.get("user"), saved)
    # Re-evaluate immediately so toggling on during a storm (or on a cool, calm
    # evening) logs the simulated action at once instead of next poll.
    try:
        weather.evaluate(load_status)
    except Exception:
        log.exception("Automation re-evaluation after settings change failed")
    return jsonify({"success": True, **saved})


@app.route("/history")
@login_required
def history():
    events = db.recent_events(limit=300)
    for e in events:
        e["conditions"] = json.loads(e["conditions"]) if e.get("conditions") else None
        e["action_fr"] = ACTION_FR.get(e["action"], e["action"])
        e["source_fr"] = SOURCE_FR.get(e["source"], e["source"])
        e["actor_fr"] = actor_label(e["actor"])
    return render_template("history.html", events=events)


@app.route("/toggle_window", methods=["POST"])
@login_required
def toggle_window():
    data = request.get_json(silent=True) or {}
    building = data.get("building")
    window = data.get("window")

    window_opened = load_status()
    if building not in window_opened or window not in window_opened[building]:
        return jsonify({"success": False, "error": "Fenêtre inconnue"}), 400
    if building not in config.EQUIPPED_BUILDINGS:
        return jsonify({"success": False, "error": "Bâtiment non équipé"}), 400

    new_state = not window_opened[building][window]
    action = "open" if new_state else "close"
    actor = session.get("user")
    conditions = weather.latest()

    success, message = send_window_command(building, window, action)
    if not success:
        # Don't persist a state we failed to apply, but do record the attempt.
        db.record_event(action, building=building, window=window, source="manual",
                        actor=actor, reason=f"Action manuelle — échec : {message}",
                        success=False, conditions=conditions)
        notify.send("relay_unreachable", "Module injoignable",
                    f"{window_label(building, window)} : commande "
                    f"« {ACTION_FR.get(action, action).lower()} » échouée — {message}")
        return jsonify({"success": False, "error": message}), 502

    window_opened[building][window] = new_state
    save_status(window_opened)
    db.record_event(action, building=building, window=window, source="manual",
                    actor=actor, reason="Action manuelle", success=True,
                    conditions=conditions)
    if config.NOTIFY_MANUAL_ACTIONS:
        event = "window_opened" if new_state else "window_closed"
        verb = "ouverte" if new_state else "fermée"
        notify.send(event, f"Fenêtre {verb}",
                    f"{window_label(building, window)} {verb} manuellement par {actor}.")
    return jsonify({"success": True, "new_state": new_state, "message": message})


@app.route("/all/<action>", methods=["POST"])
@login_required
def control_all_buildings(action):
    if action not in ("open", "close"):
        flash("Action invalide", "danger")
        return redirect(url_for("home"))

    window_opened = load_status()
    failures: list[str] = []
    actor = session.get("user")
    conditions = weather.latest()
    bulk_reason = f"Action groupée — tout {'ouvrir' if action == 'open' else 'fermer'}"

    for building_id, window_list in config.BUILDINGS.items():
        if building_id not in config.EQUIPPED_BUILDINGS:
            continue
        for window_id in window_list:
            success, message = send_window_command(building_id, window_id, action)
            if success:
                window_opened[building_id][window_id] = (action == "open")
            else:
                failures.append(f"{building_id}/{window_id}: {message}")
            db.record_event(
                action, building=building_id, window=window_id, source="manual",
                actor=actor,
                reason=bulk_reason if success else f"{bulk_reason} — échec : {message}",
                success=success, conditions=conditions,
            )

    save_status(window_opened)

    label = "Ouverture" if action == "open" else "Fermeture"
    # One push for the whole bulk action, not one per window.
    if failures:
        notify.send("relay_unreachable", "Module injoignable",
                    f"{label} groupée — {len(failures)} fenêtre(s) en échec : "
                    + "; ".join(failures))
    if config.NOTIFY_MANUAL_ACTIONS:
        verb = "ouvertes" if action == "open" else "fermées"
        event = "window_opened" if action == "open" else "window_closed"
        notify.send(event, f"Fenêtres {verb}",
                    f"Toutes les fenêtres équipées {verb} manuellement par {actor}"
                    + (f" ({len(failures)} en échec)." if failures else "."))
    if failures:
        flash(f"{label} partielle — {len(failures)} erreur(s) : " + "; ".join(failures),
              "warning")
    else:
        flash(f"{label} envoyée à toutes les fenêtres équipées", "success")
    return redirect(url_for("home"))


db.init()
reset_status_on_startup()
if os.environ.get("ENABLE_WEATHER_POLLER", "1") == "1":
    weather.start_poller(load_status)
if config.RADAR_ENABLED and radar.available():
    # Re-evaluate the close advisory on each radar poll too, so radar-detected
    # rain reacts without waiting for the next (slower) forecast poll.
    radar.start_poller(on_update=lambda: weather.evaluate(load_status))
elif config.RADAR_ENABLED:
    log.warning("Radar enabled but Pillow is missing — radar disabled")
if config.HEALTHCHECK_URL:
    # Outbound heartbeat: a missed ping is how the external monitor learns the
    # app (or the whole Pi) is down — the one alert the app can't send itself.
    heartbeat.start(config.HEALTHCHECK_URL, config.HEALTHCHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0")
