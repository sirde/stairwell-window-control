"""Modbus-TCP relay client.

One relay board per building. Each board exposes coils that, when pulsed,
drive the open / close contactors of a window motor. We hold each coil
high for the caller-supplied drive time, then fire an "all relays off"
command so the contactors are not left energised. Travel is purely timed
(no position feedback), so the drive duration is what sets how far the
window moves — see config.WINDOW_*_SECONDS.

Each window gets its OWN release timer that zeroes just its two coils after
exactly its drive time, so overlapping commands on one board can neither cut
each other short nor stretch a partial open into a full one. A board-wide
"all relays off" backstop is also armed, at the furthest deadline any command
asked for, in case a per-window release fails. If a command fails after a
coil may have been energised, a short failsafe release is armed so the
contactor can't stay pulled in forever.
"""
import logging
import socket
import threading
import time
from contextlib import contextmanager

import config

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 3

# When a command fails after a coil write may have gone out, release quickly:
# the app reports the command failed (state not persisted), so the window must
# not keep driving on a coil nobody accounts for.
FAILSAFE_RELEASE_SECONDS = 2.0

# Per-module-IP state. One lock serialises every Modbus transaction against
# a given relay board (most boards only tolerate a single client at a time).
# One release timer per window (keyed by (ip, open-channel)) plus one backstop
# timer per board; the deadline a timer was armed for doubles as its identity,
# so a stale timer that already started firing can be told to stand down.
_module_locks: dict[str, threading.Lock] = {}
_release_timers: dict[str, threading.Timer] = {}
_release_deadlines: dict[str, float] = {}
_window_timers: dict[tuple[str, int], threading.Timer] = {}
_window_deadlines: dict[tuple[str, int], float] = {}
_state_lock = threading.Lock()


def _module_lock(ip: str) -> threading.Lock:
    with _state_lock:
        lock = _module_locks.get(ip)
        if lock is None:
            lock = threading.Lock()
            _module_locks[ip] = lock
        return lock


def _schedule_window_release(ip: str, relay_open: int, delay_seconds: float) -> None:
    """Arm this window's own coils-off after exactly its drive time.

    Timers are per window, so overlapping commands on the same board can never
    change how far another window travels. A new command for the SAME window
    replaces its pending release outright (the coils were just rewritten, the
    new command owns the window now).
    """
    key = (ip, relay_open)
    with _state_lock:
        deadline = time.monotonic() + delay_seconds
        _window_deadlines[key] = deadline
        existing = _window_timers.get(key)
        if existing is not None:
            existing.cancel()
        t = threading.Timer(delay_seconds, _release_window,
                            args=(ip, relay_open, deadline))
        t.daemon = True
        _window_timers[key] = t
        t.start()


def _release_window(ip: str, relay_open: int, deadline: float) -> None:
    key = (ip, relay_open)
    try:
        with _module_lock(ip):
            # Re-check under the board lock: Timer.cancel() can't stop a
            # callback that has already started, so a timer that fired while a
            # new command for this window held the lock would otherwise cut
            # that command's drive short the instant the lock is released.
            with _state_lock:
                if _window_deadlines.get(key) != deadline:
                    return
            with _tcp_session(ip) as client:
                client.write_coil(relay_open, 0)
                client.write_coil(relay_open + 1, 0)
    except Exception:
        log.exception("Window release failed on %s ch%d "
                      "(board backstop all-off still armed)", ip, relay_open)
    finally:
        with _state_lock:
            if _window_deadlines.get(key) == deadline:
                _window_deadlines.pop(key, None)
                _window_timers.pop(key, None)


def _schedule_release(ip: str, delay_seconds: float) -> None:
    """Arm the board-wide all-relays-off BACKSTOP.

    The per-window releases above do the precise work; this fires at the
    furthest deadline any command asked for and clears the whole board, so a
    window release that failed (network blip) can't leave a contactor pulled
    in. Taking the max means it can never truncate a drive still in flight.
    """
    with _state_lock:
        deadline = time.monotonic() + delay_seconds
        current = _release_deadlines.get(ip)
        if current is not None and current >= deadline:
            return  # an armed backstop already covers this command
        _release_deadlines[ip] = deadline
        existing = _release_timers.get(ip)
        if existing is not None:
            existing.cancel()
        t = threading.Timer(delay_seconds, _release_all, args=(ip, deadline))
        t.daemon = True
        _release_timers[ip] = t
        t.start()


def _release_all(ip: str, deadline: float) -> None:
    try:
        with _module_lock(ip):
            # Same stand-down rule as _release_window: if a newer deadline owns
            # the backstop now, this (stale, already-fired) timer must not
            # touch the board.
            with _state_lock:
                if _release_deadlines.get(ip) != deadline:
                    return
            with _tcp_session(ip) as client:
                client.all_relay_off()
    except Exception:
        log.exception("Release-all failed on %s", ip)
    finally:
        with _state_lock:
            if _release_deadlines.get(ip) == deadline:
                _release_deadlines.pop(ip, None)
                _release_timers.pop(ip, None)


class ModbusTCPClient:
    def __init__(self, host: str, port: int = 502):
        self.host = host
        self.port = port
        self.socket: socket.socket | None = None

    def connect(self) -> None:
        s = socket.socket()
        s.settimeout(CONNECT_TIMEOUT_SECONDS)
        s.connect((self.host, self.port))
        self.socket = s
        log.debug("Connected to %s:%d", self.host, self.port)

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _send(self, command: list[int]) -> bytes:
        if self.socket is None:
            raise RuntimeError("Socket not connected")
        self.socket.send(bytearray(command))
        time.sleep(0.2)
        return self.socket.recv(1024)

    def write_coil(self, channel: int, value: int) -> None:
        """Force Single Coil (0x05) on the given channel."""
        cmd = [0, 0, 0, 0, 0, 0x06, 0x01, 0x05,
               0x00, channel,
               0xFF if value else 0x00, 0x00]
        resp = self._send(cmd)
        log.debug("write_coil ch=%d val=%d resp=%s", channel, value, resp.hex())

    def all_relay_off(self) -> None:
        """Vendor-specific "release all relays at once" (coil address 0x00FF).

        Not standard Modbus — this board interprets writing coil 0xFF = OFF
        as an "all coils off" broadcast. Do not replace with function 0x0F.
        """
        cmd = [0, 0, 0, 0, 0, 0x06, 0x01, 0x05, 0x00, 0xFF, 0x00, 0x00]
        resp = self._send(cmd)
        log.debug("all_relay_off resp=%s", resp.hex())


@contextmanager
def _tcp_session(ip: str):
    client = ModbusTCPClient(ip)
    client.connect()
    try:
        yield client
    finally:
        client.close()


def probe(ip: str) -> bool:
    """Reachability check: can we open a Modbus/TCP session to the board now?

    Serialised on the per-board lock so a probe never collides with an in-flight
    window command (these boards accept a single client at a time). Returns True
    on a successful connect, False on any connection error/timeout — never
    raises, so a monitor loop can call it freely.
    """
    try:
        with _module_lock(ip), _tcp_session(ip):
            return True
    except (OSError, socket.timeout):
        return False
    except Exception:
        log.exception("Relay probe error for %s", ip)
        return False


def send_window_command(building: str, window_id: str, action: str,
                        duration_seconds: float) -> tuple[bool, str]:
    log.info("%s window %s: %s (%.0fs)", building, window_id, action, duration_seconds)
    try:
        ip = config.module_address[building]
    except KeyError:
        return False, f"Aucun module configuré pour {building}"

    energised = False
    try:
        with _module_lock(ip), _tcp_session(ip) as client:
            relay_open = config.BUILDINGS[building].index(window_id) * 2
            relay_close = relay_open + 1
            energised = True  # from here on a coil write may have gone out
            if action == "close":
                client.write_coil(relay_open, 0)
                client.write_coil(relay_close, 1)
            else:
                client.write_coil(relay_open, 1)
                client.write_coil(relay_close, 0)
            # Arm the releases while still holding the board lock, so a stale
            # timer firing right now (see _release_window/_release_all)
            # observes the new deadlines before it can touch the board.
            _schedule_window_release(ip, relay_open, duration_seconds)
            _schedule_release(ip, duration_seconds)
    except (OSError, socket.timeout) as e:
        if energised:
            _schedule_window_release(ip, relay_open, FAILSAFE_RELEASE_SECONDS)
            _schedule_release(ip, FAILSAFE_RELEASE_SECONDS)
        log.exception("Modbus communication error on %s/%s (%s)", building, window_id, ip)
        return False, f"Erreur de communication avec le module {ip} : {e}"
    except Exception as e:
        if energised:
            _schedule_window_release(ip, relay_open, FAILSAFE_RELEASE_SECONDS)
            _schedule_release(ip, FAILSAFE_RELEASE_SECONDS)
        log.exception("Unexpected error on %s/%s (%s)", building, window_id, ip)
        return False, f"Erreur inattendue ({ip}) : {e}"

    return True, "Commande envoyée"
