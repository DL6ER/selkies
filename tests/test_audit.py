# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tests for :mod:`selkies.audit`.

The audit module is best-effort and fire-and-forget. The tests below
verify the contractual promises that the operator-facing
``docs/audit-webhook.md`` makes:

* with the URL empty, :func:`audit.emit` is a strict no-op and never
  touches the network;
* with the URL set, :meth:`AuditClient.emit` schedules a POST on the
  running event loop and returns immediately;
* HTTP failures (4xx, 5xx, timeouts, connection refused) are absorbed
  into log lines and never raised into the caller;
* the JSON payload matches the documented schema.

The suite is intentionally small enough to read in one sitting. It
runs against the real :mod:`aiohttp` stack via :mod:`aioresponses`
rather than mocking it ad-hoc, so the test contract is "selkies.audit
calls aiohttp the way aiohttp expects to be called", not "selkies.audit
calls our private mock the way we hoped".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import aiohttp
import pytest
from aioresponses import aioresponses

from selkies import audit


# Single shared URL used across the suite. The value is a syntactically
# valid HTTPS URL so aiohttp does not reject it before the request is
# dispatched; aioresponses intercepts it before any real network call.
WEBHOOK_URL = "https://audit.example.test/sink"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Ensure each test sees a fresh module-level client."""
    audit._default = None
    yield
    audit._default = None


@pytest.mark.asyncio
async def test_emit_is_noop_when_url_empty():
    """Empty URL must not schedule a task and must not raise."""
    audit.configure(url="")
    assert not audit.is_enabled()
    # The call returns synchronously and does not need an event loop
    # to do anything meaningful.
    audit.emit("clipboard.send", size_bytes=1, mime_type="text/plain")
    # No background tasks were created.
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


@pytest.mark.asyncio
async def test_emit_posts_documented_schema():
    """Happy path: emit() posts {event, ts, ...fields} to the URL."""
    audit.configure(url=WEBHOOK_URL)
    with aioresponses() as mocked:
        mocked.post(WEBHOOK_URL, status=204)
        audit.emit(
            "clipboard.send",
            size_bytes=42,
            mime_type="text/plain",
        )
        # emit() returns immediately; the POST is a background task.
        # Wait one event-loop turn so the task actually runs.
        await asyncio.sleep(0.05)

        requests = mocked.requests[("POST", aiohttp.client.URL(WEBHOOK_URL))]
        assert len(requests) == 1
        body = json.loads(requests[0].kwargs["json"]) if isinstance(
            requests[0].kwargs["json"], str
        ) else requests[0].kwargs["json"]
        assert body["event"] == "clipboard.send"
        assert body["size_bytes"] == 42
        assert body["mime_type"] == "text/plain"
        # ts is RFC3339 UTC.
        ts = body["ts"]
        assert ts.endswith("+00:00") or ts.endswith("Z")
        # And actually parseable.
        datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_emit_sends_bearer_token_when_configured():
    """Authorization header carries the configured token."""
    audit.configure(url=WEBHOOK_URL, token="abc123")
    with aioresponses() as mocked:
        mocked.post(WEBHOOK_URL, status=204)
        audit.emit("clipboard.send", size_bytes=1)
        await asyncio.sleep(0.05)
        # aioresponses captures the session-level headers.
        client = audit._default._session  # type: ignore[union-attr]
        assert client is not None
        assert client.headers.get("Authorization") == "Bearer abc123"


@pytest.mark.asyncio
async def test_emit_swallows_http_5xx(caplog):
    """A 500 from the collector logs WARN and does not raise."""
    audit.configure(url=WEBHOOK_URL)
    with aioresponses() as mocked:
        mocked.post(WEBHOOK_URL, status=500, body="boom")
        with caplog.at_level(logging.WARNING, logger="audit"):
            audit.emit("clipboard.send", size_bytes=1)
            await asyncio.sleep(0.05)
        # One WARN line referencing the 500 status.
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("500" in r.getMessage() for r in warns)


@pytest.mark.asyncio
async def test_emit_swallows_connection_error(caplog):
    """A connection refused / DNS-fail also collapses to WARN."""
    audit.configure(url=WEBHOOK_URL)
    with aioresponses() as mocked:
        mocked.post(WEBHOOK_URL, exception=aiohttp.ClientConnectionError("nope"))
        with caplog.at_level(logging.WARNING, logger="audit"):
            audit.emit("file.upload.end", filename="a.bin", size_bytes=10)
            await asyncio.sleep(0.05)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("client error" in r.getMessage().lower() for r in warns)


@pytest.mark.asyncio
async def test_emit_without_event_loop_drops_silently(caplog):
    """If there is no running loop, emit() must not crash."""
    audit.configure(url=WEBHOOK_URL)
    # Step out of the test's running loop by running emit() on a new
    # thread - asyncio.get_running_loop() raises RuntimeError there
    # and audit.emit() must catch it.
    import threading

    holder: dict = {"err": None}

    def _no_loop():
        try:
            audit.emit("clipboard.send", size_bytes=1)
        except Exception as exc:  # noqa: BLE001
            holder["err"] = exc

    t = threading.Thread(target=_no_loop)
    t.start()
    t.join(timeout=1)
    assert holder["err"] is None


@pytest.mark.asyncio
async def test_close_releases_session():
    """close() empties the module-level client and closes the session."""
    audit.configure(url=WEBHOOK_URL)
    with aioresponses() as mocked:
        mocked.post(WEBHOOK_URL, status=204)
        audit.emit("clipboard.send", size_bytes=1)
        await asyncio.sleep(0.05)
        # Session is alive after first emit.
        session = audit._default._session  # type: ignore[union-attr]
        assert session is not None
        assert not session.closed
    await audit.close()
    assert audit._default is None
    assert session.closed
