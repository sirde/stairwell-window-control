"""Believed physical window state, persisted to a small JSON file.

Extracted from app.py so the weather poller can read AND write it directly
(going live, the auto evaluators drive the relay and must record the new state)
without importing app — which would be circular. Depth-aware values:
False = closed, "full" / "partial" = open depth.

The file is a *belief* cache, not ground truth (there is no position feedback):
it is reset to all-closed on startup and reconciled with the current building
config on every read. Writes are atomic (temp file + rename).
"""
import json
import os
import tempfile

import config

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")


def default_status() -> dict:
    return {b: {w: False for w in windows} for b, windows in config.BUILDINGS.items()}


def load_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_status()

    # Reconcile with current config: add missing buildings/windows, drop stale.
    # Values are depth-aware: False = closed, "full" / "partial" = open depth.
    # Legacy plain-boolean files (pre-depth) map True to a full open.
    reconciled = default_status()
    for building, windows in reconciled.items():
        for window in windows:
            if building in data and window in data[building]:
                v = data[building][window]
                reconciled[building][window] = (
                    v if v in ("full", "partial") else ("full" if v else False))
    return reconciled


def save_status(data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(STATUS_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, STATUS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
