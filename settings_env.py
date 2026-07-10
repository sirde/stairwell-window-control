"""UI-editable runtime settings, persisted to an env-format overrides file.

The settings page (/config) rewrites SETTINGS_ENV_FILE (default ./settings.env;
/app/data/settings.env in Docker so it lives on the bind mount) on every save.
Resolution order for the keys below: this file > process environment
(defaults.env / extracteur.env via compose) > derived / code default.
config.py resolves through effective_values() at import; app.py saves the file
then reloads config in place, so changes apply without a restart.

Kept import-light on purpose (os / tempfile / logging only): config.py imports
this module at startup, so it must not import config or anything that does.
"""
import logging
import os
import tempfile

log = logging.getLogger(__name__)


def path() -> str:
    return os.environ.get("SETTINGS_ENV_FILE", "settings.env")


# Schema of the tunables exposed on the /config page. `label`/`help` are
# user-facing (French, like the rest of the UI); `min`/`max` bound both the
# form inputs and server-side validation. Derivable fields (close / partial
# follow full travel) resolve via DERIVED when neither the UI file nor the
# environment pins them.
FIELDS = [
    dict(key="WINDOW_FULL_TRAVEL_SECONDS", group="Course des fenêtres",
         label="Course complète", unit="s", type=float, default=20.0,
         min=1, max=120, step=0.5,
         help="Temps moteur mesuré de « fermé » à « grand ouvert ». La fermeture "
              "et l'ouverture partielle en découlent si elles ne sont pas fixées."),
    dict(key="WINDOW_CLOSE_SECONDS", group="Course des fenêtres",
         label="Fermeture (surcourse)", unit="s", type=float, default=None,
         min=1, max=120, step=0.5,
         help="Dépasse la course complète pour plaquer la fenêtre au cadre — le "
              "moteur cale sans danger, comme avec l'interrupteur manuel. "
              "Dérivé : course + 10 s."),
    dict(key="WINDOW_PARTIAL_OPEN_SECONDS", group="Course des fenêtres",
         label="Ouverture partielle", unit="s", type=float, default=None,
         min=0.5, max=120, step=0.5,
         help="Entrebâillement utilisé quand il y a du vent ou un risque de "
              "pluie. Dérivé : moitié de la course."),

    dict(key="WIND_GUST_THRESHOLD_KMH", group="Seuils météo",
         label="Rafales — fermeture", unit="km/h", type=float, default=40.0,
         min=10, max=150, step=1,
         help="Au-delà, le système ferme les fenêtres (ou le conseille si "
              "la fermeture automatique est désactivée)."),
    dict(key="WIND_FULL_OPEN_MAX_KMH", group="Seuils météo",
         label="Rafales — ouverture complète", unit="km/h", type=float, default=20.0,
         min=0, max=150, step=1,
         help="En dessous (et au sec) : ouverture complète ; entre les deux "
              "seuils de rafales : ouverture partielle."),
    dict(key="RAIN_PROB_THRESHOLD", group="Seuils météo",
         label="Probabilité de pluie", unit="%", type=int, default=60,
         min=0, max=100, step=5,
         help="La prévision compte comme « pluie » au-delà de cette probabilité."),
    dict(key="CAPE_THRESHOLD", group="Seuils météo",
         label="Instabilité (CAPE)", unit="J/kg", type=float, default=1000.0,
         min=0, max=10000, step=100,
         help="Vigilance orage au-delà de cette énergie convective."),

    dict(key="MORNING_VENT_GRACE_HOURS", group="Aération nocturne",
         label="Grâce après le lever du soleil", unit="h", type=float, default=2.0,
         min=0, max=12, step=0.5,
         help="L'ouverture automatique reste permise pendant ces heures après "
              "le lever (toit orienté sud : l'air le plus frais suit l'aube). "
              "Passé ce délai, l'aération se referme automatiquement."),
    dict(key="AUTO_CLOSE_TEMP_MARGIN_C", group="Aération nocturne",
         label="Marge de fermeture (chaleur)", unit="°C", type=float, default=2.0,
         min=0, max=15, step=0.5,
         help="Referme aussi si la température extérieure dépasse le seuil "
              "d'ouverture de cette marge. L'écart évite les allers-retours "
              "d'ouverture / fermeture autour du seuil."),

    dict(key="WEATHER_LOOKAHEAD_HOURS", group="Horizons de prévision",
         label="Fenêtre imminente", unit="h", type=int, default=3,
         min=1, max=24, step=1,
         help="Heures de prévision qui pèsent sur la décision de fermeture."),
    dict(key="WEATHER_WATCH_HOURS", group="Horizons de prévision",
         label="Fenêtre de vigilance", unit="h", type=int, default=12,
         min=1, max=48, step=1,
         help="Heures scrutées pour le risque d'orage (les orages se voient "
              "des heures à l'avance)."),

    dict(key="WEATHER_STALE_RISKY_SECONDS", group="Météo indisponible",
         label="Délai avant alerte (dernier relevé agité)", unit="s", type=int,
         default=600, min=60, max=86400, step=60,
         help="Panne de prévision alors que le dernier relevé était venteux / "
              "pluvieux / orageux : délai court avant la fermeture préventive."),
    dict(key="WEATHER_STALE_CALM_SECONDS", group="Météo indisponible",
         label="Délai avant alerte (dernier relevé calme)", unit="s", type=int,
         default=5400, min=60, max=86400, step=60,
         help="Dernier relevé calme : la panne est tolérée plus longtemps."),
]

FIELD_BY_KEY = {f["key"]: f for f in FIELDS}

DERIVED = {
    "WINDOW_CLOSE_SECONDS":
        lambda v: v["WINDOW_FULL_TRAVEL_SECONDS"] + 10,
    "WINDOW_PARTIAL_OPEN_SECONDS":
        lambda v: v["WINDOW_FULL_TRAVEL_SECONDS"] / 2,
}


def load_overrides() -> dict[str, str]:
    """Raw KEY=value pairs from the UI overrides file (missing file = none)."""
    out: dict[str, str] = {}
    try:
        with open(path()) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("Cannot read %s — ignoring UI overrides", path())
    return out


def save_overrides(overrides: dict[str, str]) -> None:
    """Atomically rewrite the overrides file (temp file + rename)."""
    target = os.path.abspath(path())
    directory = os.path.dirname(target) or "."
    lines = [
        "# settings.env — réglages enregistrés depuis la page « Réglages ».",
        "# Fichier géré par l'application (réécrit à chaque enregistrement).",
        "# Ces clés priment sur defaults.env / extracteur.env ; supprimer une",
        "# ligne (ou ce fichier) redonne la main à l'environnement / défauts.",
    ] + [f"{key}={overrides[key]}" for key in sorted(overrides)]
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_field(field: dict, raw: str) -> float | int | None:
    """Typed value, or None when unparseable / out of the field's range."""
    try:
        value = field["type"](raw)
    except (TypeError, ValueError):
        return None
    if not field["min"] <= value <= field["max"]:
        return None
    return value


def effective_values(overrides: dict[str, str]) -> dict:
    """Resolve every field: UI override > environment > derived / default.

    A bad UI override is dropped with a warning — the file is app-managed and
    self-heals on the next save, and a boot loop on the Pi would be worse. A
    bad *environment* value still aborts startup loudly: a silently-accepted
    0-second drive would "succeed" every command without moving a motor.
    """
    vals: dict = {}
    for f in FIELDS:
        key = f["key"]
        raw = overrides.get(key)
        if raw not in (None, ""):
            value = parse_field(f, raw)
            if value is not None:
                vals[key] = value
                continue
            log.warning("%s=%r in %s is invalid — ignoring the UI override",
                        key, raw, path())
        raw = os.environ.get(key)
        if raw not in (None, ""):
            value = parse_field(f, raw)
            if value is None:
                raise SystemExit(
                    f"{key}={raw!r} : must be a {f['type'].__name__} in "
                    f"[{f['min']:g}, {f['max']:g}] ({f['unit']})")
            vals[key] = value
            continue
        vals[key] = None
    # Derived values and code defaults; FIELDS order puts full travel first.
    for f in FIELDS:
        if vals[f["key"]] is None:
            derive = DERIVED.get(f["key"])
            vals[f["key"]] = derive(vals) if derive else f["default"]
    return vals


def cross_errors(vals: dict) -> list[str]:
    """Consistency checks across fields, on the EFFECTIVE (resolved) values.

    Enforced when saving from the UI; boot stays lenient so a hand-edited
    combination can't brick the app.
    """
    errors = []
    if vals["WINDOW_CLOSE_SECONDS"] < vals["WINDOW_FULL_TRAVEL_SECONDS"]:
        errors.append("La fermeture doit durer au moins la course complète "
                      "(surcourse pour plaquer la fenêtre au cadre).")
    if vals["WINDOW_PARTIAL_OPEN_SECONDS"] > vals["WINDOW_FULL_TRAVEL_SECONDS"]:
        errors.append("L'ouverture partielle ne peut pas dépasser la course complète.")
    if vals["WIND_FULL_OPEN_MAX_KMH"] > vals["WIND_GUST_THRESHOLD_KMH"]:
        errors.append("Le seuil d'ouverture complète doit rester sous le seuil "
                      "de fermeture (rafales).")
    if vals["WEATHER_LOOKAHEAD_HOURS"] > vals["WEATHER_WATCH_HOURS"]:
        errors.append("La fenêtre imminente ne peut pas dépasser la fenêtre "
                      "de vigilance.")
    if vals["WEATHER_STALE_RISKY_SECONDS"] > vals["WEATHER_STALE_CALM_SECONDS"]:
        errors.append("Le délai « agité » doit rester inférieur ou égal au "
                      "délai « calme ».")
    return errors
