import json
import logging
import os
import tempfile
from datetime import timedelta
from functools import wraps

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

import config
from modbus_tcp import send_window_command

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


def default_status() -> dict:
    return {b: {w: False for w in windows} for b, windows in config.BUILDINGS.items()}


def reset_status_on_startup() -> None:
    """Physical window state is unknown after a restart — assume all closed."""
    save_status(default_status())
    log.info("Status reset to all-closed on startup")


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
    )


@app.route("/window_status")
def window_status():
    # Intentionally public: read-only, used by the dashboard auto-refresh.
    return jsonify(load_status())


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

    success, message = send_window_command(building, window, action)
    if not success:
        # Don't persist a state we failed to apply.
        return jsonify({"success": False, "error": message}), 502

    window_opened[building][window] = new_state
    save_status(window_opened)
    return jsonify({"success": True, "new_state": new_state, "message": message})


@app.route("/all/<action>", methods=["POST"])
@login_required
def control_all_buildings(action):
    if action not in ("open", "close"):
        flash("Action invalide", "danger")
        return redirect(url_for("home"))

    window_opened = load_status()
    failures: list[str] = []

    for building_id, window_list in config.BUILDINGS.items():
        if building_id not in config.EQUIPPED_BUILDINGS:
            continue
        for window_id in window_list:
            success, message = send_window_command(building_id, window_id, action)
            if success:
                window_opened[building_id][window_id] = (action == "open")
            else:
                failures.append(f"{building_id}/{window_id}: {message}")

    save_status(window_opened)

    label = "Ouverture" if action == "open" else "Fermeture"
    if failures:
        flash(f"{label} partielle — {len(failures)} erreur(s) : " + "; ".join(failures),
              "warning")
    else:
        flash(f"{label} envoyée à toutes les fenêtres équipées", "success")
    return redirect(url_for("home"))


reset_status_on_startup()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0")
