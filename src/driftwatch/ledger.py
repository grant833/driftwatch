"""Tamper-evident prediction ledger.

Each entry's hash covers its sequence number, timestamp, kind, canonical JSON body,
and the previous entry's hash. Changing any past entry breaks every hash after it.
Committing the daily head hash to a public git repo ("anchoring") gives an
external timestamp that proves predictions were made before outcomes were known.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

GENESIS = "0" * 64
LOCK_ID = 7_210_001  # advisory lock key for serialized appends


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ts_str(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def compute_hash(seq: int, recorded_at: datetime, kind: str, body: str, prev_hash: str) -> str:
    material = "\n".join([str(seq), ts_str(recorded_at), kind, body, prev_hash])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    recorded_at: datetime
    kind: str
    body: str
    prev_hash: str
    hash: str


def make_entry(prev: Entry | None, kind: str, payload: dict, now: datetime | None = None) -> Entry:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    seq = 1 if prev is None else prev.seq + 1
    prev_hash = GENESIS if prev is None else prev.hash
    body = canonical(payload)
    return Entry(seq, now, kind, body, prev_hash, compute_hash(seq, now, kind, body, prev_hash))


def verify_chain(entries: list[Entry]) -> tuple[bool, int | None]:
    """Return (ok, first_bad_seq)."""
    prev_hash, expected_seq = GENESIS, 1
    for e in entries:
        if e.seq != expected_seq or e.prev_hash != prev_hash:
            return False, e.seq
        if compute_hash(e.seq, e.recorded_at, e.kind, e.body, e.prev_hash) != e.hash:
            return False, e.seq
        prev_hash, expected_seq = e.hash, e.seq + 1
    return True, None


# ---------- database operations ----------

def _row_to_entry(r) -> Entry:
    return Entry(r[0], r[1], r[2], r[3], r[4], r[5])


def head(conn) -> Entry | None:
    r = conn.execute(
        "SELECT seq, recorded_at, kind, body, prev_hash, hash FROM ledger "
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return _row_to_entry(r) if r else None


def append(conn, kind: str, payload: dict) -> Entry:
    from psycopg.types.json import Jsonb

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        entry = make_entry(head(conn), kind, payload)
        conn.execute(
            "INSERT INTO ledger (seq, recorded_at, kind, body, payload, prev_hash, hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (entry.seq, entry.recorded_at, entry.kind, entry.body,
             Jsonb(json.loads(entry.body)), entry.prev_hash, entry.hash),
        )
    return entry


def verify(conn) -> tuple[bool, int | None, int]:
    rows = conn.execute(
        "SELECT seq, recorded_at, kind, body, prev_hash, hash FROM ledger ORDER BY seq"
    ).fetchall()
    entries = [_row_to_entry(r) for r in rows]
    ok, bad = verify_chain(entries)
    return ok, bad, len(entries)


def anchor(conn, anchors_dir: Path, today: date | None = None) -> Path | None:
    """Write today's head hash to a file meant to be committed to a public repo."""
    h = head(conn)
    if h is None:
        return None
    today = today or datetime.now(UTC).date()
    anchors_dir.mkdir(parents=True, exist_ok=True)
    path = anchors_dir / f"{today.isoformat()}.txt"
    path.write_text(f"seq={h.seq}\nrecorded_at={ts_str(h.recorded_at)}\nhash={h.hash}\n")
    return path
