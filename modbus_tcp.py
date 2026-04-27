"""Modbus-TCP relay client.

One relay board per building. Each board exposes coils that, when pulsed,
drive the open / close contactors of a window motor. We hold each coil
high briefly, then fire an "all relays off" command ~10 s later so the
contactors are not left energised.
"""
import logging
import socket
import threading
import time
from contextlib import contextmanager

import config

log = logging.getLogger(__name__)

RELEASE_DELAY_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 3

# Per-module-IP state. One lock serialises every Modbus transaction against
# a given relay board (most boards only tolerate a single client at a time).
# One release timer per board (re)arms the all-relays-off on each command.
_module_locks: dict[str, threading.Lock] = {}
_release_timers: dict[str, threading.Timer] = {}
_state_lock = threading.Lock()


def _module_lock(ip: str) -> threading.Lock:
    with _state_lock:
        lock = _module_locks.get(ip)
        if lock is None:
            lock = threading.Lock()
            _module_locks[ip] = lock
        return lock


def _schedule_release(ip: str) -> None:
    with _state_lock:
        existing = _release_timers.get(ip)
        if existing is not None:
            existing.cancel()
        t = threading.Timer(RELEASE_DELAY_SECONDS, _release_all, args=(ip,))
        t.daemon = True
        _release_timers[ip] = t
        t.start()


def _release_all(ip: str) -> None:
    try:
        with _module_lock(ip), _tcp_session(ip) as client:
            client.all_relay_off()
    except Exception:
        log.exception("Release-all failed on %s", ip)
    finally:
        with _state_lock:
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


def send_window_command(building: str, window_id: str, action: str) -> tuple[bool, str]:
    log.info("%s window %s: %s", building, window_id, action)
    try:
        ip = config.module_address[building]
    except KeyError:
        return False, f"Aucun module configuré pour {building}"

    try:
        with _module_lock(ip), _tcp_session(ip) as client:
            relay_open = config.BUILDINGS[building].index(window_id) * 2
            relay_close = relay_open + 1
            if action == "close":
                client.write_coil(relay_open, 0)
                client.write_coil(relay_close, 1)
            else:
                client.write_coil(relay_open, 1)
                client.write_coil(relay_close, 0)
    except (OSError, socket.timeout) as e:
        log.exception("Modbus communication error on %s/%s (%s)", building, window_id, ip)
        return False, f"Erreur de communication avec le module {ip} : {e}"
    except Exception as e:
        log.exception("Unexpected error on %s/%s (%s)", building, window_id, ip)
        return False, f"Erreur inattendue ({ip}) : {e}"

    _schedule_release(ip)
    return True, "Commande envoyée"
