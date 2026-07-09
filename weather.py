"""Open-Meteo weather poller and rain/wind close-advisory logic.

Open-Meteo is a free, keyless forecast API. We pull the current temperature
plus the next few hours of precipitation probability and wind gusts, and
decide whether open windows ought to be closed for protection.

Live actuation is gated by the two automation toggles (db.get_automation):

  - auto-close ON  -> the close evaluators DRIVE every equipped window shut on a
    wind/rain (or forecast-outage) trigger, and record the real result. OFF ->
    advisory only: a "Fermeture conseillée" history row + push for the windows
    believed open, nothing driven.
  - auto-open ON   -> the open evaluator DRIVES cool/calm/dry night openings, but
    ONLY when auto-close is also ON (safety link — never open a window without
    its automatic close protection). Otherwise the open is simulated (logged,
    not driven).

So a toggle is the live switch: off keeps logging the trigger for calibration
without moving a motor; on drives for real.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

import config
import db
import modbus_tcp
import notify
import radar
import window_state

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Latest summary for the UI; None until the first successful poll.
_latest: dict | None = None
_latest_lock = threading.Lock()

# Open windows we've already logged a close-advisory for, so the advisory is
# recorded once per weather episode (rising edge) rather than on every poll.
_advised: set[tuple[str, str]] = set()
_advised_lock = threading.Lock()

# Closed windows we've already acted on for auto-open (driven or simulated), so
# the open is recorded once per favourable episode (rising edge) too.
_auto_opened: set[tuple[str, str]] = set()
_auto_opened_lock = threading.Lock()

# Notification edges — episode-level, distinct from the per-window history
# dedupe above: a storm tripping five windows is ONE push, not five. Guarded by
# one lock so the forecast and radar pollers can't double-send.
#   _notified_close : a close advisory / auto-close push is outstanding.
#   _notified_open  : a favourable-to-open push is outstanding.
#   _weather_alerted: any weather alert (close or watch) is outstanding; gates
#                     the single "all clear" push so it fires once, at the end.
#   _close_pushed   : the close push actually went out this episode. Separate
#                     from _notified_close because we only push once a window is
#                     actually open — if the advisory rises while everything is
#                     shut we latch the episode but hold the push until (and if)
#                     a window opens during it.
_notified_close = False
_notified_open = False
_weather_alerted = False
_close_pushed = False
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
        "current": "temperature_2m,weathercode,precipitation,wind_gusts_10m,is_day",
        "hourly": "precipitation_probability,precipitation,wind_gusts_10m,temperature_2m,weathercode,cape",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "kmh",
        "timezone": "auto",       # hourly/daily times come back in local time for display
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


def _daily_today(daily: dict, key: str, now: str | None) -> str | None:
    """Today's entry (same calendar date as `now`) from a daily series, or None."""
    if not now:
        return None
    return next((s for s in (daily.get(key) or []) if s and s[:10] == now[:10]), None)


def _venting_allowed(current: dict, sunrise: str | None) -> bool | None:
    """Is *now* inside the night-airing window (auto-open gate)?

    The window is the whole night plus a grace period past sunrise: a south-facing
    roof barely heats in the first hours after dawn (sun still low in the NE) and
    the coolest outdoor air is right after sunrise, so venting stays worthwhile
    for `MORNING_VENT_GRACE_HOURS` past it. The night side comes from Open-Meteo's
    `is_day` flag (0 = night); `sunrise` (today's, from _daily_today) only bolts
    the morning grace on top.

    Returns None when neither part can be evaluated, so the caller can fall
    back to the old temperature-only behaviour — and flag the gap — rather than
    freeze the feature.
    """
    now = current.get("time")
    is_day = current.get("is_day")
    try:
        now_dt = datetime.strptime(now, "%Y-%m-%dT%H:%M") if now else None
        sunrise_dt = datetime.strptime(sunrise, "%Y-%m-%dT%H:%M") if sunrise else None
    except (ValueError, TypeError):
        now_dt = sunrise_dt = None

    # Morning grace: keep venting up to grace hours past today's sunrise, even
    # though is_day has already flipped to 1.
    if now_dt is not None and sunrise_dt is not None:
        grace = timedelta(hours=config.MORNING_VENT_GRACE_HOURS)
        if sunrise_dt <= now_dt < sunrise_dt + grace:
            return True

    # Otherwise it's simply the night part — trust the is_day flag.
    return None if is_day is None else (is_day == 0)


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
    daily = data.get("daily", {})
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
    # The live observed gust counts alongside the forecast window: an
    # unforecast squall happening right now must be able to raise the signal.
    current_gust = current.get("wind_gusts_10m") or 0
    wind_gust = max([current_gust, *window_gusts])
    cape_max = round(max(window_capes)) if window_capes else 0
    temp_c = current.get("temperature_2m")
    storm_now = current.get("weathercode") in STORM_CODES
    storm_eta = _storm_eta(times, codes, start, watch_end, now)

    # Night-airing time gate for auto-open. Undeterminable (missing is_day /
    # sun times) collapses to True — the old temperature-only behaviour — but
    # is flagged as venting_unknown and logged, so a data gap can never
    # silently pass for "night".
    sunrise_today = _daily_today(daily, "sunrise", now)
    sunset_today = _daily_today(daily, "sunset", now)
    venting = _venting_allowed(current, sunrise_today)
    if venting is None:
        log.warning("Night-airing gate undeterminable (is_day / current time "
                    "missing) — failing open to temperature-only behaviour")

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
        "is_day": current.get("is_day"),
        "sunrise": sunrise_today,
        "sunset": sunset_today,
        "venting_ok": True if venting is None else venting,
        "venting_unknown": venting is None,
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


def open_plan(wsum: dict | None = None, rsum: dict | None = None) -> dict:
    """How far to open right now, and why.

    Fully open only when the forecast is fresh AND calm AND dry: gust below
    WIND_FULL_OPEN_MAX_KMH and no rain signal (radar or forecast, same source
    the close advisory uses). Anything breezier or any rain risk gets a partial
    ("cracked") open. Unknown or stale conditions (no forecast yet, or the
    poller degraded — see evaluate_staleness) fall back to partial — the
    cautious choice.

    Callers that already hold a summary (e.g. to record it with the event)
    should pass it in, so the depth decision and the recorded conditions come
    from the same snapshot.

    Returns {"full": bool, "reason": str | None}; reason is None when fully open.
    """
    if wsum is None:
        wsum = latest()
    if rsum is None:
        rsum = radar.latest()
    if wsum is None:
        return {"full": False, "reason": "conditions inconnues"}
    with _degraded_lock:
        if _degraded:
            return {"full": False, "reason": "météo indisponible"}
    gust = wsum.get("wind_gust_kmh") or 0
    rain = close_decision(wsum, rsum)["rain"]
    if gust < config.WIND_FULL_OPEN_MAX_KMH and not rain:
        return {"full": True, "reason": None}
    reason = "risque de pluie" if rain else f"vent {round(gust)} km/h"
    return {"full": False, "reason": reason}


def drive_command(action: str, wsum: dict | None) -> tuple[float, str, bool | str]:
    """(drive_seconds, reason_suffix, stored_state) for a window command.

    Close always overdrives to seat fully shut; open is full or partial depending
    on wind/rain (open_plan, fed the same summary that gets recorded with the
    event so the decision and the history conditions can't diverge). The suffix
    makes the depth visible in the history reason; the stored state
    (False / "full" / "partial") keeps it visible in the live UI. Shared by the
    manual routes (app.py) and the live auto evaluators below.
    """
    if action == "close":
        return config.WINDOW_CLOSE_SECONDS, "", False
    plan = open_plan(wsum)
    if plan["full"]:
        return config.WINDOW_FULL_TRAVEL_SECONDS, " — ouverture complète", "full"
    return (config.WINDOW_PARTIAL_OPEN_SECONDS,
            f" — ouverture partielle ({plan['reason']})", "partial")


def _drive_and_record(building: str, window: str, action: str, *, source: str,
                      actor: str, reason: str, conditions: dict, status: dict) -> bool:
    """Drive ONE window for a live auto action and record the event.

    Mutates `status` in place on success (the caller saves it once for the whole
    batch). The reason gets the depth suffix on success, or the failure message
    on error — a failed drive is recorded (success=0) but the belief state is not
    advanced. Returns True iff the drive succeeded. Never raises: a Modbus error
    surfaces as success=False, so one dead window can't abort the batch.
    """
    duration, suffix, new_status = drive_command(action, conditions)
    try:
        success, message = modbus_tcp.send_window_command(building, window, action, duration)
    except Exception as e:                       # send_window_command shouldn't raise, but be safe
        success, message = False, str(e)
    if success:
        status.setdefault(building, {})[window] = new_status
        full_reason = reason + suffix
    else:
        full_reason = f"{reason} — échec : {message}"
    db.record_event(action, building=building, window=window, source=source,
                    actor=actor, reason=full_reason, success=success,
                    conditions=conditions)
    log.info("Auto %s %s %s/%s: %s", action, "OK" if success else "FAILED",
             building, window, full_reason)
    return success


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
    """React to a wind/rain close trigger, once per episode (rising edge).

    Two modes, chosen by the "auto-close" toggle:
      - ON (live): DRIVE *every* equipped window shut, regardless of believed
        state — closing is idempotent (drives to the stop), so a wrong belief
        can't leave one open in a storm. The real result is recorded and the
        belief state persisted.
      - OFF (advisory): drive nothing; log a "Fermeture conseillée" row (and the
        push, in notify_weather) for the windows we believe are open, so a
        resident is nudged to close them by hand.

    Called by both pollers (forecast and radar), so a radar update arriving
    between forecast polls still reacts promptly. Acted on once per episode.
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
            actor = "+".join(decision["triggers"])
            reasons = " ; ".join(decision["reasons"])
            conditions = {**wsum, **decision, "radar": rsum}
            if auto_close:
                targets = all_equipped
                driven = False
                for building, window in targets - _advised:
                    if _drive_and_record(building, window, "close", source="auto",
                                         actor=actor,
                                         reason=f"Fermeture automatique — {reasons}",
                                         conditions=conditions, status=status):
                        driven = True
                if driven:
                    window_state.save_status(status)
            else:
                targets = open_equipped
                reason = "Fermeture conseillée — " + reasons
                for building, window in targets - _advised:
                    db.record_event(
                        "close", building=building, window=window,
                        source="advisory", actor=actor, reason=reason,
                        success=None, conditions=conditions,
                    )
                    log.info("Advisory close logged for %s/%s: %s",
                             building, window, reason)
            _advised.update(targets)
            _advised.intersection_update(targets)
        else:
            _advised.clear()


def _open_favourable(settings: dict, wsum: dict, decision: dict) -> bool:
    """Single definition of "auto-open would fire right now", shared by the
    history logger (evaluate_open) and the push notifier (notify_weather) so
    the two can't drift.

    Favourable = enabled AND forecast fresh (a degraded poller means venting_ok
    and temp are frozen at whatever the last fetch saw — not trustable) AND
    inside the night-airing window (whole night + morning grace, see
    _venting_allowed) AND cool enough AND nothing that would advise closing
    (advise_close already means high wind OR rain — so its negation is
    "wind OK and no rain").
    """
    with _degraded_lock:
        degraded = _degraded
    temp = wsum.get("temp_c")
    return bool(
        settings["open_enabled"]
        and not degraded
        and wsum.get("venting_ok", True)
        and temp is not None
        and temp < settings["open_temp_c"]
        and not decision["advise_close"]
    )


def evaluate_open(status_provider) -> None:
    """React to a cool/calm/dry night opening, once per favourable episode.

    Fires only inside the night-airing window when it's cool enough and nothing
    would advise closing (see _open_favourable, which also requires the auto-open
    toggle). Then, per equipped window we believe closed:
      - auto-close ALSO on (safety link) -> DRIVE it open and record the result;
      - otherwise                        -> simulate (log the open, drive nothing)
    so a window is never opened without its automatic close protection, and the
    trigger is still logged for calibration when driving is held back. Acted on
    once per favourable episode (rising edge), so it doesn't flood the history.
    """
    settings = db.get_automation()
    wsum = latest()
    if wsum is None:
        return
    temp = wsum.get("temp_c")
    threshold = settings["open_temp_c"]
    rsum = radar.latest()
    decision = close_decision(wsum, rsum)

    favourable = _open_favourable(settings, wsum, decision)
    # Safety link: only actually drive open when auto-close will protect it.
    live = settings["close_enabled"]

    status = status_provider()
    closed_equipped = {
        (b, w)
        for b in config.EQUIPPED_BUILDINGS
        for w, is_open in status.get(b, {}).items()
        if not is_open
    }

    with _auto_opened_lock:
        if favourable:
            detail = f"{round(temp)} °C (seuil {threshold} °C), sans vent ni pluie"
            conditions = {**wsum, **decision, "radar": rsum,
                          "auto_open_threshold_c": threshold}
            driven = False
            for building, window in closed_equipped - _auto_opened:
                if live:
                    if _drive_and_record(building, window, "open", source="auto",
                                         actor="cooling",
                                         reason=f"Ouverture automatique : {detail}",
                                         conditions=conditions, status=status):
                        driven = True
                else:
                    db.record_event(
                        "open", building=building, window=window,
                        source="auto", actor="cooling",
                        reason=f"Simulation — ouverture automatique : {detail}",
                        success=None, conditions={**conditions, "simulated": True},
                    )
                    log.info("Simulated auto-open logged for %s/%s (auto-close off)",
                             building, window)
            if driven:
                window_state.save_status(status)
            _auto_opened.update(closed_equipped)
            _auto_opened.intersection_update(closed_equipped)
        else:
            _auto_opened.clear()


def _msg(body: str, temps: str) -> str:
    """Append the temperature suffix to a notification body, if known."""
    return f"{body}. {temps}." if temps else f"{body}."


def notify_weather(status_provider=None) -> None:
    """Push a notification on each rising/falling edge of the weather signals.

    Independent of the per-window history logging in evaluate_close/open: here we
    care about *episodes*, not per-window state, so people get one push when a
    storm arrives and one when it passes. Decisions (which flags to flip, what
    to send) are made under the lock; the actual sends happen after releasing it
    so a slow ntfy POST can't block the other poller.

      close advisory rising  -> "Fermeture conseillée" / "Fermeture automatique"
      caution rising (first)  -> "Météo à surveiller"     (history only, muted)
      favourable-to-open      -> "Ouverture automatique"  (history only, muted)
      everything clears       -> "Météo dégagée"          (history only, muted)

    Only the close advisory still pushes, and only when a window is actually open
    (see `any_open`) — the softer signals are muted to push in the notify catalog
    (`push: False`) but their `system` weather rows are still recorded here so
    /history keeps the full picture. Caution and the all-clear leave no window
    event of their own (they aren't window actions), which is why they're logged
    here rather than by evaluate_close/open.
    """
    global _notified_close, _notified_open, _weather_alerted, _close_pushed
    wsum = latest()
    if wsum is None:
        return
    rsum = radar.latest()
    decision = close_decision(wsum, rsum)
    settings = db.get_automation()
    advise = decision["advise_close"]
    caution = bool(wsum.get("caution"))
    threshold = settings["open_temp_c"]
    temps = notify.format_temps(wsum)

    # Only worth telling anyone to close if something is actually open. Gate the
    # close push on at least one equipped window being open right now; if none
    # are, the advisory is moot (nothing to close) and stays silent.
    any_open = False
    if status_provider is not None:
        status = status_provider()
        any_open = any(status.get(b, {}).get(w)
                       for b in config.EQUIPPED_BUILDINGS
                       for w in status.get(b, {}))

    def _close_push():
        reason = "; ".join(decision["reasons"]) or "vent fort ou pluie"
        if settings["close_enabled"]:
            return ("auto_closed", "Fermeture automatique",
                    _msg(f"Fermeture des fenêtres — {reason}", temps))
        return ("close_advised", "Fermeture conseillée",
                _msg(f"Pensez à fermer les fenêtres — {reason}", temps))

    pending: list[tuple[str, str, str]] = []
    logs: list[tuple[str, str]] = []     # (action, reason) weather events to record
    with _notify_lock:
        if advise and not _notified_close:
            # Advisory just rose: push now if a window is open, else latch the
            # episode and wait for one to open.
            if any_open:
                pending.append(_close_push())
                _close_pushed = True
            _notified_close = True
            _weather_alerted = True
        elif advise and not _close_pushed and any_open:
            # Advisory already up from before, and a window has now been opened
            # into it — send the (still-pending) close push.
            pending.append(_close_push())
            _close_pushed = True
            _weather_alerted = True
        elif not advise:
            _notified_close = False
            _close_pushed = False

        # Caution is the softer "keep an eye out" signal; only push it as a
        # standalone first alert — once a close push has gone out (_weather_alerted)
        # a second "à surveiller" would just be noise.
        if caution and not advise and not _weather_alerted:
            reasons = "; ".join(wsum.get("caution_reasons") or []) or "risque météo"
            pending.append(("weather_caution", "Météo à surveiller",
                            _msg(f"À surveiller — {reasons}", temps)))
            logs.append(("caution", f"Vigilance météo — {reasons}"))
            _weather_alerted = True

        favourable = _open_favourable(settings, wsum, decision)
        if favourable and not _notified_open:
            pending.append(("auto_opened", "Ouverture automatique",
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
    """Run both auto evaluators (close then open — order matters: a window shut
    for a storm must not be re-opened in the same pass), then fire any edge
    notifications. Each evaluator drives or simulates per its toggle."""
    evaluate_close(status_provider)
    evaluate_open(status_provider)
    try:
        notify_weather(status_provider)
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
    """Precautionary close on a forecast outage, then fire the stale notify.

    Same live/advisory split as evaluate_close: auto-close ON drives every
    equipped window shut (blind during risk is the dangerous case, so seat them
    all); OFF advises for the windows believed open.
    """
    minutes = round(age / 60)
    last_state = _describe_last_state(wsum, rsum)
    auto_close = db.get_automation()["close_enabled"]
    status = status_provider()
    all_equipped = {
        (b, w) for b in config.EQUIPPED_BUILDINGS for w in status.get(b, {})
    }
    open_equipped = {(b, w) for (b, w) in all_equipped if status.get(b, {}).get(w)}

    detail = (f"météo indisponible depuis {minutes} min "
              f"(dernier état : {last_state})")
    conditions = {"stale": True, "stale_seconds": round(age), "touchy": touchy,
                  "last_summary": wsum, "radar": rsum}
    if auto_close:
        verb = "Fermeture préventive"
        driven = False
        for building, window in all_equipped:
            if _drive_and_record(building, window, "close", source="auto",
                                 actor="stale", reason=f"{verb} — {detail}",
                                 conditions=conditions, status=status):
                driven = True
        if driven:
            window_state.save_status(status)
    else:
        verb = "Fermeture préventive conseillée"
        for building, window in open_equipped:
            db.record_event("close", building=building, window=window,
                            source="advisory", actor="stale",
                            reason=f"{verb} — {detail}",
                            success=None, conditions=conditions)
    log.warning("Weather degraded (touchy=%s): %s — %s", touchy, verb, detail)
    notify.send(
        "weather_stale", "Météo indisponible", f"{verb} — {detail}",
        priority="high" if touchy else "default",
        stale_minutes=minutes, touchy=touchy,
    )


def evaluate_staleness(status_provider) -> None:
    """Detect a forecast outage and react on the last known state.

    Runs every poll cycle (success or failure). When the most recent successful
    forecast is older than the applicable timeout — short if the last state was
    touchy, long if it was calm — enter a degraded state once: a precautionary
    close (driven or advisory per the auto-close toggle) and notify. A later
    successful poll clears it and notifies recovery.
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
                     "caution=%s, advise_close=%s (%s), venting=%s",
                     summary["model"], summary["temp_c"], summary["rain_prob"],
                     summary["precip_mm"], round(summary["wind_gust_kmh"]),
                     summary["cape_max"], summary["caution"],
                     decision["advise_close"], decision["rain_source"],
                     summary["venting_ok"])
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
