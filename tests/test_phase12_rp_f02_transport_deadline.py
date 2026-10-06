"""RP-F-02 repair regression: the declared completion deadline is enforced at transport.

Classified finding RP-F-02 (phase12 linux-v0.1-torture 20261006-01): ``CompletionRequest.timeout_seconds``
was declared (``providers/base.py``), plumbed from ``GenerationRequest.timeout_seconds``
(``infrastructure/generation/openai_compatible.py``), and read nowhere; every provider
client constructed ``httpx.AsyncClient(timeout=None, ...)`` so a hung connect or stalled
stream had no transport-level backstop below the application-level absolute deadline.

Repair (option a): the deadline is genuinely intended — it is plumbed from the caller —
so ``providers.base.transport_timeout`` maps a positive deadline onto
``httpx.Timeout(deadline)`` for every client that has a request context.
``providers/discovery.py`` has no request context (no caller-supplied deadline exists),
so it deliberately keeps ``timeout=None``; that disposition is pinned by
``test_discovery_keeps_unbounded_transport_without_request_context``.

Subject: S (source/runtime).  Loopback-only real-TCP; no external requests.
"""
from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest

from bots5.domain.provider import BackendType, CredentialSource, ProviderConnection, ProviderProfile
from bots5.errors import ProviderError
from bots5.providers.base import CompletionRequest, transport_timeout
from bots5.providers.discovery import OpenAICompatibleModelDiscoverer
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider

from tests.test_provider_stream_cleanup import loopback_only  # noqa: F401  (accepted fixture)


class _StubTransport(httpx.AsyncBaseTransport):
    """Offline transport: witnesses client construction without any socket."""

    def __init__(self, response):
        self.response = response
        self.closed = False

    async def handle_async_request(self, request):
        return self.response

    async def aclose(self):
        self.closed = True


@pytest.fixture
def capture_client_timeout(monkeypatch):
    """Record the ``timeout`` kwarg of every httpx.AsyncClient construction."""
    created = []
    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, *args, **kwargs):
            created.append(kwargs.get("timeout", "MISSING"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return created


def test_transport_deadline_mapping():
    """0.0/None/negative keep the legacy unbounded transport; positive maps all bounds."""
    assert transport_timeout(None) is None
    assert transport_timeout(0.0) is None
    assert transport_timeout(-1.0) is None
    mapped = transport_timeout(0.25)
    assert isinstance(mapped, httpx.Timeout)
    assert (mapped.connect, mapped.read, mapped.write, mapped.pool) == (0.25, 0.25, 0.25, 0.25)


_COMPLETE_RESPONSE = httpx.Response(
    200,
    json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
)
_STREAM_RESPONSE = httpx.Response(200, content=b"data: [DONE]\n\n")


@pytest.mark.parametrize(
    ("provider_cls", "method"),
    [
        (OpenAICompatibleProvider, "complete"),
        (OpenAICompatibleProvider, "stream"),
        (OpenRouterProvider, "complete"),
        (OpenRouterProvider, "stream"),
    ],
    ids=["openai_compatible.complete", "openai_compatible.stream",
         "openrouter.complete", "openrouter.stream"],
)
@pytest.mark.parametrize("deadline", [0.0, 0.25])
def test_provider_clients_receive_declared_deadline(capture_client_timeout, provider_cls, method, deadline):
    """Wiring witness: the client is CONSTRUCTED with the mapped deadline."""
    transport = _StubTransport(
        _COMPLETE_RESPONSE if method == "complete" else _STREAM_RESPONSE,
    )
    if provider_cls is OpenRouterProvider:
        adapter = provider_cls(
            "synthetic-local-only", base_url="http://127.0.0.1:9000/v1", _transport=transport,
        )
    else:
        adapter = provider_cls("http://127.0.0.1:9000/v1", _transport=transport)
    request = CompletionRequest("local/rpf02-model", "", "synthetic", 0.0, 32, deadline)

    async def scenario():
        if method == "complete":
            await adapter.complete(request)
        else:
            stream = adapter.stream(request)
            async for _chunk in stream:
                pass
            await stream.aclose()

    asyncio.run(scenario(), debug=True)
    assert capture_client_timeout, "client was never constructed"
    expected = None if deadline == 0.0 else httpx.Timeout(0.25)
    assert capture_client_timeout == [expected]
    assert transport.closed


def test_transport_deadline_fires_on_stalled_real_tcp_stream(loopback_only):
    """A real loopback server that never responds is cut off by the transport backstop.

    Pre-repair this scenario hung indefinitely (``AsyncClient(timeout=None)``);
    the ``asyncio.wait_for`` below is only a safety net so a reverted repair
    fails instead of hanging the suite.
    """
    observations = []
    connect = socket.socket.connect

    async def scenario():
        server_tasks = set()

        async def handler(reader, writer):
            task = asyncio.current_task()
            server_tasks.add(task)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length:
                    await reader.readexactly(length)
                observations.append("server_read_request_never_responds")
                try:
                    tail = await reader.read()
                    observations.append("server_observed_client_eof" if tail == b"" else "server_observed_tail")
                except ConnectionError:
                    observations.append("server_observed_client_reset")
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                server_tasks.discard(task)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        assert isinstance(port, int) and port > 0

        class _Capture(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport(retries=0)
                self.socket = None
                self.closed = False

            async def handle_async_request(self, request):
                assert request.url.host == "127.0.0.1"
                response = await self.inner.handle_async_request(request)
                self.socket = response.extensions["network_stream"].get_extra_info("socket")
                return response

            async def aclose(self):
                await self.inner.aclose()
                self.closed = True
                observations.append("client_transport_close_exit")

        capture = _Capture()
        adapter = OpenAICompatibleProvider(
            f"http://127.0.0.1:{port}/v1", _transport=capture,
        )
        request = CompletionRequest("local/rpf02-model", "", "probe", 0.0, 32, 0.25)
        started = time.monotonic()
        failure = None
        try:
            stream = adapter.stream(request)
            try:
                async for _chunk in stream:
                    pass
            finally:
                await stream.aclose()
        except ProviderError as error:
            failure = error
        elapsed = time.monotonic() - started
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(server_tasks), return_exceptions=True)
        return failure, elapsed, capture

    failure, elapsed, capture = asyncio.run(scenario(), debug=True)
    # Reachability witness: the transport backstop (not the harness) ended the wait.
    assert observations[0] == "server_read_request_never_responds"
    assert failure is not None and failure.__class__.__name__ == "ProviderError"
    assert str(failure).startswith("provider_transport_error")
    assert 0.2 <= elapsed < 5.0
    assert observations[-2:] == ["client_transport_close_exit", "server_observed_client_eof"]
    # Client-side closure witness: the capture transport is aclosed by the
    # client teardown.  The OS socket handle itself is not retrievable on
    # this stalled path BY CONSTRUCTION — the server never responds, so no
    # response object (and no "network_stream" extension) is ever produced
    # to read it from — and the peer-side EOF above is the kernel-level
    # witness that the client socket was actually closed.
    assert capture.closed is True


def test_discovery_keeps_unbounded_transport_without_request_context(capture_client_timeout):
    """Pinned RP-F-02 disposition: discovery has no caller deadline to wire.

    ``OpenAICompatibleModelDiscoverer.discover`` receives a connection, not a
    ``CompletionRequest``; no caller-supplied deadline exists in its context,
    so option (a) is inapplicable there and an invented default would be an
    unclassified behaviour change.  The transport stays unbounded by decision.
    """
    connection = ProviderConnection(
        id="connection-rpf02",
        name="RP-F-02 disposition probe",
        backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile.GENERIC,
        endpoint="http://127.0.0.1:9000/v1",
        credential_source=CredentialSource.NONE,
    )
    catalogue = httpx.Response(
        200,
        json={"data": [{"id": "local/discovered-model", "owned_by": "probe"}]},
    )
    discoverer = OpenAICompatibleModelDiscoverer(transport=_StubTransport(catalogue))

    models = asyncio.run(discoverer.discover(connection, None), debug=True)

    assert [model.provider_model_id for model in models] == ["local/discovered-model"]
    assert capture_client_timeout == [None]
