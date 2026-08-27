"""
Unit tests for alerting/telegram_notify.py.

Core invariants under test:
  - Disabled (no token/chat id) => every call is a silent no-op, never
    touches the network, always reports "success" (nothing was supposed to
    send).
  - Enabled => send() actually POSTs to the Telegram API with the right
    chat_id/text.
  - A non-200 response or a raised exception from requests.post is caught
    and turned into a `False` return, never an exception — a Telegram
    outage must never crash or stall the trading loop.
  - dedupe_key + cooldown suppresses repeat sends within the window, and
    lets them through again once the window has passed.
"""

import unittest
from unittest.mock import patch

import config
from alerting import telegram_notify


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _enable_telegram():
    config.TELEGRAM_ENABLED = True
    config.TELEGRAM_BOT_TOKEN = "test-token"
    config.TELEGRAM_CHAT_ID = "12345"
    config.TELEGRAM_ERROR_COOLDOWN_SEC = 60


def _disable_telegram():
    config.TELEGRAM_ENABLED = False
    config.TELEGRAM_BOT_TOKEN = ""
    config.TELEGRAM_CHAT_ID = ""


class TestSendWhenDisabled(unittest.TestCase):
    def setUp(self):
        _disable_telegram()
        telegram_notify._last_sent_by_key.clear()

    def test_returns_true_without_calling_requests(self):
        with patch("alerting.telegram_notify.requests.post") as mock_post:
            result = telegram_notify.send("hello")
        self.assertTrue(result)
        mock_post.assert_not_called()

    def test_convenience_wrappers_are_also_noops(self):
        with patch("alerting.telegram_notify.requests.post") as mock_post:
            self.assertTrue(telegram_notify.notify_started(("A", "B"), "btc-updown-5m"))
            self.assertTrue(telegram_notify.notify_trade_opened("A", "slug", "BUY_UP", 10.0, 0.55, 0.07))
            self.assertTrue(telegram_notify.notify_trade_settled("A", "slug", "UP", True, 5.0, 1005.0))
            self.assertTrue(telegram_notify.notify_error("ctx", "detail", dedupe_key="k"))
            self.assertTrue(telegram_notify.notify_status(["line1"]))
        mock_post.assert_not_called()


class TestSendWhenEnabled(unittest.TestCase):
    def setUp(self):
        _enable_telegram()
        telegram_notify._last_sent_by_key.clear()

    def tearDown(self):
        _disable_telegram()

    def test_posts_to_telegram_api_with_chat_id_and_text(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            result = telegram_notify.send("hello world")

        self.assertTrue(result)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn("test-token", args[0])
        self.assertEqual(kwargs["json"]["chat_id"], "12345")
        self.assertEqual(kwargs["json"]["text"], "hello world")

    def test_non_200_response_returns_false_without_raising(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(401, "Unauthorized")):
            result = telegram_notify.send("hello")
        self.assertFalse(result)

    def test_request_exception_returns_false_without_raising(self):
        with patch("alerting.telegram_notify.requests.post", side_effect=ConnectionError("boom")):
            result = telegram_notify.send("hello")  # must not raise
        self.assertFalse(result)

    def test_dedupe_key_suppresses_repeat_within_cooldown(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            telegram_notify.send("first", dedupe_key="err1")
            telegram_notify.send("second", dedupe_key="err1")  # suppressed
        self.assertEqual(mock_post.call_count, 1)

    def test_dedupe_key_allows_send_again_after_cooldown_elapses(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            with patch("alerting.telegram_notify.time.time", return_value=1000.0):
                telegram_notify.send("first", dedupe_key="err1", cooldown_sec=60)
            with patch("alerting.telegram_notify.time.time", return_value=1061.0):  # 61s later
                telegram_notify.send("second", dedupe_key="err1", cooldown_sec=60)
        self.assertEqual(mock_post.call_count, 2)

    def test_different_dedupe_keys_are_independent(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            telegram_notify.send("first", dedupe_key="err1")
            telegram_notify.send("second", dedupe_key="err2")
        self.assertEqual(mock_post.call_count, 2)

    def test_events_without_dedupe_key_always_go_through(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            telegram_notify.notify_trade_opened("A", "slug", "BUY_UP", 10.0, 0.55, 0.07)
            telegram_notify.notify_trade_opened("A", "slug", "BUY_UP", 10.0, 0.55, 0.07)
        self.assertEqual(mock_post.call_count, 2)

    def test_notify_trade_opened_handles_missing_edge(self):
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            telegram_notify.notify_trade_opened("A", "slug", "BUY_UP", 10.0, 0.55, None)
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("n/a", text)

    def test_notify_trade_opened_shows_magnitude_not_signed_value_for_buy_down(self):
        # Regression test for a real live-confirmed bug: net_edge is signed
        # toward "Up" (see engine/decision_engine.py's decide()) — a
        # BUY_DOWN trade is triggered by a strongly NEGATIVE net_edge (e.g.
        # -0.114 for a real trade seen live), which is the CORRECT, healthy
        # trigger condition, not a costs-exceeding-edge problem. Showing
        # that raw signed "-11.4%" next to "BUY_DOWN" reads backwards — as
        # if the bot traded on bad edge. The message must show the
        # magnitude only; direction is already conveyed by `side`.
        with patch("alerting.telegram_notify.requests.post", return_value=_FakeResponse(200)) as mock_post:
            telegram_notify.notify_trade_opened("B", "slug", "BUY_DOWN", 30.0, 0.770, -0.114)
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("11.4%", text)
        self.assertNotIn("-11.4%", text)


if __name__ == "__main__":
    unittest.main()