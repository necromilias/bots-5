"""RP-F-01 repair regression: backend uncertainty classification aligns with Rule D-9.

Classified finding RP-F-01 (phase12 linux-v0.1-torture 20261006-01): the
streaming backend kept a parallel status heuristic (``status_code >= 500``)
that classified 408/429 as a KNOWN remote outcome, while the canonical rule
(``ProviderHttpError.definitive_rejection`` / ``provider_side_outcome_unknown``)
explicitly leaves 408/429 provider-side outcomes UNKNOWN.  After the repair the
backend delegates to the canonical classification, so both classifiers must
agree for every 4xx/5xx status and 408/429 must surface as
``remote_outcome_unknown=True`` terminal events.

Subject: S (source/runtime).  Loopback-only real-TCP; no external requests.
"""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from bots5.core.generation import GenerationDispatched, GenerationFailed, GenerationRequest
from bots5.errors import (
    ContextAdmissionError,
    ProviderError,
    ProviderHttpError,
    ProviderResponseError,
    ProviderTimeoutError,
    provider_side_outcome_unknown,
)
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend

from tests.test_provider_stream_cleanup import loopback_only  # noqa: F401  (accepted fixture)


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex

_REPO = "bots5-linux-v0.1-phase12-torture-20261006-01"


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Wrap the real loopback transport and witness the client socket."""

    def __init__(self, observations):
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.observations = observations
        self.socket = None
        self.closed = False

    async def handle_async_request(self, request):
        assert request.url.host == "127.0.0.1" and request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        response = await self.inner.handle_async_request(request)
        self.socket = response.extensions["network_stream"].get_extra_info("socket")
        return response

    async def aclose(self):
        await self.inner.aclose()
        self.closed = True
        self.observations.append("client_transport_close_exit")


async def _read_request(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length:
        await reader.readexactly(length)
    return head


async def _await_client_eof(reader, observations):
    """Witness the client tearing the connection down (EOF or reset)."""
    try:
        tail = await reader.read()
        observations.append("server_observed_client_eof" if tail == b"" else "server_observed_tail")
    except ConnectionError:
        observations.append("server_observed_client_reset")


def _request(port):
    return GenerationRequest(
        attempt_id="attempt-rpf01",
        chat_id="chat-rpf01",
        user_message_id="message-rpf01",
        backend_id="openai_compatible_http",
        model="local/rpf01-model",
        prompt="hostile status probe",
        provider_id="generic",
        provider_profile="generic",
        base_url=f"http://127.0.0.1:{port}/v1",
    )


def test_provider_http_classifier_matches_canonical_rule_for_every_4xx_and_5xx_status():
    """The backend keeps no parallel heuristic: every 4xx/5xx status agrees."""
    classify = OpenAICompatibleStreamingBackend._provider_failure_is_uncertain
    disagreements = []
    for status in range(400, 600):
        error = ProviderHttpError(status, "controlled probe")
        if classify(error) is not provider_side_outcome_unknown(error):
            disagreements.append(status)
    assert disagreements == []
    # The classified contradiction is exactly what must now hold.
    assert classify(ProviderHttpError(408, "controlled probe")) is True
    assert classify(ProviderHttpError(429, "controlled probe")) is True
    assert classify(ProviderHttpError(400, "controlled probe")) is False
    assert classify(ProviderHttpError(500, "controlled probe")) is True
    # The ContextAdmissionError short-circuit is preserved by the repair.
    assert classify(ContextAdmissionError("controlled probe")) is False
    # Unclassified failures stay conservatively ambiguous.
    assert classify(ProviderError("controlled probe")) is True
    assert classify(ProviderTimeoutError("controlled probe")) is True
    # RP-F-05: ProviderResponseError is failed-but-received — a KNOWN remote
    # outcome under the canonical rule.  Direction pin: this deliberately
    # flipped from the pre-repair conservative True (a known outcome reported
    # unknown) to False — the opposite, dangerous direction of RP-F-01.
    assert classify(ProviderResponseError("controlled probe")) is False


@pytest.mark.parametrize(
    ("status", "expected_unknown"),
    [(400, False), (408, True), (429, True), (500, True)],
)
def test_real_tcp_http_status_terminal_event_matches_canonical_rule(
    loopback_only, status, expected_unknown
):
    """Real-TCP non-200 streaming settles with the canonical unknown flag."""
    observations = []
    sent = []

    async def scenario():
        handler_tasks = set()

        async def handler(reader, writer):
            task = asyncio.current_task()
            handler_tasks.add(task)
            try:
                await _read_request(reader)
                body = json.dumps({"error": {"message": "hostile status", "code": status}}).encode()
                writer.write(
                    f"HTTP/1.1 {status} Hostile\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n".encode()
                )
                writer.write(body)
                await writer.drain()
                sent.append({"status_line": f"HTTP/1.1 {status}", "body_bytes": len(body)})
                observations.append(f"server_sent_http_{status}")
                await _await_client_eof(reader, observations)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                handler_tasks.discard(task)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        assert isinstance(port, int) and port > 0
        try:
            capture = _CaptureTransport(observations)
            from bots5.infrastructure.generation.router import BuiltinProviderRouter

            router = BuiltinProviderRouter()
            router._transports[""] = capture
            stream = router.stream(_request(port))
            events = []
            try:
                async for event in stream:
                    events.append(event)
                    if isinstance(event, GenerationFailed):
                        break
            finally:
                await stream.aclose()
            return events, capture
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*tuple(handler_tasks), return_exceptions=True)

    events, capture = asyncio.run(scenario(), debug=True)

    # Reachability witness: the server actually sent the hostile status.
    assert observations == [
        f"server_sent_http_{status}",
        "client_transport_close_exit",
        "server_observed_client_eof",
    ]
    assert sent and sent[0]["status_line"] == f"HTTP/1.1 {status}"
    # Durable terminal outcome: exactly one failure event, no deltas, no
    # completion, carrying the canonical unknown classification.
    assert [type(event) for event in events] == [GenerationDispatched, GenerationFailed]
    failure = events[1]
    assert failure.attempt_id == "attempt-rpf01"
    assert failure.error_type == "ProviderHttpError"
    assert f"status={status}" in failure.error_message
    assert failure.remote_outcome_unknown is expected_unknown
    # Resource ownership: the client transport closed and the OS socket is gone.
    assert capture.closed and capture.socket.fileno() == -1
