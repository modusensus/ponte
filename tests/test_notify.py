"""pytest tests for :mod:`ponte.notify` (no real HTTP request is ever made)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from ponte.config import ConfigValidationError, NotifyConfig, load_config
from ponte.notify import Notification, Notifier


class _Response:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Recorder:
    """``urlopen`` stand-in that records requests instead of sending them."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.requests: list[object] = []
        self.timeouts: list[float | None] = []
        self.status = status
        self.error = error

    def __call__(self, request, timeout=None):  # noqa: ANN001 - urlopen shape
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return _Response(self.status)

    @property
    def last(self):  # noqa: ANN201 - request object
        return self.requests[-1]

    def body(self) -> dict:
        return json.loads(self.last.data.decode("utf-8"))

    def headers(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.last.header_items()}


class _Clock:
    """Monotonic clock stand-in, so the cooldown is tested without sleeping."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _notify(**overrides) -> NotifyConfig:
    base = dict(enabled=True, on_consecutive_failures=3, cooldown=900)
    base.update(overrides)
    return NotifyConfig(**base)


def _notification(profile: str = "web") -> Notification:
    return Notification(
        profile=profile, failures=4, reason="ssh exited with code 255", destination="u@h"
    )


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


def test_disabled_notifier_sends_nothing() -> None:
    recorder = _Recorder()
    notifier = Notifier(
        _notify(enabled=False, ntfy_topic="t"), opener=recorder, clock=_Clock()
    )
    assert notifier.enabled is False
    assert notifier.notify(_notification()) is False
    assert recorder.requests == []


def test_enabled_without_channel_is_not_enabled() -> None:
    """enabled = true 但没配通道：不能假装发出去了（doctor 会报这个）。"""
    notifier = Notifier(_notify(), opener=_Recorder(), clock=_Clock())
    assert notifier.channels == ()
    assert notifier.enabled is False


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_ntfy_posts_the_topic_in_the_body() -> None:
    """话题走 JSON body：HTTP 头是 latin-1，中文标题只能放在 body 里。"""
    recorder = _Recorder()
    notifier = Notifier(_notify(ntfy_topic="ponte-x"), opener=recorder, clock=_Clock())
    assert notifier.notify(_notification()) is True

    request = recorder.last
    assert request.full_url == "https://ntfy.sh"
    assert request.get_method() == "POST"
    body = recorder.body()
    assert body["topic"] == "ponte-x"
    assert "web" in body["title"]
    assert "4" in body["title"] or "4 次" in body["title"]
    assert body["priority"] == 4
    assert "authorization" not in recorder.headers()
    assert recorder.timeouts == [10.0]


def test_ntfy_server_and_token_are_honoured() -> None:
    recorder = _Recorder()
    notifier = Notifier(
        _notify(
            ntfy_topic="t",
            ntfy_server="https://ntfy.example.com/",
            ntfy_token="tk_secret",
        ),
        opener=recorder,
        clock=_Clock(),
    )
    notifier.notify(_notification())
    assert recorder.last.full_url == "https://ntfy.example.com"
    assert recorder.headers()["authorization"] == "Bearer tk_secret"


def test_webhook_posts_a_flat_payload() -> None:
    recorder = _Recorder()
    notifier = Notifier(
        _notify(webhook_url="https://hooks.example.com/x"), opener=recorder, clock=_Clock()
    )
    assert notifier.notify(_notification()) is True
    body = recorder.body()
    assert body["profile"] == "web"
    assert body["failures"] == 4
    assert body["reason"] == "ssh exited with code 255"
    assert body["destination"] == "u@h"
    assert "conn" in body["message"] or "失败" in body["message"]
    assert recorder.headers()["content-type"] == "application/json"


def test_both_channels_receive_the_alert() -> None:
    recorder = _Recorder()
    notifier = Notifier(
        _notify(ntfy_topic="t", webhook_url="https://hooks.example.com/x"),
        opener=recorder,
        clock=_Clock(),
    )
    assert notifier.notify(_notification()) is True
    assert len(recorder.requests) == 2


# ---------------------------------------------------------------------------
# Rate limiting & failure tolerance
# ---------------------------------------------------------------------------


def test_cooldown_suppresses_then_allows_again() -> None:
    recorder = _Recorder()
    clock = _Clock(1000.0)
    notifier = Notifier(_notify(ntfy_topic="t", cooldown=60), opener=recorder, clock=clock)

    assert notifier.notify(_notification()) is True
    clock.now += 10
    assert notifier.notify(_notification()) is False, "冷却期内不该重复发送"
    assert len(recorder.requests) == 1

    clock.now += 60
    assert notifier.notify(_notification()) is True
    assert len(recorder.requests) == 2


def test_cooldown_is_per_profile() -> None:
    recorder = _Recorder()
    notifier = Notifier(_notify(ntfy_topic="t"), opener=recorder, clock=_Clock())
    assert notifier.notify(_notification("web")) is True
    assert notifier.notify(_notification("db")) is True
    assert len(recorder.requests) == 2


def test_transport_failure_is_swallowed_and_recorded() -> None:
    """通知失败不能变成第二个故障：不抛、返回 False、留下原因。"""
    recorder = _Recorder(error=urllib.error.URLError("network is unreachable"))
    notifier = Notifier(_notify(ntfy_topic="t"), opener=recorder, clock=_Clock())
    assert notifier.notify(_notification()) is False
    assert notifier.last_error is not None
    assert "URLError" in notifier.last_error


def test_http_error_status_is_recorded() -> None:
    notifier = Notifier(
        _notify(ntfy_topic="t"), opener=_Recorder(status=500), clock=_Clock()
    )
    assert notifier.notify(_notification()) is False
    assert "HTTP 500" in (notifier.last_error or "")


def test_failed_delivery_does_not_start_the_cooldown() -> None:
    """发送失败不占用冷却窗口，否则一次网络抖动会吞掉整个窗口。"""
    recorder = _Recorder(status=500)
    notifier = Notifier(_notify(ntfy_topic="t"), opener=recorder, clock=_Clock())
    notifier.notify(_notification())
    recorder.status = 200
    assert notifier.notify(_notification()) is True


def test_send_test_bypasses_the_cooldown() -> None:
    """测试通知的意义就是立刻验证配置，不能被冷却吞掉。"""
    recorder = _Recorder()
    notifier = Notifier(_notify(ntfy_topic="t"), opener=recorder, clock=_Clock())
    assert notifier.notify(_notification()) is True
    results = notifier.send_test(profile="web")
    assert results == {"ntfy": True}
    assert len(recorder.requests) == 2
    body = recorder.body()
    assert "测试" in body["message"]
    # 测试消息不能说成「连续失败 0 次」——那会让人以为真实告警长这样。
    assert "失败" not in body["title"]
    assert "web" in body["title"]


def test_send_test_reports_every_channel() -> None:
    notifier = Notifier(
        _notify(ntfy_topic="t", webhook_url="https://h.example.com"),
        opener=_Recorder(status=502),
        clock=_Clock(),
    )
    assert notifier.send_test() == {"ntfy": False, "webhook": False}


def test_notification_rendering_mentions_profile_and_count() -> None:
    notification = _notification("db")
    assert "db" in notification.title and "4" in notification.title
    assert "db" in notification.message
    assert "u@h" in notification.message
    assert notification.as_dict()["title"] == notification.title


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _config(tmp_path, notify: str) -> str:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[ssh]
host = "example.com"
user = "u"
identity_file = "{(tmp_path / 'id_rsa').as_posix()}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

{notify}
""",
        encoding="utf-8",
    )
    return str(path)


def test_notify_defaults_are_off(tmp_path) -> None:
    notify = load_config(_config(tmp_path, "")).notify
    assert notify.enabled is False
    assert notify.channels == ()
    assert notify.on_consecutive_failures == 3
    assert notify.cooldown == 900
    assert notify.ntfy_server == "https://ntfy.sh"


def test_notify_parsed(tmp_path) -> None:
    notify = load_config(
        _config(
            tmp_path,
            """
[notify]
enabled = true
on_consecutive_failures = 5
cooldown = 60
ntfy_topic = "ponte-x"
ntfy_token = "tk"
webhook_url = "https://hooks.example.com/x"
""",
        )
    ).notify
    assert notify.enabled is True
    assert notify.channels == ("ntfy", "webhook")
    assert notify.on_consecutive_failures == 5
    assert notify.cooldown == 60
    assert notify.ntfy_token == "tk"


def test_notify_rejects_non_http_url(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="http"):
        load_config(_config(tmp_path, '\n[notify]\nwebhook_url = "file:///etc/passwd"\n'))


def test_notify_rejects_zero_threshold(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="on_consecutive_failures"):
        load_config(
            _config(tmp_path, "\n[notify]\nntfy_topic = \"t\"\non_consecutive_failures = 0\n")
        )


def test_notify_enabled_without_channel_warns(tmp_path) -> None:
    cfg = load_config(_config(tmp_path, "\n[notify]\nenabled = true\n"))
    assert any("不会发出任何通知" in warning for warning in cfg.warnings)


def test_notify_unknown_key_is_reported(tmp_path) -> None:
    cfg = load_config(_config(tmp_path, '\n[notify]\nntfy_topics = "typo"\n'))
    assert any("notify.ntfy_topics" in warning for warning in cfg.warnings)
