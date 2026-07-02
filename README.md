# Extracteur — Stairwell Smoke-Extractor Window Control

Small Flask web app that controls the smoke-extractor windows in the stairwells
of our building. It runs on a Raspberry Pi in the building and talks to a
Modbus-TCP relay module which drives the window motors.

At night residents can open the windows to air the stairwells, and close them
again in the morning. A background weather poller (Open-Meteo) watches the
forecast and, when rain or strong wind is likely, records a **close advisory**
in the history. This is **advisory only** for now — it does not actuate; the
operator is still responsible for closing the windows when the weather turns.

## Architecture

```
Browser ──HTTP──> Flask (app.py) ──Modbus/TCP──> Relay module ──> Window motors
                    │   │
                    │   └── status.json       (latest open/closed state)
                    │
   Open-Meteo ◀──── weather.py (poller)
                    │
                    └── extracteur.db (SQLite) ── events + weather snapshots
```

- `app.py` — Flask app, login, UI, toggle & bulk-control routes, `/history`
- `modbus_tcp.py` — Modbus-TCP client, relay pulsing + auto-release timer
- `config.py` — Building/window layout, relay IPs, weather thresholds
- `weather.py` — Open-Meteo poller; rain/wind close-advisory logic (no API key)
- `radar.py` — RainViewer radar nowcast; "rain near us now" detection (no key)
- `db.py` — SQLite: event history + weather/radar snapshots
- `status.json` — Latest window state, created on first write

Each window uses two relay channels (open / close). `send_window_command`
pulses the relevant channel for a caller-supplied drive time, then schedules an
automatic all-relays-off once it elapses so motors aren't left energised. Travel
is purely timed (no position feedback), so that duration sets how far the window
moves: closing overdrives past full travel to seat shut, while opening is full or
partial (`config.WINDOW_*_SECONDS`, derived from the measured full-travel time
unless pinned, and validated at startup) depending on wind and rain risk.

Each window has its *own* release timer that zeroes just its two coils after
exactly its drive time, so overlapping commands on one board (say a 30 s close
then a 10 s partial open on another window) can neither cut each other short
nor stretch a partial open into a full one. A board-wide all-relays-off
backstop is also armed at the furthest requested deadline, in case a window
release fails. A command that fails after a coil write may have gone out arms
a short failsafe release instead, so a contactor can't stay energised on a
command the app reported as failed.

## History & weather

Every open/close — manual *or* the all-closed reset on startup — is recorded
in SQLite (`events` table) with a timestamp, the actor (user name, weather
trigger, or `startup`), a reason, success/failure, and a snapshot of the
weather conditions at that moment. Logged-in operators can browse it at
`/history` (timestamps shown in local `d/m/y` time).

The weather poller fetches the Open-Meteo forecast every `WEATHER_POLL_SECONDS`
(preferring the MeteoSwiss high-resolution models, which resolve Alpine
thunderstorms the global blend misses) and stores a snapshot each time — so the
model's risk calls can be checked against what actually happened. It produces
**two distinct signals**:

- **Close advisory** (actionable) — fires when **high wind *or* rain** is
  present: gusts ≥ `WIND_GUST_THRESHOLD_KMH`, **or** rain (radar-detected within
  `RADAR_RAIN_RADIUS_KM` of the residence, or forecast probability ≥
  `RAIN_PROB_THRESHOLD` / precipitation within the imminent
  `WEATHER_LOOKAHEAD_HOURS`). This is the only signal logged as an advisory event
  (once per episode) and the one a future notification would use.
- **Caution flag** (informational, may be frequent) — a "high-risk day, keep an
  eye out" heads-up from thunderstorm risk / instability (CAPE) / strong gusts
  over the longer `WEATHER_WATCH_HOURS`. It is **never** an auto-action: during a
  heatwave convection is near-daily, so triggering on it would keep the windows
  shut every day and defeat the night airing.

CAPE (convective fuel) is shown for context. Both signals appear on the home
page (green OK / amber caution / red close-advisory).

### Radar nowcast (`radar.py`)

Forecast models can't resolve a *single* cell already overhead — on 2026-06-29
even Open-Meteo's `minutely_15` showed 0 mm while a thunderstorm sat 5 km away.
Radar can. `radar.py` samples RainViewer's free global radar mosaic at the
residence (Web-Mercator tile → pixel, decoded with Pillow) and reports whether
there's an echo within `RADAR_RAIN_RADIUS_KM`, its distance/bearing, whether it's
approaching, and — when RainViewer publishes nowcast frames — a short ETA. It is
**observe-only**: shown on the home page (`🛰️ Radar : …`) and logged to
`radar_snapshots`, not wired into any close decision yet, so its reliability can
be judged first. RainViewer's *forecast* frames are intermittent (often absent),
but the "raining near us now" detection from the latest frame is dependable.

> The longer game is group thinking: give several residents access and nudge the
> group (via ntfy.sh — see **Notifications** below) so whoever is home closes —
> the way a shared parasol already gets managed.

## Notifications

Alerts are pushed via [ntfy.sh](https://ntfy.sh) (free, no account, self-hostable).
Residents subscribe by adding the topic in the ntfy app — there is no
server-side subscriber list to manage. The subscribe page at `/notifications`
(public, linked as **🔔 Alertes** in the header) shows a QR and the topic name;
unsubscribing is done in the app.

**What gets pushed** (priority in brackets):

| Event | When |
|---|---|
| Fermeture conseillée / automatique `[high]` | Close advisory rises (wind/rain/storm) |
| Météo à surveiller `[default]` | Caution flag rises (storm risk / instability) |
| Ouverture (simulation) `[default]` | Conditions become favourable to auto-open |
| Météo dégagée `[low]` | Everything clears again |
| Météo indisponible `[high/default]` | Forecast outage (escalates when the last state was touchy) |
| Module injoignable `[high]` | A relay command fails |
| Application redémarrée `[default]` | App start / startup reset |

Each weather push carries the external temperature (and the indoor temperature
once the Phase-2 sensor lands). Weather alerts are **episode-level** — one push
when a storm arrives, one when it passes, not one per window. Manual open/close
pushes are off by default (`NOTIFY_MANUAL_ACTIONS`).

**Decoupling.** The transport is a self-contained, project-agnostic package
(`notifier/` — a `Notifier` fanning out to pluggable `Channel`s: `NtfyChannel`,
`LogChannel`) with no app imports, so it drops into another project unchanged.
The Extracteur-specific part (event catalog, config wiring, message wording)
lives in `notify.py`; everything routes through `notify.send(event, title, msg)`.

**App-down.** A dead process can't announce its own death, so liveness is judged
from outside: the app pings `HEALTHCHECK_URL` (e.g. a [healthchecks.io](https://healthchecks.io)
check) every `HEALTHCHECK_INTERVAL_SECONDS` from a background thread; a missed
ping makes that monitor raise the alarm. Point the check's own alert at the same
ntfy topic and app-down lands in the same stream. The ping is outbound-only, so
it needs no inbound exposure. (`/healthz` is also served for a pull-based monitor
if you prefer one.)

## Network

The Pi runs on the **services** VLAN (2200, `10.132.0.0/16`) as `services.eco`
(`10.132.0.133`) — the same address it's reachable on from anywhere on the
estate. The relay modules live on a separate, **isolated IoT VLAN** (470,
`10.137.0.0/16`), routed by the Mikrotik with no NAT and firewalled so that
*only* the Pi can reach them — the relays themselves have no route to the
internet. The app talks to each relay over **Modbus/TCP on port 502**.

Building identifiers are the postal addresses (Chemin des Tines 1 / 3 / 5), which
map to the construction-code buildings and relay IPs as follows:

| Config key (`RELAY_IP_BUILDING_…`) | Address | Building | Relay IP | Status |
|---|---|---|---|---|
| `building_1` | Tines 1 | B | `10.137.0.10` | wired |
| `building_3` | Tines 3 | A | _tbd_ | planned |
| `building_5` | Tines 5 | C | _tbd_ | planned |

On every building's DGS-1210 switch, **port 15** is pre-configured as a VLAN 470
access port, so a relay board can be plugged in and reach the Pi with no further
switch work. List the buildings whose relay is actually wired in
`EQUIPPED_BUILDINGS` (e.g. `building_1`); the others show in the UI but disabled.

Full addressing, VLAN and switch-port details live in the network docs
(`shared-internet/Network/Layout/`). To administer the switches or a relay
directly, see **Administering the building network (VPN)** below.

## Configuration

Config comes from environment variables, split across **three files by
stability** so shared defaults propagate through git, secrets stay per-machine,
and day-to-day tuning needs no shell at all:

- **`defaults.env`** — *committed.* Every non-secret tunable (weather thresholds,
  radar, ntfy enable/server, poll intervals, timezone…) at its production
  default. Add a new setting here, commit, `git pull` on the Pi → it reaches
  every deployment with **no per-machine editing**.
- **`extracteur.env`** — *gitignored.* Only the 7 required secrets
  (`FLASK_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`,
  `RELAY_IP_BUILDING_1/3/5`, `EQUIPPED_BUILDINGS`) plus per-machine notification
  values (`NTFY_TOPIC`, `NTFY_TOKEN`, `PUBLIC_BASE_URL`, `HEALTHCHECK_URL`).
  Copy it from `extracteur.env.example` and fill it in. It's small and rarely
  changes.
- **`settings.env`** — *app-managed* (`SETTINGS_ENV_FILE`, `/app/data/settings.env`
  in Docker so it sits on the bind mount). Written by the **Réglages** page
  (`/config`), where a logged-in admin can tune drive durations, wind/rain/CAPE
  thresholds, the night-airing grace and the stale-weather delays. Saved values
  apply immediately (no restart — the app reloads its config in place) and take
  precedence over both files above; an empty field falls back to the
  environment / derived value shown in grey. Delete a line (or the file) to
  hand the key back to the environment.

Compose layers the first two (`defaults.env` first, then `extracteur.env`
overriding), and every non-secret key also has a matching default in code — so
a machine missing `defaults.env` still runs, and only the 7 required secrets
are truly mandatory (the app fails fast with a clear message listing any that
are absent). The UI-tunable subset and its bounds live in `settings_env.py`.

> `extracteur.env` is intentionally **not** named `.env`: Docker Compose
> auto-loads `.env` for YAML interpolation, which mangles werkzeug password
> hashes (they contain `$`). Using a different name avoids that.

| Variable | Used by | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | `app.py` | Flask session signing key |
| `ADMIN_USERNAME`   | `app.py` | Web UI login |
| `ADMIN_PASSWORD_HASH` | `app.py` | Web UI password, as a werkzeug hash |
| `WEATHER_LAT` / `WEATHER_LON` | `weather.py` | Location for the Open-Meteo forecast (optional; defaults to the residence) |
| `WEATHER_POLL_SECONDS` | `weather.py` | Forecast poll interval (default 600) |
| `WEATHER_MODELS` | `weather.py` | Open-Meteo models tried in order (default `meteoswiss_icon_ch1,meteoswiss_icon_ch2`, then best-match) |
| `WEATHER_LOOKAHEAD_HOURS` | `weather.py` | Imminent window for rain/wind "close now" (default 3) |
| `WEATHER_WATCH_HOURS` | `weather.py` | Longer thunderstorm-watch window (default 12) |
| `RAIN_PROB_THRESHOLD` | `weather.py` | Rain-probability % that advises closing (default 60) |
| `WIND_GUST_THRESHOLD_KMH` | `weather.py` | Wind-gust km/h that advises closing (default 40) |
| `CAPE_THRESHOLD` | `weather.py` | CAPE J/kg above which instability is flagged for context (default 1000) |
| `RADAR_ENABLED` | `radar.py` | Toggle the RainViewer radar nowcast (default on; needs Pillow) |
| `RADAR_RAIN_RADIUS_KM` | `radar.py` | Echo within this radius counts as "rain near us" (default 10) |
| `RADAR_POLL_SECONDS` / `RADAR_ZOOM` | `radar.py` | Radar poll interval / tile zoom (defaults 300 / 7 — do **not** raise `RADAR_ZOOM` above 7; the mosaic returns a placeholder that reads as fake rain) |
| `DISPLAY_TZ` | `app.py` | Timezone for displayed timestamps (default `Europe/Paris`) |
| `DB_FILE` | `db.py` | SQLite path (Docker sets `/app/data/extracteur.db`) |
| `RELAY_IP_BUILDING_1` / `_3` / `_5` | `config.py` | IP of each Modbus-TCP relay module |
| `EQUIPPED_BUILDINGS` | `config.py` | Comma-separated list of wired buildings (e.g. `building_1`) |
| `NTFY_ENABLED` | `notify.py` | Toggle ntfy push (default on; log-only when off or no topic) |
| `NTFY_SERVER` / `NTFY_TOPIC` | `notify.py` | ntfy server (default `https://ntfy.sh`) and topic — pick an **unguessable** topic |
| `NTFY_TOKEN` | `notify.py` | Optional bearer token for a reserved/self-hosted topic |
| `PUBLIC_BASE_URL` | `notify.py` | Dashboard URL used as the notification tap target (optional) |
| `NOTIFY_MANUAL_ACTIONS` | `app.py` | Also push (low-priority) on manual open/close (default off) |
| `HEALTHCHECK_URL` | `app.py` | Heartbeat ping URL for an app-down monitor (empty = disabled) |
| `HEALTHCHECK_INTERVAL_SECONDS` | `app.py` | Heartbeat interval (default 300) |

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

**First-time setup** on the Pi (currently deployed at `~/stairwell-window-control`):

```bash
git clone <repo-url> ~/stairwell-window-control
cd ~/stairwell-window-control

# defaults.env is committed (shared non-secret config). You only create the
# secrets file — the 7 required vars + any per-machine notification values:
cp extracteur.env.example extracteur.env
chmod 600 extracteur.env
$EDITOR extracteur.env

# First start
docker compose up -d --build
```

**Updates** (from your laptop, push; on the Pi):

```bash
cd ~/stairwell-window-control
git pull        # brings any new defaults.env settings automatically
docker compose up -d --build
```

Because non-secret settings live in the committed `defaults.env` (and in code),
a `git pull` propagates new config to the Pi with no env editing — you only touch
`extracteur.env` when a genuinely new **secret** appears.

### About the env files

Secrets are **not** baked into the image. `extracteur.env` is `.gitignore`d and
`.dockerignore`d, and is read by Docker at container start via `env_file:` in the
compose file (layered after the committed `defaults.env`). Keep it next to
`docker-compose.yml` with mode `600`. To rotate a credential, edit it and run
`docker compose up -d` — no rebuild needed. If it's missing entirely, the
container still starts (Compose `required: false`) and exits with a clear list of
the required vars to set.

A `data/` directory next to `docker-compose.yml` is bind-mounted to
`/app/data` so `status.json` (window state) and `extracteur.db` (history +
weather snapshots) survive container restarts. The directory is created
automatically on first start.

Then open `http://<pi-address>:5000`.

## Administering the building network (VPN)

The switches and relay modules live on the building's internal management / IoT
subnets, not on the public internet. To reach them — e.g. to configure the relay
VLAN or a switch — bring up the technical WireGuard tunnel first:

```bash
sudo wg-quick up /home/cedric/wg-technical.conf    # connect
sudo wg-quick down /home/cedric/wg-technical.conf  # disconnect when done
```

This routes the management network `10.134.0.0/16`, so the L3 switch
(`http://10.134.0.1`) and the per-building DGS-1210 switches
(`10.134.0.111` / `.131` / `.151`) become reachable. The relay modules sit on the
isolated IoT VLAN (`10.137.0.0/16`, VID 470), routed by the Mikrotik and reachable
from the services Pi rather than directly over this tunnel.

## Running locally

```bash
pip install -r requirements.txt
export $(grep -vhE '^\s*#|^\s*$' defaults.env extracteur.env | xargs)
python app.py
```

## Notes

- The app listens on `0.0.0.0:5000` in debug mode. For production, put it
  behind a reverse proxy and disable `debug=True`.
- `status.json` reflects the last command sent, not the real physical state
  of the windows (no feedback contacts wired yet).
