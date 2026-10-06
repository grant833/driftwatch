from dataclasses import replace
from datetime import UTC, datetime

from driftwatch.ledger import GENESIS, make_entry, verify_chain


def build(n=5):
    entries, prev = [], None
    for i in range(n):
        prev = make_entry(prev, "prediction", {"ticker": "ABC", "i": i, "p_up": 0.61},
                          now=datetime(2026, 10, 6, 14, 30, i, tzinfo=UTC))
        entries.append(prev)
    return entries


def test_chain_verifies():
    entries = build()
    assert entries[0].prev_hash == GENESIS
    assert verify_chain(entries) == (True, None)


def test_tampered_body_detected():
    entries = build()
    entries[2] = replace(entries[2], body=entries[2].body.replace("0.61", "0.91"))
    assert verify_chain(entries) == (False, 3)


def test_backdated_timestamp_detected():
    entries = build()
    entries[1] = replace(entries[1], recorded_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert verify_chain(entries) == (False, 2)


def test_deleted_entry_detected():
    entries = build()
    del entries[3]
    assert verify_chain(entries) == (False, 5)


def test_canonical_key_order_irrelevant():
    a = make_entry(None, "note", {"b": 1, "a": 2}, now=datetime(2026, 1, 1, tzinfo=UTC))
    b = make_entry(None, "note", {"a": 2, "b": 1}, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert a.hash == b.hash
