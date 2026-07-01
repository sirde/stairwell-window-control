"""Open-Meteo weather poller and rain/wind close-advisory logic.

Open-Meteo is a free, keyless forecast API. We pull the current temperature
plus the next few hours of precipitation probability and wind gusts, and
decide whether open windows ought to be closed for protection.

Phase 1 is advisory only: decisions are written to the history (so thresholds
can be tuned against reality) but no Modbus command is ever sent. The hook for
Phase 2 is `evaluate_and_log` — that's where actuation would slot in once a
relay is deployed and trusted.
"""
import logging
import threading
from datetime import datetime, timezone

import requests

import config
import db
import notify
import radar

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Latest summary for the UI; None until the first successful poll.
_latest: dict | None = None
_latest_lock = threading.Lock()

# Open windows we've already logged a close-advisory for, so the advisory is
# recorded once per weather episode (rising edge) rather than on every poll.
_advised: set[tuple[str, str]] = set()
_advised_lock = threading.Lock()

# Closed windows we've already logged a *simulated* auto-open for, so the
# simulated open is recorded once per favourable episode (rising edge) too.
_auto_opened: set[tuple[str, str]] = set()
_auto_opened_lock = threading.Lock()

# Notification edges — episode-level, distinct from the per-window history
# dedupe above: a storm tripping five windows is ONE push, not five. Guarded by
# one lock so the forecast and radar pollers can't double-send.
#   _notified_close : a close advisory / auto-close push is outstanding.
#   _notified_open  : a favourable-to-open push is outstanding.
#   _weather_alerted: any weather alert (close or watch) is outstanding; gates
#                     the single "all clear" push so it fires once, at the end.
_notified_close = False
_notified_open = False
_weather_alerted = False
_notify_lock = threading.Lock()

# Graceful degradation: when the forecast can't be fetched we act on the last
# known state. _degraded is the rising-edge flag so the precautionary close +
# notification fire once per outage (and once on recovery); _started_at lets us
# measure "no data since startup" when no poll has ever succeeded.
_degraded = False
_degraded_lock = threading.Lock()
_started_at: str | None = None

# French labels for the trigger tokens that make up an advisory's actor.
TRIGGER_FR = {"rain": "pluie", "wind": "vent", "storm": "orage"}

# WMO weather codes that mean a storm: thunderstorm (+hail) and violent showers.
STORM_CODES = {82, 95, 96, 99}


def latest() -> dict | None:
    with _latest_lock:
        return dict(_latest) if _latest else None


def _set_latest(summary: dict) -> None:
    global _latest
    with _latest_lock:
        _latest = summary


def _request(model: str) -> dict:
    params = {
        "latitude": config.WEATHER_LAT,
        "longitude": config.WEATHER_LON,
        "current": "temperature_2m,weathercode,precipitation,wind_gusts_10m",
        "hourly": "precipitation_probability,precipitation,wind_gusts_10m,temperature_2m,weathercode,cape",
        "wind_speed_unit": "kmh",
        "timezone": "auto",       # hourly times come back in local time for display
        "forecast_days": 2,
    }
    if model:
        params["models"] = model
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_forecast() -> dict:
    """Fetch the forecast and reduce it to a decision-ready summary.

    Tries the preferred models in order (MeteoSwiss CH1/CH2), falling back to
    Open-Meteo's default blend, so a model with no data for the location can't
    blind the rules.
    """
    last_err: Exception | None = None
    for model in config.WEATHER_MODELS + [""]:
        try:
            summary = _summarise(_request(model), model or "best_match")
        except requests.RequestException as e:
            last_err = e
            continue
        if summary["temp_c"] is not None:
            return summary
    if last_err is not None:
        raise last_err
    raise RuntimeError("No weather model returned usable data")


def _storm_eta(times: list, codes: list, start: int, end: int, now: str) -> str | None:
    """Local 'HH:MM' (prefixed 'demain' for tomorrow) of the first storm hour."""
    for i in range(start, min(end, len(times))):
        if codes[i] in STORM_CODES:
            t = times[i]
            prefix = "demain " if t[:10] != (now or "")[:10] else ""
            return prefix + t[11:16]
    return None


def _summarise(data: dict, model: str) -> dict:
    """Reduce a raw Open-Meteo response into two separate signals.

    - close_advisory: actionable, rare — high wind AND rain together (wind-driven
      rain getting in). This is the only thing that logs an advisory / will nudge.
    - caution: informational, can be frequent — thunderstorm risk / instability /
      strong gusts. A "keep an eye out today" flag, never an auto-action. During a
      heatwave convection is near-daily, so this must NOT drive closing.
    """
    current = data.get("current", {})
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    probs = hourly.get("precipitation_probability", [])
    precs = hourly.get("precipitation", [])
    gusts = hourly.get("wind_gusts_10m", [])
    codes = hourly.get("weathercode", [])
    capes = hourly.get("cape", [])

    now = current.get("time")
    start = next((i for i, t in enumerate(times) if now and t >= now), 0)
    imminent = slice(start, start + config.WEATHER_LOOKAHEAD_HOURS)   # close-now
    watch_end = start + config.WEATHER_WATCH_HOURS                    # risk watch

    window_probs = [p for p in probs[imminent] if p is not None]
    window_precs = [p for p in precs[imminent] if p is not None]
    window_gusts = [g for g in gusts[imminent] if g is not None]
    window_capes = [c for c in capes[start:watch_end] if c is not None]

    rain_prob = max(window_probs) if window_probs else 0
    precip_mm = round(sum(window_precs), 1)
    wind_gust = max(window_gusts) if window_gusts else (current.get("wind_gusts_10m") or 0)
    cape_max = round(max(window_capes)) if window_capes else 0
    temp_c = current.get("temperature_2m")
    storm_now = current.get("weathercode") in STORM_CODES
    storm_eta = _storm_eta(times, codes, start, watch_end, now)

    # --- Forecast inputs to the close decision (combined with radar elsewhere) ---
    high_wind = wind_gust >= config.WIND_GUST_THRESHOLD_KMH
    forecast_rain = precip_mm > 0 or rain_prob >= config.RAIN_PROB_THRESHOLD
    forecast_rain_detail = ("pluie en cours" if precip_mm > 0
                            else f"pluie probable {rain_prob}%")

    # --- Caution: high-risk day, informational only (may be frequent) ---
    caution_reasons = []
    if storm_now:
        caution_reasons.append("orage en cours")
    elif storm_eta:
        caution_reasons.append(f"orage possible vers {storm_eta}")
    if cape_max >= config.CAPE_THRESHOLD:
        caution_reasons.append(f"forte instabilité (CAPE {cape_max})")
    if high_wind:
        caution_reasons.append(f"rafales {round(wind_gust)} km/h")

    return {
        "temp_c": temp_c,
        "wind_gust_kmh": wind_gust,
        "rain_prob": rain_prob,
        "precip_mm": precip_mm,
        "cape_max": cape_max,
        "cape_high": cape_max >= config.CAPE_THRESHOLD,
        "storm_now": storm_now,
        "storm_eta": storm_eta,
        "high_wind": high_wind,
        "forecast_rain": forecast_rain,
        "forecast_rain_detail": forecast_rain_detail,
        "caution": bool(caution_reasons),
        "caution_reasons": caution_reasons,
        "model": model,
        "lookahead_hours": config.WEATHER_LOOKAHEAD_HOURS,
        "watch_hours": config.WEATHER_WATCH_HOURS,
        "fetched_at": db.iso_utc_now(),
    }


def close_decision(wsum: dict, rsum: dict | None) -> dict:
    """Decide the close advisory: high wind OR rain — either alone is enough.

    Radar-detected rain near the residence is ground truth and takes precedence
    over the forecast probability for the rain signal.
    """
    high_wind = bool(wsum.get("high_wind"))
    radar_rain = bool(rsum and rsum.get("rain_near"))
    forecast_rain = bool(wsum.get("forecast_rain"))
    rain = radar_rain or forecast_rain

    if radar_rain:
        nk = rsum.get("nearest_km")
        if nk is not None and nk < 1.5:
            rain_detail = "pluie sur la résidence"
        else:
            rain_detail = f"pluie au radar à {nk} km {rsum.get('direction')}"
            if rsum.get("approaching"):
                rain_detail += " (se rapproche)"
    elif forecast_rain:
        rain_detail = wsum.get("forecast_rain_detail", "pluie probable")
    else:
        rain_detail = None

    advise_close = high_wind or rain
    triggers: list[str] = []
    parts: list[str] = []
    if high_wind:
        triggers.append("wind")
        parts.append(f"vent fort {round(wsum['wind_gust_kmh'])} km/h")
    if rain:
        triggers.append("rain")
        parts.append(rain_detail or "pluie")
    reasons = []
    if parts:
        joined = " + ".join(parts)
        reasons = [joined[0].upper() + joined[1:]]

    return {
        "advise_close": advise_close,
        "triggers": triggers,
        "reasons": reasons,
        "rain": rain,
        "rain_detail": rain_detail,
        "rain_source": "radar" if radar_rain else ("forecast" if forecast_rain else None),
    }


def current() -> dict | None:
    """Latest forecast summary merged with the combined close decision (for UI)."""
    wsum = latest()
    if wsum is None:
        return None
    merged = {**wsum, **close_decision(wsum, radar.latest())}
    age = _age_seconds(wsum.get("fetched_at"))
    merged["age_seconds"] = round(age) if age is not None else None
    with _degraded_lock:
        merged["stale"] = _degraded
    return merged


def evaluate_close(status_provider) -> None:
    """Rising-edge log a close when wind or rain fires the close advisory.

    Two modes, chosen by the "auto-close" setting:
      - OFF (group nudge): advise closing the windows we believe are open.
      - ON (auto): drive *every* equipped window shut, regardless of believed
        state — closing is idempotent (drives to the stop), so a wrong belief
        can't leave one open in a storm. Dry-run in Phase 1: logged, not driven.

    Called by both pollers (forecast and radar), so a radar update arriving
    between forecast polls still reacts promptly. Logged once per episode.
    """
    wsum = latest()
    if wsum is None:
        return
    rsum = radar.latest()
    decision = close_decision(wsum, rsum)
    auto_close = db.get_automation()["close_enabled"]
    status = status_provider()
    all_equipped = {
        (b, w) for b in config.EQUIPPED_BUILDINGS for w in status.get(b, {})
    }
    open_equipped = {(b, w) for (b, w) in all_equipped if status.get(b, {}).get(w)}

    with _advised_lock:
        if decision["advise_close"]:
            if auto_close:
                targets = all_equipped
                source = "auto"
                reason = "Simulation — fermeture auto — " + " ; ".join(decision["reasons"])
            else:
                targets = open_equipped
                source = "advisory"
                reason = "Fermeture conseillée — " + " ; ".join(decision["reasons"])
            actor = "+".join(decision["triggers"])
            conditions = {**wsum, **decision, "radar": rsum}
            for building, window in targets - _advised:
                db.record_event(
                    "close", building=building, window=window,
                    source=source, actor=actor, reason=reason,
                    success=None, conditions=conditions,
                )
                log.info("%s close logged for %s/%s: %s",
                         "Auto" if auto_close else "Advisory", building, window, reason)
            _advised.update(targets)
            _advised.intersection_update(targets)
        else:
            _advised.clear()


def evaluate_open(status_provider) -> None:
    """Rising-edge log a *simulated* auto-open when it's cool, calm and dry.

    Phase 1 is dry-run: no Modbus command is ever sent. When the user has
    enabled "auto-open below X°C" (a UI toggle), each poll where conditions are
    favourable — wind OK, no rain (radar or forecast), and the outside
    temperature under the chosen threshold — logs an 'open' event per equipped
    window so the history shows when the windows *would* have opened. Logged
    once per favourable episode (rising edge), mirroring the close advisory, so
    it doesn't flood the history on every poll.
    """
    settings = db.get_automation()
    wsum = latest()
    if wsum is None:
        return
    temp = wsum.get("temp_c")
    threshold = settings["open_temp_c"]
    rsum = radar.latest()
    decision = close_decision(wsum, rsum)

    # Favourable = enabled AND cool enough AND nothing that would advise closing
    # (advise_close already means high wind OR rain — so its negation is
    # "wind OK and no rain").
    favourable = (
        settings["open_enabled"]
        and temp is not None
        and temp < threshold
        and not decision["advise_close"]
    )

    status = status_provider()
    closed_equipped = {
        (b, w)
        for b in config.EQUIPPED_BUILDINGS
        for w, is_open in status.get(b, {}).items()
        if not is_open
    }

    with _auto_opened_lock:
        if favourable:
            reason = (f"Simulation — ouverture auto : {round(temp)} °C "
                      f"(seuil {threshold} °C), sans vent ni pluie")
            conditions = {**wsum, **decision, "radar": rsum,
                          "simulated": True, "auto_open_threshold_c": threshold}
            for building, window in closed_equipped - _auto_opened:
                db.record_event(
                    "open", building=building, window=window,
                    source="auto", actor="cooling", reason=reason,
                    success=None, conditions=conditions,
                )
                log.info("Simulated auto-open logged for %s/%s: %s",
                         building, window, reason)
            _auto_opened.update(closed_equipped)
            _auto_opened.intersection_update(closed_equipped)
        else:
            _auto_opened.clear()


def _msg(body: str, temps: str) -> str:
    """Append the temperature suffix to a notification body, if known."""
    return f"{body}. {temps}." if temps else f"{body}."


def notify_weather() -> None:
    """Push a notification on each rising/falling edge of the weather signals.

    Independent of the dry-run history logging in evaluate_close/open: here we
    care about *episodes*, not per-window state, so people get one push when a
    storm arrives and one when it passes. Decisions (which flags to flip, what
    to send) are made under the lock; the actual sends happen after releasing it
    so a slow ntfy POST can't block the other poller.

      close advisory rising  -> "Fermeture conseillée" / "Fermeture automatique"
      caution rising (first)  -> "Météo à surveiller"
      favourable-to-open      -> "Ouverture (simulation)"
      everything clears       -> "Météo dégagée" (once)

    Caution and the all-clear leave no window event of their own (they aren't
    window actions), so unlike the close/open pushes — which evaluate_close/open
    already log — we record them here as `system` weather events, so every push
    has a matching row in /history.
    """
    global _notified_close, _notified_open, _weather_alerted
    wsum = latest()
    if wsum is None:
        return
    rsum = radar.latest()
    decision = close_decision(wsum, rsum)
    settings = db.get_automation()
    advise = decision["advise_close"]
    caution = bool(wsum.get("caution"))
    temp = wsum.get("temp_c")
    threshold = settings["open_temp_c"]
    temps = notify.format_temps(wsum)

    pending: list[tuple[str, str, str]] = []
    logs: list[tuple[str, str]] = []     # (action, reason) weather events to record
    with _notify_lock:
        if advise and not _notified_close:
            reason = "; ".join(decision["reasons"]) or "vent fort ou pluie"
            if settings["close_enabled"]:
                pending.append(("auto_closed", "Fermeture automatique",
                                _msg(f"Fermeture des fenêtres — {reason}", temps)))
            else:
                pending.append(("close_advised", "Fermeture conseillée",
                                _msg(f"Pensez à fermer les fenêtres — {reason}", temps)))
            _notified_close = True
            _weather_alerted = True
        elif not advise:
            _notified_close = False

        # Caution is the softer "keep an eye out" signal; only push it as a
        # standalone first alert — once a close push has gone out (_weather_alerted)
        # a second "à surveiller" would just be noise.
        if caution and not advise and not _weather_alerted:
            reasons = "; ".join(wsum.get("caution_reasons") or []) or "risque météo"
            pending.append(("weather_caution", "Météo à surveiller",
                            _msg(f"À surveiller — {reasons}", temps)))
            logs.append(("caution", f"Vigilance météo — {reasons}"))
            _weather_alerted = True

        # Mirror evaluate_open's favourable test (cool, calm, dry, enabled).
        favourable = (settings["open_enabled"] and temp is not None
                      and temp < threshold and not advise)
        if favourable and not _notified_open:
            pending.append(("auto_opened", "Ouverture (simulation)",
                            _msg(f"Conditions favorables — sans vent ni pluie, "
                                 f"sous {threshold} °C", temps)))
            _notified_open = True
        elif not favourable:
            _notified_open = False

        if not advise and not caution and _weather_alerted:
            pending.append(("weather_clear", "Météo dégagée",
                            _msg("Tout est au beau fixe — ni vent fort, ni pluie, "
                                 "ni orage", temps)))
            logs.append(("clear", "Retour au beau fixe — ni vent fort, ni pluie, ni orage"))
            _weather_alerted = False

    # Record the weather events first so the /history row exists by the time the
    # push lands; conditions=wsum gives the row its temp/rain/gust detail line.
    for action, reason in logs:
        db.record_event(action, source="system", actor="weather",
                        reason=reason, conditions=wsum)
    for event, title, message in pending:
        notify.send(event, title, message)


def evaluate(status_provider) -> None:
    """Run both dry-run evaluators (close advisory + auto-open simulation), then
    fire any edge notifications."""
    evaluate_close(status_provider)
    evaluate_open(status_provider)
    try:
        notify_weather()
    except Exception:
        log.exception("Weather notification error")


# --- Graceful degradation when the forecast can't be fetched -----------------

def _age_seconds(iso: str | None) -> float | None:
    """Seconds since the given ISO-UTC timestamp, or None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _is_touchy(wsum: dict | None, rsum: dict | None) -> bool:
    """Was the last known weather anything other than calm (no wind, no rain)?

    'Touchy' = the close advisory was up, or a caution flag (storm risk /
    instability / strong gusts) was set. Being blind during a touchy spell is
    the dangerous case, so it gets the short stale timeout; a calm last reading
    tolerates a much longer outage.
    """
    if wsum is None:
        return False
    decision = close_decision(wsum, rsum)
    return bool(decision["advise_close"] or wsum.get("caution")
                or wsum.get("cape_high") or wsum.get("storm_eta"))


def _describe_last_state(wsum: dict | None, rsum: dict | None) -> str:
    if wsum is None:
        return "aucune donnée depuis le démarrage"
    decision = close_decision(wsum, rsum)
    if decision["advise_close"]:
        return "; ".join(decision["reasons"]) or "fermeture conseillée"
    if wsum.get("caution"):
        return "vigilance — " + "; ".join(wsum.get("caution_reasons") or [])
    return "calme (ni vent ni pluie)"


def _enter_degraded(status_provider, wsum, rsum, age, touchy) -> None:
    """Log a dry-run precautionary close and fire the (placeholder) notify."""
    minutes = round(age / 60)
    last_state = _describe_last_state(wsum, rsum)
    auto_close = db.get_automation()["close_enabled"]
    status = status_provider()
    all_equipped = {
        (b, w) for b in config.EQUIPPED_BUILDINGS for w in status.get(b, {})
    }
    open_equipped = {(b, w) for (b, w) in all_equipped if status.get(b, {}).get(w)}

    if auto_close:
        targets, source = all_equipped, "auto"
        verb = "Simulation — fermeture préventive"
    else:
        targets, source = open_equipped, "advisory"
        verb = "Fermeture préventive conseillée"
    reason = (f"{verb} — météo indisponible depuis {minutes} min "
              f"(dernier état : {last_state})")
    conditions = {"stale": True, "stale_seconds": round(age), "touchy": touchy,
                  "last_summary": wsum, "radar": rsum}
    for building, window in targets:
        db.record_event("close", building=building, window=window,
                        source=source, actor="stale", reason=reason,
                        success=None, conditions=conditions)
    log.warning("Weather degraded (touchy=%s): %s", touchy, reason)
    notify.send(
        "weather_stale", "Météo indisponible", reason,
        priority="high" if touchy else "default",
        stale_minutes=minutes, touchy=touchy,
    )


def evaluate_staleness(status_provider) -> None:
    """Detect a forecast outage and react on the last known state.

    Runs every poll cycle (success or failure). When the most recent successful
    forecast is older than the applicable timeout — short if the last state was
    touchy, long if it was calm — enter a degraded state once: log a dry-run
    precautionary close and notify. A later successful poll clears it and
    notifies recovery. Dry-run in Phase 1: nothing is ever actuated.
    """
    global _degraded
    wsum = latest()
    rsum = radar.latest()
    if wsum is not None:
        age = _age_seconds(wsum.get("fetched_at"))
        touchy = _is_touchy(wsum, rsum)
    else:
        # No successful poll yet — measure from startup, treat as calm/unknown.
        age = _age_seconds(_started_at)
        touchy = False
    if age is None:
        return

    limit = (config.WEATHER_STALE_RISKY_SECONDS if touchy
             else config.WEATHER_STALE_CALM_SECONDS)

    with _degraded_lock:
        if age >= limit and not _degraded:
            _degraded = True
            _enter_degraded(status_provider, wsum, rsum, age, touchy)
        elif age < limit and _degraded:
            _degraded = False
            log.info("Weather recovered after %s s outage", round(age))
            notify.send(
                "weather_recovered", "Météo rétablie",
                "Les prévisions sont de nouveau disponibles.",
            )


def _poll_loop(status_provider, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            summary = fetch_forecast()
            _set_latest(summary)
            decision = close_decision(summary, radar.latest())
            db.record_weather(
                temp_c=summary["temp_c"],
                wind_gust_kmh=summary["wind_gust_kmh"],
                rain_prob=summary["rain_prob"],
                advise_close=decision["advise_close"],
                caution=summary["caution"],
                raw={**summary, **decision},
            )
            evaluate(status_provider)
            log.info("Weather[%s]: %s°C, rain %s%% / %smm, gust %s km/h, CAPE %s, "
                     "caution=%s, advise_close=%s (%s)",
                     summary["model"], summary["temp_c"], summary["rain_prob"],
                     summary["precip_mm"], round(summary["wind_gust_kmh"]),
                     summary["cape_max"], summary["caution"],
                     decision["advise_close"], decision["rain_source"])
        except requests.RequestException as e:
            log.warning("Weather fetch failed: %s", e)
        except Exception:
            log.exception("Weather poll error")
        # Always assess staleness — this is what fires the graceful-degradation
        # precautionary close + notification when fetches keep failing, and
        # clears it once a poll succeeds again.
        try:
            evaluate_staleness(status_provider)
        except Exception:
            log.exception("Weather staleness evaluation error")
        stop_event.wait(config.WEATHER_POLL_SECONDS)


def start_poller(status_provider) -> threading.Event:
    """Start the background poller. Returns the Event used to stop it."""
    global _started_at
    _started_at = db.iso_utc_now()
    stop_event = threading.Event()
    threading.Thread(
        target=_poll_loop, args=(status_provider, stop_event),
        name="weather-poller", daemon=True,
    ).start()
    log.info("Weather poller started (every %ss at %s,%s)",
             config.WEATHER_POLL_SECONDS, config.WEATHER_LAT, config.WEATHER_LON)
    return stop_event


if __name__ == "__main__":
    # Manual check: print one forecast summary for the configured location.
    logging.basicConfig(level="INFO")
    import json
    print(json.dumps(fetch_forecast(), indent=2, ensure_ascii=False))
