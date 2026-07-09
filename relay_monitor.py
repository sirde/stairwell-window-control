"""Relay reachability monitor.

A background thread periodically TCP-probes each equipped building's Modbus
relay board, tracks per-building up/down state in memory, and fires exactly one
"module injoignable" push on each healthy->unreachable transition — including
the case where a board is already unreachable at startup (None -> down counts
as a transition). Recovery is logged, not pushed, to keep the alert stream to
what residents asked for; the home page clears its live warning on its own from
`status()`.

Kept deliberately separate from `modbus_tcp` (which stays pure hardware, no
notify/app imports) and mirrors the weather/radar poller shape.
"""
import logging
import threading

import config
import db
import modbus_tcp
import notify

log = logging.getLogger(__name__)

_lock = threading.Lock()
# building_id -> {"reachable": bool, "ip": str, "checked_at": iso, "since": iso}
# `since` is when the current reachable/unreachable state was first observed.
_status: dict[str, dict] = {}


def status() -> dict:
    """Snapshot of the latest per-building reachability (safe to serialise)."""
    with _lock:
        return {b: dict(v) for b, v in _status.items()}


def _label(building: str) -> str:
    return building.replace("building_", "Bâtiment ")


def _probe_all() -> None:
    for building in sorted(config.EQUIPPED_BUILDINGS):
        ip = config.module_address.get(building)
        if not ip:
            continue
        reachable = modbus_tcp.probe(ip)
        now = db.iso_utc_now()
        with _lock:
            prev = _status.get(building, {}).get("reachable")
            changed = prev != reachable
            _status[building] = {
                "reachable": reachable,
                "ip": ip,
                "checked_at": now,
                "since": now if changed else _status[building].get("since", now),
            }
        if not changed:
            continue
        if not reachable:
            log.warning("Relay unreachable: %s (%s)", building, ip)
            notify.send(
                "relay_unreachable", "Module injoignable",
                f"{_label(building)} : relais injoignable ({ip}). "
                f"Impossible de piloter les fenêtres de ce bâtiment.")
        elif prev is not None:
            # prev None -> reachable is a healthy first probe: nothing to say.
            log.info("Relay reachable again: %s (%s)", building, ip)


def _loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _probe_all()
        except Exception:
            log.exception("Relay monitor error")
        stop_event.wait(config.RELAY_PING_SECONDS)


def start() -> threading.Event:
    """Start the background monitor. Returns the Event used to stop it."""
    stop_event = threading.Event()
    if not config.EQUIPPED_BUILDINGS:
        log.info("Relay monitor: no equipped buildings — not starting")
        return stop_event
    threading.Thread(target=_loop, args=(stop_event,),
                     name="relay-monitor", daemon=True).start()
    log.info("Relay monitor started (every %ss for %s)",
             config.RELAY_PING_SECONDS, ", ".join(sorted(config.EQUIPPED_BUILDINGS)))
    return stop_event
