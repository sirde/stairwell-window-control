"""Reusable, channel-agnostic notification toolkit.

Public API:

    from notifier import Notifier, Notification, Channel
    from notifier import LogChannel, NtfyChannel
    from notifier import heartbeat

No host-application imports — drop this package into any project.
"""
from . import heartbeat
from .channels import LogChannel, NtfyChannel
from .core import PRIORITIES, Channel, Notification, Notifier

__all__ = [
    "Notifier", "Notification", "Channel", "PRIORITIES",
    "LogChannel", "NtfyChannel", "heartbeat",
]
