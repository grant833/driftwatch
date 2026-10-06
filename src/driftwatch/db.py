from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .config import home


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True)


def apply_schema(conn: psycopg.Connection) -> None:
    sql = Path(home() / "db" / "schema.sql").read_text()
    conn.execute(sql)


def insert_news(conn: psycopg.Connection, item: dict, via: str = "stream") -> bool:
    row = conn.execute(
        """
        INSERT INTO news_items (source, external_id, headline, summary, content, symbols,
                                url, author, published_at, updated_at, via, raw)
        VALUES (%(source)s, %(external_id)s, %(headline)s, %(summary)s, %(content)s,
                %(symbols)s, %(url)s, %(author)s, %(published_at)s, %(updated_at)s,
                %(via)s, %(raw)s)
        ON CONFLICT (source, external_id) DO NOTHING
        RETURNING id
        """,
        {**item, "via": via, "raw": Jsonb(item["raw"])},
    ).fetchone()
    return row is not None


def latest_news_time(conn: psycopg.Connection, source: str):
    row = conn.execute(
        "SELECT max(published_at) FROM news_items WHERE source = %s", (source,)
    ).fetchone()
    return row[0] if row else None


def insert_filing(conn: psycopg.Connection, f: dict) -> bool:
    row = conn.execute(
        """
        INSERT INTO filings (accession, form_type, cik, ticker, company, role, items,
                             filed_at, url, raw)
        VALUES (%(accession)s, %(form_type)s, %(cik)s, %(ticker)s, %(company)s, %(role)s,
                %(items)s, %(filed_at)s, %(url)s, %(raw)s)
        ON CONFLICT (accession) DO NOTHING
        RETURNING accession
        """,
        {**f, "raw": Jsonb(f["raw"])},
    ).fetchone()
    return row is not None


def insert_tone(conn: psycopg.Connection, query_name: str, points) -> int:
    n = 0
    for bucket_at, tone in points:
        row = conn.execute(
            """
            INSERT INTO gdelt_tone (query_name, bucket_at, tone) VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING RETURNING 1
            """,
            (query_name, bucket_at, tone),
        ).fetchone()
        n += row is not None
    return n
