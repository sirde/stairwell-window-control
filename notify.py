"""Project notification layer: event catalog + configured channels.

This is the Extracteur-specific glue on top of the project-agnostic `notifier`
package. It builds the channel list from `config` once at import, owns the
catalog mapping each event token to its default priority and emoji tags, and
exposes the same `send(event, title, message, ...)` chokepoint the rest of the
app already calls — so wiring a new channel is still a one-file change here,
and call sites stay terse.

The reusable transport lives in `notifier/`; only this file knows about our
events, our config, and our weather summary shape.
"""
import logging

import config
from notifier import LogChannel, Notification, Notifier, NtfyChannel

log = logging.getLogger(__name__)


def _build_notifier() -> Notifier:
    """Assemble the channel list from config. LogChannel is always on so
    notifications stay visible in the app logs even with no push configured."""
    channels = [LogChannel(logging.getLogger("notify"))]
    if config.NTFY_ENABLED and config.NTFY_TOPIC:
        channels.append(NtfyChannel(
            server=config.NTFY_SERVER, topic=config.NTFY_TOPIC,
            token=config.NTFY_TOKEN,
        ))
        log.info("ntfy notifications enabled (%s/%s)",
                 config.NTFY_SERVER, config.NTFY_TOPIC)
    elif config.NTFY_ENABLED:
        log.warning("NTFY_ENABLED but NTFY_TOPIC is empty — push disabled, log only")
    return Notifier(channels)


_notifier = _build_notifier()


# Event catalog: token -> default priority + emoji tags (ntfy renders the tag
# names as emoji). A caller may still override the priority per call (e.g. the
# stale alert escalates to "high" when the last weather was touchy). Tags are
# ASCII GitHub-style shortcodes so they stay header-safe.
EVENTS = {
    "app_started":       {"priority": "default", "tags": ["arrows_counterclockwise"]},
    "relay_unreachable": {"priority": "high",    "tags": ["warning"]},
    "close_advised":     {"priority": "high",    "tags": ["wind_face", "umbrella"]},
    "auto_closed":       {"priority": "high",    "tags": ["lock", "umbrella"]},
    "auto_opened":       {"priority": "default", "tags": ["window"]},
    "window_opened":     {"priority": "low",     "tags": ["window"]},
    "window_closed":     {"priority": "low",     "tags": ["window"]},
    "weather_caution":   {"priority": "default", "tags": ["eyes"]},
    "weather_clear":     {"priority": "low",     "tags": ["white_check_mark"]},
    "weather_stale":     {"priority": "default", "tags": ["fog"]},
    "weather_recovered": {"priority": "default", "tags": ["white_check_mark"]},
}


def format_temps(wsum: dict | None) -> str:
    """Compact 'Int 26 °C · Ext 24 °C' suffix for notification bodies.

    External comes from the forecast summary. Internal arrives with the Phase-2
    indoor sensor (`indoor_temp_c`); until then only the external part shows.
    Returns '' when no temperature is known.
    """
    if not wsum:
        return ""
    parts = []
    indoor = wsum.get("indoor_temp_c")
    if indoor is not None:
        parts.append(f"Int {round(indoor)} °C")
    outdoor = wsum.get("temp_c")
    if outdoor is not None:
        parts.append(f"Ext {round(outdoor)} °C")
    return " · ".join(parts)


def send(event: str, title: str, message: str, *,
         priority: str | None = None, tags: list[str] | None = None,
         click: str | None = None, **context) -> None:
    """Emit a notification through every configured channel.

    `priority`/`tags` fall back to the event catalog when not given; an explicit
    value always wins. `click` defaults to the dashboard so a tap opens the app.
    Never raises — delivery failures are swallowed and logged by the notifier.
    """
    defaults = EVENTS.get(event, {})
    n = Notification(
        event=event,
        title=title,
        message=message,
        priority=priority or defaults.get("priority", "default"),
        tags=tags if tags is not None else list(defaults.get("tags", [])),
        click=click or (config.PUBLIC_BASE_URL or None),
        context=context,
    )
    _notifier.send(n)
