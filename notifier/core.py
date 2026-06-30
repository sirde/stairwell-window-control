"""Channel-agnostic notification core.

Project-agnostic by design: this module imports nothing from the host
application, so it drops into another project unchanged. A `Notifier` holds an
ordered list of `Channel`s and fans every notification out to all of them. A
channel that raises is logged and skipped — a failed push must never propagate
into (and break) the calling code, since notifications are a side effect.

Typical use:

    from notifier import Notifier, NtfyChannel, LogChannel
    notifier = Notifier([LogChannel(), NtfyChannel(server=..., topic=...)])
    notifier.send(Notification("app_started", "Hello", "world"))

The host app usually wraps this behind its own thin `notify.send(event, ...)`
and an event catalog (titles, priorities, tags), keeping call sites terse.
"""
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Priority vocabulary shared across channels. Channels map these to their own
# native scale (e.g. ntfy's 1..5). Kept as plain strings so callers never need
# to import an enum from here.
PRIORITIES = ("min", "low", "default", "high", "urgent")


@dataclass
class Notification:
    """One notification, independent of how it is delivered.

    event    stable token (e.g. "weather_stale") — channels may route on it.
    title    short human title.
    message  body text (channels that support it may render light markdown).
    priority one of PRIORITIES.
    tags     channel-agnostic hints (ntfy turns known names into emoji).
    click    optional URL opened when the notification is tapped.
    context  arbitrary structured extras (logged, and available to channels).
    """
    event: str
    title: str
    message: str
    priority: str = "default"
    tags: list[str] = field(default_factory=list)
    click: str | None = None
    context: dict = field(default_factory=dict)


class Channel:
    """A delivery backend. Subclasses implement `deliver`."""

    name = "channel"

    def deliver(self, n: Notification) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class Notifier:
    """Fans notifications out to every configured channel, isolating failures."""

    def __init__(self, channels: list[Channel] | None = None):
        self.channels: list[Channel] = list(channels or [])

    def add(self, channel: Channel) -> None:
        self.channels.append(channel)

    def send(self, n: Notification) -> None:
        for channel in self.channels:
            try:
                channel.deliver(n)
            except Exception:
                # A broken channel (network down, bad config) must not break the
                # caller — the action that triggered the notification already
                # happened. Log and carry on to the next channel.
                log.exception("Notification channel %r failed for event %s",
                              getattr(channel, "name", channel), n.event)
