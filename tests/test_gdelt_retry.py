import httpx

from driftwatch.ingest import gdelt


def test_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"timeline": []})

    monkeypatch.setattr(gdelt.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert gdelt.fetch_tone(client, "q", "24h") == {"timeline": []}
    assert calls["n"] == 3


def test_gives_up_after_all_retries(monkeypatch):
    monkeypatch.setattr(gdelt.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    try:
        gdelt.fetch_tone(client, "q", "24h")
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError:
        pass


def test_retries_network_errors(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("Name or service not known")
        return httpx.Response(200, json={"timeline": []})

    monkeypatch.setattr(gdelt.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert gdelt.fetch_tone(client, "q", "24h") == {"timeline": []}
