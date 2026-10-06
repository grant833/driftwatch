"""Trading-universe guards. Every future order must pass through can_trade()."""
from __future__ import annotations


def can_trade(ticker: str, blocklist: frozenset[str]) -> tuple[bool, str]:
    t = ticker.strip().upper()
    if not t:
        return False, "empty ticker"
    if t in blocklist:
        return False, "blocklisted (UBS model overlap: wash-sale risk)"
    return True, "ok"
