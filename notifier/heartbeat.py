"""Outbound liveness heartbeat for a dead-man's-switch monitor.

A process cannot announce its own death, so "is the app up?" must be judged
from outside. This pings a monitor URL (e.g. healthchecks.io's hc-ping.com/<id>)
on a fixed interval from a daemon thread; when the process or host dies the pings
stop and the monitor raises the alarm on its own side (which can be wired to the
same ntfy topic for a unified alert stream).

Project-agnostic: give it a URL and an interval.
"""
import logging
import threading

import requests

log = logging.getLogger(__name__)


def _loop(url: str, interval: float, stop_event: threading.Event,
          timeout: float) -> None:
    # Ping once immediately so a misconfigured URL surfaces in the logs at
    # startup rather than one interval later.
    while True:
        try:
            requests.get(url, timeout=timeout)
            log.debug("Heartbeat ping ok")
        except requests.RequestException as e:
            # A failed ping is not fatal — the monitor treats a *missing* ping
            # as down, and a transient network blip will be covered by the next
            # one (and the monitor's grace period).
            log.warning("Heartbeat ping failed: %s", e)
        if stop_event.wait(interval):
            return


def start(url: str, interval: float, *, timeout: float = 10.0) -> threading.Event:
    """Start the heartbeat thread. Returns the Event used to stop it."""
    stop_event = threading.Event()
    threading.Thread(
        target=_loop, args=(url, interval, stop_event, timeout),
        name="heartbeat", daemon=True,
    ).start()
    log.info("Heartbeat started (every %ss to %s)", interval, url)
    return stop_event
