"""Concrete notification channels: log (always-on) and ntfy.sh.

Both are project-agnostic. `NtfyChannel` speaks the ntfy.sh publish API
(https://docs.ntfy.sh/publish/), which works against the public server or any
self-hosted instance, with optional bearer-token auth for reserved/locked
topics.
"""
import logging

import requests

from .core import Channel, Notification

log = logging.getLogger(__name__)


class LogChannel(Channel):
    """Writes notifications to the logging system. The Phase-1 behaviour, kept
    as an always-on channel so notifications are visible in the app logs even
    when no push channel is configured."""

    name = "log"

    def __init__(self, logger: logging.Logger | None = None):
        self._log = logger or logging.getLogger("notify")

    def deliver(self, n: Notification) -> None:
        level = logging.WARNING if n.priority in ("high", "urgent") else logging.INFO
        self._log.log(level, "NOTIFY[%s] %s — %s%s", n.event, n.title, n.message,
                      f" {n.context}" if n.context else "")


# Our priority vocabulary → ntfy's numeric scale (1 min .. 5 max).
_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class NtfyChannel(Channel):
    """Publishes to an ntfy.sh topic, one HTTP POST per notification.

    Uses ntfy's JSON publishing endpoint (POST to the base server with the topic
    in the body) rather than the header-based form: the JSON body is UTF-8, so
    accented titles/messages (this app is in French) travel cleanly, whereas
    HTTP headers are limited to latin-1 and would mojibake an em dash or emoji.
    `markdown=True` asks supporting clients to render the body as Markdown.
    """

    name = "ntfy"

    def __init__(self, *, server: str, topic: str, token: str | None = None,
                 timeout: float = 10.0, markdown: bool = False):
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.markdown = markdown

    def deliver(self, n: Notification) -> None:
        payload = {
            "topic": self.topic,
            "title": n.title,
            "message": n.message,
            "priority": _NTFY_PRIORITY.get(n.priority, 3),
        }
        if n.tags:
            payload["tags"] = list(n.tags)
        if n.click:
            payload["click"] = n.click
        if self.markdown:
            payload["markdown"] = True
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = requests.post(self.server, json=payload, headers=headers,
                             timeout=self.timeout)
        resp.raise_for_status()
