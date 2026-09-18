"""Out-of-band alerts when a tunnel keeps failing.

A tunnel manager that only writes to its own log assumes somebody reads that
log. :class:`Notifier` covers the opposite case — "this profile has failed N
times in a row" — by pushing that fact to ntfy and/or a generic webhook, so an
unattended machine can tell you instead of waiting to be checked.

Three rules shape the implementation:

* **It never raises.** A notification is a courtesy; turning an unreachable
  webhook into a second failure would be strictly worse than staying silent.
* **It never blocks the reconnection loop for long.** One request per channel,
  hard timeout, and only once the failure threshold is crossed.
* **It is rate limited per profile.** A tunnel that flaps all night sends one
  message per cooldown window instead of thousands.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ponte.config import NotifyConfig

__all__ = ["Notification", "Notifier"]

logger = logging.getLogger(__name__)

#: Hard timeout (seconds) for a single notification request. Long enough for a
#: slow mobile network, short enough that the retry loop never notices. The
#: request runs on the profile's own thread, between reconnect attempts.
_TIMEOUT = 10.0


@dataclasses.dataclass(frozen=True)
class Notification:
    """What happened, rendered per channel.

    ``failures`` is the number of consecutive failed attempts that triggered
    this message, so the payload carries the severity rather than just an event.
    """

    profile: str
    failures: int
    reason: str
    destination: str
    #: True for a ``ponte notify-test`` message. A test has no failure count to
    #: report, so it must not claim "failed 0 times in a row" and leave the user
    #: wondering whether a real alert would look like that.
    test: bool = False

    @property
    def title(self) -> str:
        if self.test:
            return f"ponte: {self.profile} 测试通知"
        return f"ponte: {self.profile} 连续失败 {self.failures} 次"

    @property
    def message(self) -> str:
        if self.test:
            return (
                f"这是 ponte 的测试通知（隧道 {self.profile} → {self.destination}）。\n"
                "收到即说明 [notify] 配置可用，真实断线告警会走同一条通道。"
            )
        return (
            f"隧道 {self.profile}（{self.destination}）已连续 {self.failures} 次连接失败。\n"
            f"最近原因：{self.reason or '未知'}"
        )

    def as_dict(self) -> dict[str, Any]:
        """Webhook payload. Flat and stable — this is a contract."""
        return {
            "profile": self.profile,
            "failures": self.failures,
            "reason": self.reason,
            "destination": self.destination,
            "title": self.title,
            "message": self.message,
        }


class Notifier:
    """Push failure alerts to the configured channels.

    Parameters:
        config: The ``[notify]`` section (see :class:`~ponte.config.NotifyConfig`).
        opener: ``urlopen``-compatible callable. Injected by tests so no test
            ever performs a real HTTP request.
        clock: Monotonic clock used for the cooldown. Injected by tests so the
            cooldown can be verified without sleeping.
    """

    def __init__(
        self,
        config: NotifyConfig,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.channels = config.channels
        # Enabled *and* pointed at something: ``enabled = true`` with no channel
        # is a config mistake that ``ponte doctor`` reports, but it must not
        # make the daemon pretend it sent something.
        self.enabled = bool(config.enabled and self.channels)
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        #: Message of the most recent delivery problem, for ``ponte doctor``.
        self.last_error: str | None = None
        #: Monotonic time of the last successful delivery, if any.
        self.last_sent_at: float | None = None

    # -- Public API ---------------------------------------------------------

    def notify(self, notification: Notification) -> bool:
        """Deliver *notification*, honouring the per-profile cooldown.

        Returns ``True`` when at least one channel accepted the message. Never
        raises: failures are logged and recorded in :attr:`last_error`.
        """
        if not self.enabled:
            return False
        now = self._clock()
        last = self._last_sent.get(notification.profile)
        if last is not None and now - last < self.config.cooldown:
            logger.debug(
                "notify[%s]: suppressed, %ds of cooldown left",
                notification.profile,
                int(self.config.cooldown - (now - last)),
            )
            return False

        delivered = 0
        if self.config.ntfy_topic:
            delivered += self._send_ntfy(notification)
        if self.config.webhook_url:
            delivered += self._send_webhook(notification)
        if delivered:
            self._last_sent[notification.profile] = now
            self.last_sent_at = time.time()
        return delivered > 0

    def send_test(self, profile: str = "test", destination: str = "-") -> dict[str, bool]:
        """Send one message per channel, bypassing the cooldown.

        Used by ``ponte notify-test``: the whole point is to verify the setup,
        so a cooldown from an earlier real alert must not swallow it.
        """
        notification = Notification(
            profile=profile,
            failures=0,
            reason="（测试消息：隧道本身没有问题）",
            destination=destination,
            test=True,
        )
        results: dict[str, bool] = {}
        if self.config.ntfy_topic:
            results["ntfy"] = self._send_ntfy(notification)
        if self.config.webhook_url:
            results["webhook"] = self._send_webhook(notification)
        return results

    # -- Channels -----------------------------------------------------------

    def _send_ntfy(self, notification: Notification) -> bool:
        """Publish to ntfy via its JSON API.

        The JSON body (rather than the ``Title`` header) is what keeps this
        working with non-ASCII text: HTTP headers are latin-1, so a Chinese
        title passed as a header would either raise or arrive mangled.
        """
        server = self.config.ntfy_server.rstrip("/") or "https://ntfy.sh"
        payload: dict[str, Any] = {
            "topic": self.config.ntfy_topic,
            "title": notification.title,
            "message": notification.message,
            "priority": 4,
            "tags": ["warning", "electric_plug"],
        }
        headers = {"Content-Type": "application/json"}
        if self.config.ntfy_token:
            headers["Authorization"] = f"Bearer {self.config.ntfy_token}"
        # ntfy's JSON API takes the topic in the *body* and is published to the
        # server root, which is also what makes the Authorization header work
        # for protected topics in one request.
        return self._post(
            server,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers,
            "ntfy",
        )

    def _send_webhook(self, notification: Notification) -> bool:
        """POST the notification as JSON to the configured webhook."""
        return self._post(
            self.config.webhook_url,
            json.dumps(notification.as_dict(), ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json"},
            "webhook",
        )

    # -- Transport ----------------------------------------------------------

    def _post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        channel: str,
    ) -> bool:
        """POST *body* to *url*, returning whether it was accepted."""
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            with self._opener(request, timeout=_TIMEOUT) as response:
                status = int(getattr(response, "status", 200))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"{channel}: {type(exc).__name__}: {exc}"
            logger.warning("notify: %s failed: %s", channel, exc)
            return False
        if not 200 <= status < 300:
            self.last_error = f"{channel}: HTTP {status}"
            logger.warning("notify: %s returned HTTP %s", channel, status)
            return False
        logger.info("notify: %s delivered", channel)
        self.last_error = None
        return True
