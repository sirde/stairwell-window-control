import os
from flask import Flask, render_template_string, redirect, url_for, request, session, flash
from werkzeug.security import check_password_hash
from datetime import timedelta
from functools import wraps

import config

from modbus_tcp import ModbusTCPClient, send_window_command

from flask import jsonify
import json


app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)  # Stay logged in for 30 days

USERNAME = os.environ["ADMIN_USERNAME"]
PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]


STATUS_FILE = "status.json"

def save_status(data):
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f)

def load_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return     {
            "building_1": {
                "A": False
            },
            "building_3": {
                "A": False,
                "B": False,
                "D": False,
                "E": False,
            },
            "building_5": {
                "A": False,
                "B": False,
                "D": False,
                "E": False
            }
}  # default if no file exists


@app.route("/window_status")
def window_status():
    window_opened = load_status()
    print(window_opened)
    return jsonify(window_opened)


# --- Login Required Decorator ---
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
        if request.form["username"] == USERNAME and check_password_hash(PASSWORD_HASH, request.form["password"]):
            session["logged_in"] = True
            return redirect(url_for("home"))
        flash("Invalid credentials", "danger")
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="icon" type="image/x-icon" href="{{ url_for('static', filename='favicon.ico') }}">
    </head>
    <body class="container p-5">
        <h2>Login</h2>
        <form method="post" class="w-25">
            <input class="form-control mb-2" name="username" placeholder="Username" required>
            <input class="form-control mb-2" name="password" type="password" placeholder="Password" required>
            <button class="btn btn-primary" type="submit">Login</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </body>
    </html>
    """)

@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Window control</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="icon" type="image/x-icon" href="{{ url_for('static', filename='favicon.ico') }}">
        <style>
            .building {
                margin-bottom: 2em;
            }
            .windows {
                display: flex;
                flex-wrap: wrap;
                gap: 1em;
            }
            .window-box {
                padding: 1em;
                border-radius: 10px;
                background-color: #f9f9f9;
                min-width: 120px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                text-align: center;
            }
            .window-label {
                font-weight: bold;
                display: block;
                font-size: 1.2em;
            }
            .status.open {
                color: green;
            }
            .status.closed {
                color: red;
            }
            .status.moving {
                color: orange;
            }
            .status {
                cursor: pointer;
                user-select: none;
            }
            .windows.disabled .window-box {
                opacity: 0.5;
            }
            .windows.disabled .status {
                cursor: not-allowed;
            }
        </style>
    </head>
    <body class="container p-5">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h2>🏠 Controle des extracteurs de fumée des cages d'escalier</h2>
            <div>
                <a href="{{ url_for('control_all_buildings', action='open') }}" class="btn btn-success btn-sm me-2">Open All Windows</a>
                <a href="{{ url_for('control_all_buildings', action='close') }}" class="btn btn-danger btn-sm">Close All Windows</a>
                <a href="{{ url_for('logout') }}" class="btn btn-secondary btn-sm ms-3">Logout</a>
            </div>
        </div>

        <div class="alert alert-info" role="alert">
            Cette interface permet de contrôler l'ouverture des fenêtres afin d’aérer la cage d’escalier durant la nuit.<br>
            ⚠️ Actuellement, il n’y a <strong>aucune protection contre la pluie ou le vent</strong> – veillez à refermer les fenêtres en cas de mauvaise météo.<br>
            ☀️ Pensez également à refermer les fenêtres le matin lorsque la température commence à monter.
        </div>

        <h2>État des fenêtres</h2>
        {% for building, windows in buildings.items() %}
            {% set equipped = building in equipped_buildings %}
            <div class="building">
                <h3>
                    {{ building.replace('_', ' ').title() }}
                    {% if not equipped %}
                        <span class="badge bg-warning text-dark ms-2">⚠️ Non équipé</span>
                    {% endif %}
                </h3>
                {% if not equipped %}
                    <div class="alert alert-warning py-2 mb-2">
                        Ce bâtiment n'est pas encore équipé du module de contrôle.
                        Les commandes sont désactivées.
                    </div>
                {% endif %}
                <div class="windows {% if not equipped %}disabled{% endif %}">
                    {% for window_id in windows %}
                        {% set status = window_opened[building][window_id] %}
                        <div class="window-box">
                            <span class="window-label">{{ window_id }}</span>
                            <span id="{{ building }}_{{ window_id }}"
                                  class="status {% if status == True %}open{% else %}closed{% endif %}"
                                  {% if equipped %}onclick="toggleWindow('{{ building }}', '{{ window_id }}')"{% endif %}>
                                {% if status == True %}
                                    🟢 Ouverte
                                {% else %}
                                    🔴 Fermée
                                {% endif %}
                            </span>
                        </div>
                    {% endfor %}
                </div>
            </div>
        {% endfor %}


        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }} mt-3">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <script>
        function updateStatuses() {
          console.log("Updating window statuses...");
          fetch('/window_status')
            .then(response => response.json())
            .then(data => {
              console.log("Received data:", data);
              for (const [building, windows] of Object.entries(data)) {
                for (const [windowId, status] of Object.entries(windows)) {
                  const elemId = `${building}_${windowId}`;
                  const el = document.getElementById(elemId);
                  if (el) {
                    if (status === true) {
                      el.className = 'status open';
                      el.textContent = '🟢 Ouverte';
                    } else {
                      el.className = 'status closed';
                      el.textContent = '🔴 Fermée';
                    }
                  } else {
                    console.warn("Element not found:", elemId);
                  }
                }
              }
            })
            .catch(error => console.error('Error fetching status:', error));
        }

        function toggleWindow(building, windowId) {
            fetch('/toggle_window', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ building: building, window: windowId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    updateStatuses();
                } else {
                    alert('Erreur: ' + data.error);
                }
            });
        }

        // Auto update every 5 seconds
        setInterval(updateStatuses, 5000);
        </script>
    </body>
    </html>
    """, window_opened=load_status(), buildings=config.BUILDINGS,
         equipped_buildings=config.EQUIPPED_BUILDINGS)

@app.route("/toggle_window", methods=["POST"])
def toggle_window():
    data = request.get_json()
    building = data.get("building")
    window = data.get("window")

    window_opened = load_status()
    if building not in window_opened or window not in window_opened[building]:
        return jsonify({"error": "Invalid window"}), 400
    if building not in config.EQUIPPED_BUILDINGS:
        return jsonify({"error": "Building not equipped"}), 400

    # Toggle state
    current = window_opened[building][window]
    new_state = False if current == True else True
    window_opened[building][window] = new_state

    # Optionally trigger your relay logic here
    # await trigger_relay_async(building, window, new_state)
    success, message = send_window_command(building, window, "open" if new_state == True else "close")
    flash(message, "success" if success else "danger")

    save_status(window_opened)

    return jsonify({"success": True, "new_state": new_state})

@app.route("/all/<action>")
@login_required
def control_all_buildings(action):
    window_opened = load_status()

    if action not in ["open", "close"]:
        flash("Invalid action", "danger")
        return redirect(url_for("home"))

    for building_id, window_list in config.BUILDINGS.items():
        if building_id not in config.EQUIPPED_BUILDINGS:
            continue
        for window_id in window_list:

            window_opened[building_id][window_id] = (action == "open")
            send_window_command(building_id, window_id, action)

    flash(f"✅ Sent '{action.title()}' to all windows in all buildings", "success")
    save_status(window_opened)

    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True,host='0.0.0.0')