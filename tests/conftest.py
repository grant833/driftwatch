import os

import pytest

URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def conn(monkeypatch):
    """Fresh schema per test (only when TEST_DATABASE_URL is set; CI provides one)."""
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg

    from driftwatch import db
    monkeypatch.setenv("DRIFTWATCH_HOME", os.path.dirname(os.path.dirname(__file__)))
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("""DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
                EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
            END LOOP; END $$""")
        db.apply_schema(c)
        yield c
