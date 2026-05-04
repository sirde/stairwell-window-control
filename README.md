# Extracteur — Stairwell Smoke-Extractor Window Control

Small Flask web app that controls the smoke-extractor windows in the stairwells
of our building. It runs on a Raspberry Pi in the building and talks to a
Modbus-TCP relay module which drives the window motors.

At night residents can open the windows to air the stairwells, and close them
again in the morning. There is currently no automatic rain/wind protection —
the operator is responsible for closing the windows when the weather turns.

## Architecture

```
Browser ──HTTP──> Flask (app.py) ──Modbus/TCP──> Relay module ──> Window motors
                         │
                         └── status.json   (persisted open/closed state)
```

- `app.py` — Flask app, login, UI, toggle & bulk-control routes
- `modbus_tcp.py` — Modbus-TCP client, relay pulsing + auto-release timer
- `config.py` — Building/window layout and relay-module IP addresses
- `weather_checker.py` — Stand-alone script, polls OpenWeather (not wired in yet)
- `status.json` — Runtime state, created on first write

Each window uses two relay channels (open / close). `send_window_command`
pulses the relevant channel; `ModbusTCPClient` schedules an automatic
all-relays-off 10 s later so motors aren't left energised.

## Configuration

The app reads secrets from environment variables. Copy
`extracteur.env.example` to `extracteur.env` and fill it in:

> The file is intentionally **not** named `.env`. Docker Compose
> auto-loads `.env` for YAML interpolation, which mangles werkzeug
> password hashes (they contain `$`). Using a different name avoids
> that.

| Variable | Used by | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | `app.py` | Flask session signing key |
| `ADMIN_USERNAME`   | `app.py` | Web UI login |
| `ADMIN_PASSWORD_HASH` | `app.py` | Web UI password, as a werkzeug hash |
| `OPENWEATHER_API_KEY` | `weather_checker.py` | OpenWeatherMap API key |
| `OPENWEATHER_LAT` / `OPENWEATHER_LON` | `weather_checker.py` | Location (optional) |
| `RELAY_IP_BUILDING_1` / `_3` / `_5` | `config.py` | IP of each Modbus-TCP relay module |
| `EQUIPPED_BUILDINGS` | `config.py` | Comma-separated list of wired buildings (e.g. `building_1`) |

Generate a password hash with:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
```

Paste the result into `extracteur.env` as `ADMIN_PASSWORD_HASH=...`.

Edit `config.py` to change the building/window layout. Relay-module IPs and
the equipped-buildings list are configured via the env vars above.

## Running with Docker

The repo ships a `docker-compose.yml` that wires up the port, env file,
restart policy and the `status.json` bind-mount:

```bash
docker compose up -d --build
```

Then open `http://<pi-address>:5000`.

To view logs / restart / stop:

```bash
docker compose logs -f
docker compose restart
docker compose down
```

## Deploying to the Raspberry Pi

Build happens on the Pi itself — the image is tiny, no registry needed.

**First-time setup** on the Pi:

```bash
sudo mkdir -p /opt/extracteur
sudo chown $USER /opt/extracteur
git clone <repo-url> /opt/extracteur
cd /opt/extracteur

# Create the real env file from the template, fill in the secrets:
cp extracteur.env.example extracteur.env
chmod 600 extracteur.env
$EDITOR extracteur.env

# First start
docker compose up -d --build
```

**Updates** (from your laptop, push; on the Pi):

```bash
cd /opt/extracteur
git pull
docker compose up -d --build
```

### About the `extracteur.env` file

Secrets are **not** baked into the image. `extracteur.env` is
`.gitignore`d and `.dockerignore`d, and is read by Docker at container
start via `env_file:` in the compose file. Keep it on the Pi at
`/opt/extracteur/extracteur.env` with mode `600`. To rotate a credential,
edit it and run `docker compose up -d` — no rebuild needed.

A `data/` directory next to `docker-compose.yml` is bind-mounted to
`/app/data` so `status.json` (window state) survives container restarts.
The directory is created automatically on first start.

Then open `http://<pi-address>:5000`.

## Running locally

```bash
pip install -r requirements.txt
export $(grep -v '^#' extracteur.env | xargs)
python app.py
```

## Notes

- The app listens on `0.0.0.0:5000` in debug mode. For production, put it
  behind a reverse proxy and disable `debug=True`.
- `status.json` reflects the last command sent, not the real physical state
  of the windows (no feedback contacts wired yet).
