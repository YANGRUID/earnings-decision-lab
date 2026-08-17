import logging

import httpx
import pytest

from observability.http_client import new_http_client


@pytest.fixture
def mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def test_logs_one_line_per_call_with_duration(mock_transport, caplog):
    client = new_http_client(transport=mock_transport)
    with caplog.at_level(logging.INFO, logger="http.client"):
        response = client.get("https://example.com/v1/thing")

    assert response.status_code == 200
    records = [r for r in caplog.records if r.name == "http.client"]
    assert len(records) == 1
    record = records[0]
    assert record.message == "outbound http call"
    assert record.host == "example.com"
    assert record.method == "GET"
    assert record.path == "/v1/thing"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, float)
    assert record.duration_ms >= 0


def test_preserves_caller_supplied_event_hooks(mock_transport):
    seen = []

    def custom_hook(response: httpx.Response) -> None:
        seen.append(response.status_code)

    client = new_http_client(transport=mock_transport, event_hooks={"response": [custom_hook]})
    client.get("https://example.com/v1/thing")

    assert seen == [200]
