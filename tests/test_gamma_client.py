"""
Unit tests for polymarket/gamma_client.py's find_live_btc_updown_market().

find_live_btc_updown_market() was rewritten to look up the live window by
its exact, predictable slug ("<slug_contains>-<window_start_unix_ts>",
computed from the wall clock) instead of paging through Gamma's "most
recently created active markets" and filtering client-side. That older
approach had a real, confirmed failure mode: Polymarket pre-creates each
5-minute window's market document roughly a day before the window itself
opens, so "most recently created" surfaces tomorrow's freshly-published
windows ahead of today's actually-live one (which was itself created ~24h
ago and is, by now, far back in creation order) — no amount of paging fixes
a wrong sort key. These tests cover the new exact-slug design; they replace
an earlier version of this file that tested the paging/scanning approach,
which no longer exists.

Also covers a second, related live-confirmed bug: MarketInfo.start_date
must NOT come from Gamma's raw "startDate" field — that field turned out to
be roughly the market's creation/publish time, not its trading window's
real start, which silently broke resolve_reference_price() (every market
was skipped as missing_reference_price, since the BTC feed never had a
sample within tolerance of a timestamp ~24h in the past). start_date must
instead be the same window-start timestamp used to compute the looked-up
slug — confirmed against two live samples to equal end_date minus the
window length exactly.

Uses the real wall clock (no datetime mocking) — the current window's start
is computed the same way the code under test computes it, so these tests
are robust to whenever they actually run.
"""

import datetime as dt
import unittest
from unittest.mock import patch

from polymarket import gamma_client

_WINDOW_SECONDS = 300  # matches "5m" in "btc-updown-5m"


def _current_window_start_ts() -> int:
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    return int(now_ts // _WINDOW_SECONDS) * _WINDOW_SECONDS


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _raw_market(slug: str, *, start_ts: float, closed: bool = False) -> dict:
    # Deliberately gives "startDate" an unrelated, far-off value (mirroring
    # the real live data: Gamma's startDate is NOT the window's real start)
    # so any test that accidentally used it instead of the looked-up
    # start_ts would fail loudly rather than passing by coincidence.
    return {
        "slug": slug,
        "question": f"question for {slug}",
        "conditionId": f"cond-{slug}",
        "startDate": _iso(start_ts - 86_400),  # ~24h earlier, like the real "publish time"
        "endDate": _iso(start_ts + _WINDOW_SECONDS),
        "closed": closed,
        "clobTokenIds": ["up-token", "down-token"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["0.5", "0.5"],
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestFindLiveBtcUpdownMarket(unittest.TestCase):
    def test_returns_the_current_window_looked_up_by_exact_slug(self):
        start_ts = _current_window_start_ts()
        expected_slug = f"btc-updown-5m-{start_ts}"
        raw = _raw_market(expected_slug, start_ts=start_ts)

        with patch("polymarket.gamma_client.requests.get", return_value=_FakeResponse([raw])) as mock_get:
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        self.assertIsNotNone(result)
        self.assertEqual(result.slug, expected_slug)
        # Queried by exact slug, not a "most recently created" scan.
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params, {"slug": expected_slug})

    def test_start_date_comes_from_the_looked_up_window_not_gammas_raw_startDate_field(self):
        # Regression test for the reference-price-killing bug: start_date
        # must equal the window's real start (what we looked it up by), NOT
        # raw["startDate"] — which _raw_market() deliberately sets ~24h off
        # so this fails loudly if the wrong field is ever used again.
        start_ts = _current_window_start_ts()
        slug = f"btc-updown-5m-{start_ts}"
        raw = _raw_market(slug, start_ts=start_ts)

        with patch("polymarket.gamma_client.requests.get", return_value=_FakeResponse([raw])):
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        expected_start = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc)
        self.assertEqual(result.start_date, expected_start)
        self.assertEqual((result.end_date - result.start_date).total_seconds(), _WINDOW_SECONDS)

    def test_falls_through_to_the_next_window_when_current_slug_is_not_found(self):
        start_ts = _current_window_start_ts()
        next_start_ts = start_ts + _WINDOW_SECONDS
        next_slug = f"btc-updown-5m-{next_start_ts}"
        next_raw = _raw_market(next_slug, start_ts=next_start_ts)

        responses = [_FakeResponse([]), _FakeResponse([next_raw])]  # current: not found, next: found
        with patch("polymarket.gamma_client.requests.get", side_effect=responses) as mock_get:
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        self.assertIsNotNone(result)
        self.assertEqual(result.slug, next_slug)
        self.assertEqual(mock_get.call_count, 2)

    def test_falls_through_to_the_next_window_when_current_is_already_closed(self):
        start_ts = _current_window_start_ts()
        current_slug = f"btc-updown-5m-{start_ts}"
        current_raw = _raw_market(current_slug, start_ts=start_ts, closed=True)
        next_start_ts = start_ts + _WINDOW_SECONDS
        next_slug = f"btc-updown-5m-{next_start_ts}"
        next_raw = _raw_market(next_slug, start_ts=next_start_ts)

        responses = [_FakeResponse([current_raw]), _FakeResponse([next_raw])]
        with patch("polymarket.gamma_client.requests.get", side_effect=responses):
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        self.assertEqual(result.slug, next_slug)

    def test_returns_none_when_neither_current_nor_next_window_exists(self):
        with patch("polymarket.gamma_client.requests.get", return_value=_FakeResponse([])) as mock_get:
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 2)  # tried both candidate windows, no more

    def test_already_expired_window_is_rejected_even_if_not_marked_closed(self):
        # Gamma's "closed" flag can lag briefly behind reality — end_date in
        # the past must be rejected on its own regardless of that flag.
        start_ts = _current_window_start_ts() - 10 * _WINDOW_SECONDS
        slug = f"btc-updown-5m-{start_ts}"
        raw = _raw_market(slug, start_ts=start_ts, closed=False)

        # Only ever queried by the current/next candidate slugs, so an old
        # window like this wouldn't normally even be fetched — this test
        # instead exercises the end_date<=now guard directly by having BOTH
        # candidate lookups return this stale, still-"open" market.
        with patch("polymarket.gamma_client.requests.get", return_value=_FakeResponse([raw])):
            result = gamma_client.find_live_btc_updown_market("btc-updown-5m")

        self.assertIsNone(result)


def _resolution_raw_market(*, end_offset: float, closed: bool, outcome_prices: list[str]) -> dict:
    return {
        "slug": "btc-updown-5m-resolution-test",
        "endDate": _iso(dt.datetime.now(dt.timezone.utc).timestamp() + end_offset),
        "closed": closed,
        "outcomes": ["Up", "Down"],
        "outcomePrices": outcome_prices,
    }


def _resolve(raw_or_none):
    payload = [raw_or_none] if raw_or_none is not None else []
    with patch("polymarket.gamma_client.requests.get", return_value=_FakeResponse(payload)):
        return gamma_client.get_resolved_up_outcome("btc-updown-5m-resolution-test")


class TestGetResolvedUpOutcome(unittest.TestCase):
    """
    Regression coverage for a real live-confirmed bug: get_resolved_up_outcome()
    used to require Gamma's "closed" boolean to be true before trusting
    outcomePrices at all. Polling an actually-just-expired market every 5s
    for a full 2 minutes showed "closed" staying False the ENTIRE time while
    outcomePrices were already frozen at a decisive, perfectly unchanging
    value from the very first check — trading had genuinely halted, but
    "closed" never reflected it. Gating on "closed" made this function
    return None 100% of the time in production, forcing every settlement
    onto the unverified proxy_coinbase_feed fallback instead of Polymarket's
    own reported outcome (see main.py's _try_resolve_market resolution_source
    values) — a live run showed 0/9 settlements sourced as gamma_official.
    """

    def test_decisive_up_price_after_expiry_resolves_true_even_though_not_marked_closed(self):
        # This is the exact scenario observed live: closed=False, prices
        # already decisive and past end_date.
        raw = _resolution_raw_market(end_offset=-30, closed=False, outcome_prices=["0.995", "0.005"])
        self.assertTrue(_resolve(raw))

    def test_decisive_down_price_after_expiry_resolves_false_even_though_not_marked_closed(self):
        raw = _resolution_raw_market(end_offset=-120, closed=False, outcome_prices=["0.005", "0.995"])
        self.assertFalse(_resolve(raw))

    def test_still_works_when_gamma_does_mark_it_closed(self):
        # The fix removes the closed requirement, it doesn't invert it —
        # a market that IS marked closed with a decisive price still
        # resolves normally.
        raw = _resolution_raw_market(end_offset=-30, closed=True, outcome_prices=["0.99", "0.01"])
        self.assertTrue(_resolve(raw))

    def test_decisive_price_before_the_markets_own_end_date_is_not_trusted(self):
        # Critical safety case: a market still mid-window can show a
        # lopsided, decisive-looking price purely from strong conviction —
        # that's not a result yet, it can still reverse before real expiry.
        # main.py's settlement loop polls EVERY pending market (including
        # ones still live) every 5s, so this function must refuse on its
        # own, not rely on the caller to gate correctly.
        raw = _resolution_raw_market(end_offset=+60, closed=False, outcome_prices=["0.98", "0.02"])
        self.assertIsNone(_resolve(raw))

    def test_ambiguous_price_after_expiry_returns_none(self):
        raw = _resolution_raw_market(end_offset=-30, closed=False, outcome_prices=["0.55", "0.45"])
        self.assertIsNone(_resolve(raw))

    def test_market_not_found_returns_none(self):
        self.assertIsNone(_resolve(None))

    def test_missing_end_date_returns_none(self):
        raw = _resolution_raw_market(end_offset=-30, closed=False, outcome_prices=["0.99", "0.01"])
        del raw["endDate"]
        self.assertIsNone(_resolve(raw))


class TestInferWindowSeconds(unittest.TestCase):
    def test_five_minute_slug(self):
        self.assertEqual(gamma_client._infer_window_seconds("btc-updown-5m"), 300)

    def test_falls_back_to_default_when_no_minute_suffix_present(self):
        self.assertEqual(gamma_client._infer_window_seconds("bitcoin-up-or-down"), 300)


if __name__ == "__main__":
    unittest.main()