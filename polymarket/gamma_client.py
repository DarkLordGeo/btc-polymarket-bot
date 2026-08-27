"""
Read-only client for Polymarket's Gamma API (market metadata/discovery).
No auth required — this only reads public market listings.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class MarketInfo:
    slug: str
    question: str
    condition_id: str
    start_date: dt.datetime | None
    end_date: dt.datetime
    up_token_id: str
    down_token_id: str
    up_price: float | None  # implied probability of "Up", from outcomePrices
    down_price: float | None
    raw: dict


def _parse_end_date(s: str) -> dt.datetime:
    # Gamma returns ISO-8601 with a trailing "Z".
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# Candidate field names for a structured numeric strike/reference price
# ("price to beat"), IF Polymarket ever starts exposing one for these
# markets. As of this writing, this was checked directly against
# Polymarket's documented Gamma API market fields (question, outcomes,
# outcomePrices, clobTokenIds, enableOrderBook, acceptingOrders, closed,
# endDate, conditionId, active, slug, startDate) plus independent published
# research into how the "price to beat" for BTC up/down markets is actually
# determined — and NONE of them expose it as a structured field. Client code
# is expected to reconstruct it itself from an external price feed at market
# open. This function is therefore a forward-compatible no-op today (it will
# correctly return None for every real market), kept so that if Polymarket
# adds a real field later, main.py picks it up automatically without a code
# change. Do not assume any of these key names are real without
# re-verifying against a live raw market response first.
_STRUCTURED_STRIKE_CANDIDATE_KEYS = (
    "strikePrice", "priceToBeat", "referencePrice", "openPrice", "startPrice",
)


def extract_structured_strike_price(raw: dict) -> float | None:
    """
    Best-effort, forward-compatible scan of a raw Gamma market dict for a
    structured numeric strike/reference price — see
    _STRUCTURED_STRIKE_CANDIDATE_KEYS above for why this returns None today
    for every real market. Deliberately does NOT parse the human-readable
    `question` string for a dollar figure — that's a much less reliable
    signal than a real structured field, and this project should not invent
    one kind of guess (question parsing) to replace another (current BTC
    price) without solid evidence it's actually reliable.
    """
    if not raw:
        return None
    for key in _STRUCTURED_STRIKE_CANDIDATE_KEYS:
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


_WINDOW_MINUTES_RE = re.compile(r"(\d+)m(?:-|$)")


def _infer_window_seconds(slug_contains: str, default_seconds: int = 300) -> int:
    """
    Pull the window length out of e.g. "btc-updown-5m" -> 300. Falls back to
    5 minutes (this project's only real target today) if slug_contains ever
    stops carrying an explicit "<N>m" — see config.py's own comments on
    MARKET_SLUG_CONTAINS for why guessing at Polymarket's naming instead of
    verifying it is exactly the mistake that produced a prior incident here.
    """
    match = _WINDOW_MINUTES_RE.search(slug_contains)
    if not match:
        return default_seconds
    return int(match.group(1)) * 60


def _fetch_raw_market_by_slug(slug: str, timeout: float) -> dict | None:
    resp = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=timeout)
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def _build_market_info(raw: dict, *, slug: str, start_ts: float, end_date: dt.datetime) -> MarketInfo | None:
    import json

    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcome_prices = raw.get("outcomePrices")
    if isinstance(outcome_prices, str):
        outcome_prices = json.loads(outcome_prices)
    outcomes = raw.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)

    if not token_ids or len(token_ids) < 2 or not outcomes:
        return None

    # Outcomes are typically ["Up", "Down"] in that order, but don't assume —
    # match by label.
    up_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "up"), 0)
    down_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "down"), 1)

    def _price_at(idx: int) -> float | None:
        if not outcome_prices or idx >= len(outcome_prices):
            return None
        try:
            return float(outcome_prices[idx])
        except (TypeError, ValueError):
            return None

    return MarketInfo(
        slug=slug,
        question=raw.get("question", slug),
        condition_id=raw.get("conditionId", ""),
        # Deliberately NOT parsed from raw["startDate"] — confirmed live
        # that Gamma's own "startDate" field is roughly when the market
        # document was PUBLISHED (Polymarket appears to pre-create these
        # windows up to ~24h ahead of when they actually open), not when its
        # 5-minute trading window actually starts. Using it here silently
        # fed resolve_reference_price() a timestamp ~24h away from the real
        # window open, so it could never find a BTC sample within
        # REFERENCE_PRICE_MAX_STALENESS_SEC and every market was skipped as
        # missing_reference_price — with no error, just quiet non-trading.
        # `start_ts` is the timestamp WE used to look this market up (the
        # slug's own embedded window-start), confirmed against two live
        # samples to equal end_date minus the window length exactly — that
        # is the trustworthy value.
        start_date=dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc),
        end_date=end_date,
        up_token_id=token_ids[up_idx],
        down_token_id=token_ids[down_idx],
        up_price=_price_at(up_idx),
        down_price=_price_at(down_idx),
        raw=raw,
    )


def find_live_btc_updown_market(slug_contains: str, timeout: float = 8.0) -> MarketInfo | None:
    """
    Return the currently-open BTC "up or down" market, or None if none is
    live right now (there can be a short gap between windows).

    These 5-minute windows are cleanly aligned to window-length boundaries
    on the Unix epoch and follow a fully predictable slug shape:
    "<slug_contains>-<window_start_unix_ts>" (confirmed directly against two
    live markets fetched at different times — see the regression tests).
    That means the live window can be looked up directly by its exact slug,
    computed from the wall clock, rather than searched for.

    This deliberately does NOT ask Gamma for "the most recently created
    active markets" and scan/page through them (an earlier version of this
    function did). That approach has a real, confirmed failure mode:
    Polymarket appears to pre-create each window's market document roughly
    a day before that window actually opens. Sorting by creation time
    surfaces TOMORROW's just-created windows before TODAY's actual live one
    (created ~24h ago, and by now far back in creation order) — no amount
    of paging fixes that, because it's the wrong sort key entirely, not a
    depth problem. In one live incident this silently locked the bot onto a
    window opening in ~20 hours, which then correctly refused to trade
    (missing_reference_price) but never found the real live window either.
    """
    window_seconds = _infer_window_seconds(slug_contains)
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = now.timestamp()
    current_start_ts = int(now_ts // window_seconds) * window_seconds

    # Try the window covering "now" first, then the next one — right at a
    # rollover boundary the "current" slug can be a few seconds from being
    # closed (or, rarely, not yet published), and the next window is the
    # correct thing to pick up in that gap rather than reporting no market.
    for start_ts in (current_start_ts, current_start_ts + window_seconds):
        slug = f"{slug_contains}-{start_ts}"
        raw = _fetch_raw_market_by_slug(slug, timeout=timeout)
        if raw is None or raw.get("closed"):
            continue
        try:
            end_date = _parse_end_date(raw["endDate"])
        except (KeyError, ValueError):
            continue
        if end_date <= now:
            continue  # already expired, Gamma's "active"/"closed" flags can lag briefly
        market = _build_market_info(raw, slug=slug, start_ts=start_ts, end_date=end_date)
        if market is not None:
            return market

    return None


def get_resolved_up_outcome(slug: str, timeout: float = 8.0) -> bool | None:
    """
    Best-effort check of how a specific (now-expired) market resolved, by
    re-fetching it and reading outcomePrices, which collapse to a decisive
    (>=0.9 or <=0.1) value once settled. Returns None if it can't tell yet
    (settlement can lag a bit after expiry) — callers should have a
    fallback.

    Deliberately does NOT require Gamma's own "closed" boolean to be true
    first (an earlier version of this function did). Confirmed live, by
    polling an just-expired market every 5s for 2 full minutes: "closed"
    stayed False the entire time, while outcomePrices were ALREADY frozen at
    a decisive, perfectly unchanging value ("0.005"/"0.995" — identical
    across all 24 polls) from the very first check. Trading had clearly
    halted and the market had resolved in every practical sense; "closed" is
    just an unreliable/laggy signal for this market type via this endpoint.
    Gating on it made this function return None 100% of the time in
    production, silently forcing every settlement onto the unverified
    proxy_coinbase_feed fallback (see main.py's _try_resolve_market) instead
    of Polymarket's own reported outcome, even though that outcome was
    sitting right there in outcomePrices the whole time.

    What this DOES still require: the market's own endDate must have
    already passed. A decisive-looking price WHILE a market is still
    actively trading is strong conviction, not a result — it can still
    reverse before actual expiry. main.py's caller only calls this after a
    market's window has ended anyway, but that's enforced again here too so
    this function is safe to call on its own, from any caller, at any time.
    """
    import json

    resp = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=timeout)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    m = results[0]

    try:
        end_date = _parse_end_date(m["endDate"])
    except (KeyError, ValueError):
        return None
    if dt.datetime.now(dt.timezone.utc) <= end_date:
        return None  # still live — a decisive-looking price here isn't a result yet

    outcome_prices = m.get("outcomePrices")
    if isinstance(outcome_prices, str):
        outcome_prices = json.loads(outcome_prices)
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not outcome_prices or not outcomes:
        return None

    up_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "up"), 0)
    try:
        up_price = float(outcome_prices[up_idx])
    except (TypeError, ValueError, IndexError):
        return None

    if up_price >= 0.9:
        return True
    if up_price <= 0.1:
        return False
    return None  # not conclusively settled yet