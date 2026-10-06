"""Phase 12 hostile stream torture — minimal witnessed set over real loopback TCP.

Subject: S (source/runtime).  Campaign: bots5-linux-v0.1-phase12-torture-20261006-01,
workstream phase12-runtime-provider.  This is deliberately NOT the withdrawn
14-scenario harness (DR-010..DR-022): six deterministic cases, each driving the
accepted stack — ``GenerationRequest`` -> ``BuiltinProviderRouter`` ->
``OpenAICompatibleStreamingBackend`` -> real loopback TCP — and each carrying a
two-sided reachability witness (the hostile server records exactly which bytes
it SENT; the client event stream records what the product did with them).

Every case consumes the router with ``aclosing`` semantics and classifies the
outcome from the emitted terminal events (``GenerationFailed`` /
``GenerationCompleted``), never from raised exceptions.  No skips of any kind.
Runs clean under ``-W error::ResourceWarning``: every server, handler task,
client and socket is closed and reaped deterministically on success AND failure
paths.  Witness records are appended as JSONL when ``BOTS5_TORTURE_WITNESSES``
is set (evidence runs set it; ordinary runs do not depend on it).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import aclosing

import httpx
import pytest

from bots5.core.generation import (
    GenerationCompleted,
    GenerationDelta,
    GenerationDispatched,
    GenerationFailed,
    GenerationMetadata,
    GenerationRequest,
)
from bots5.infrastructure.generation.router import BuiltinProviderRouter

from tests.test_provider_stream_cleanup import loopback_only  # noqa: F401  (accepted fixture)


_CAMPAIGN = "bots5-linux-v0.1-phase12-torture-20261006-01"
_WORKSTREAM = "phase12-runtime-provider"

_HEADERS = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
)


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


# A content-only delta (no finish, no usage): exercises the delta path only.
_PARTIAL = _sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
# The healthy completion shape: content + finish + model + usage in one chunk.
_COMPLETE = _sse(
    {
        "choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}],
        "model": "local/torture-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
)
_DONE = b"data: [DONE]\n\n"
_MALFORMED = b"data: {not-json\n\n"
_TAIL_GARBAGE = b"TAIL-GARBAGE-NOT-SSE \r\n"


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


async def _read_request(reader) -> None:
    """Consume the client's POST (headers + body) before staging the fault."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length:
        await reader.readexactly(length)


async def _await_client_teardown(reader, observations) -> None:
    """Witness how the client tore the connection down (EOF or reset)."""
    try:
        tail = await reader.read()
        observations.append("server_observed_client_eof" if tail == b"" else "server_observed_tail_bytes")
    except ConnectionError:
        observations.append("server_observed_client_reset")


async def _start_hostile_server(handler, observations):
    handler_tasks = set()

    async def wrapped(reader, writer):
        task = asyncio.current_task()
        handler_tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handler_tasks.discard(task)

    server = await asyncio.start_server(wrapped, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert isinstance(port, int) and port > 0
    return server, port, handler_tasks


async def _stop_hostile_server(server, handler_tasks) -> None:
    server.close()
    await server.wait_closed()
    await asyncio.gather(*tuple(handler_tasks), return_exceptions=True)


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Wrap the real loopback transport; witness the request and client socket."""

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


def _request(port, *, timeout_seconds=None) -> GenerationRequest:
    return GenerationRequest(
        attempt_id="attempt-torture",
        chat_id="chat-torture",
        user_message_id="message-torture",
        backend_id="openai_compatible_http",
        model="local/torture-model",
        prompt="hostile probe",
        provider_id="generic",
        provider_profile="generic",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout_seconds=timeout_seconds,
    )


def _router(capture) -> BuiltinProviderRouter:
    router = BuiltinProviderRouter()
    # Requests without a connection id resolve the transport under key "".
    router._transports[""] = capture
    return router


def _witness(case_id, **fields) -> None:
    path = os.environ.get("BOTS5_TORTURE_WITNESSES")
    if not path:
        return
    record = {
        "campaign": _CAMPAIGN,
        "workstream": _WORKSTREAM,
        "subject": "S",
        "case_id": case_id,
        "module": "tests/test_phase12_hostile_stream_torture.py",
        **fields,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def test_tr01_malformed_sse_reaches_parser_boundary(loopback_only):
    """TR-01: malformed SSE bytes are SENT by the server and parse into exactly
    one ProviderResponseError terminal event, with zero deltas."""
    observations = []

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(_HEADERS)
            writer.write(_chunk(_MALFORMED))
            await writer.drain()
            observations.append("server_sent_malformed_line")
            await _await_client_teardown(reader, observations)

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port))
            events = []
            try:
                async with aclosing(stream) as owned:
                    async for event in owned:
                        events.append(event)
                        if isinstance(event, (GenerationFailed, GenerationCompleted)):
                            break
            finally:
                await stream.aclose()
            await _stop_hostile_server(server, handler_tasks)
            return events, capture, stream
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    events, capture, stream = asyncio.run(scenario(), debug=True)
    # Reachability witness (server side): the malformed bytes were actually sent.
    assert observations == [
        "server_sent_malformed_line",
        "client_transport_close_exit",
        "server_observed_client_eof",
    ]
    # Reachability witness (client side): a malformed-JSON parse error can only
    # occur if those bytes reached the parser boundary.
    assert [type(event) for event in events] == [GenerationDispatched, GenerationFailed]
    failure = events[1]
    assert failure.error_type == "ProviderResponseError"
    assert failure.error_message == "malformed_provider_response"
    # Zero deltas: the malformed stream produced no content.
    assert not [event for event in events if isinstance(event, GenerationDelta)]
    # Durable classification (RP-F-05): a malformed response is
    # failed-but-received — the provider-side outcome is KNOWN under the
    # canonical Rule D-9 rule.  Direction pin: this deliberately flipped from
    # the pre-repair conservative True (known reported as unknown) to False.
    assert failure.remote_outcome_unknown is False
    # Clean ownership.
    assert stream.ag_frame is None and capture.closed is True
    _witness(
        "TR-01",
        subsystem="generation.streaming/provider-parser",
        lifecycle_point="stream body parse (pre-DONE)",
        fault_config={"scenario": "malformed_sse_line", "sent_bytes": _MALFORMED.decode().strip(), "framing": "chunked"},
        reachability_witness={
            "server_sent": "server_sent_malformed_line",
            "bytes_sent": _MALFORMED.decode().strip(),
            "client_parse_boundary": failure.error_type,
        },
        expected_postcondition="GenerationFailed(ProviderResponseError, remote_outcome_unknown=False — failed-but-received per RP-F-05) terminal event, zero deltas, connection torn down",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "error_type": failure.error_type,
            "remote_outcome_unknown": failure.remote_outcome_unknown,
            "teardown": observations,
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
        },
    )


def test_tr02_pre_done_disconnect_yields_terminal_failure(loopback_only):
    """TR-02: the provider disconnects BEFORE [DONE]; the durable terminal
    outcome is GenerationFailed with an unknown remote outcome."""
    observations = []

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(_HEADERS)
            writer.write(_chunk(_PARTIAL))
            await writer.drain()
            observations.append("server_sent_partial_then_closed_before_done")
            writer.close()  # abrupt hostile close before [DONE]

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port))
            events = []
            try:
                async with aclosing(stream) as owned:
                    async for event in owned:
                        events.append(event)
                        if isinstance(event, (GenerationFailed, GenerationCompleted)):
                            break
            finally:
                await stream.aclose()
            await _stop_hostile_server(server, handler_tasks)
            return events, capture, stream
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    events, capture, stream = asyncio.run(scenario(), debug=True)
    # Reachability witness: the partial chunk was sent, then the server closed
    # before [DONE]; the client saw a transport failure, not a clean end.
    assert observations[0] == "server_sent_partial_then_closed_before_done"
    assert observations[-1] == "client_transport_close_exit"
    # Durable terminal outcome: exactly one delta delivered, then the terminal
    # failure; never a completion.
    assert [type(event) for event in events] == [
        GenerationDispatched,
        GenerationDelta,
        GenerationFailed,
    ]
    assert events[1].text == "partial"
    failure = events[2]
    assert failure.error_type == "ProviderError"
    assert failure.error_message.startswith("provider_transport_error")
    assert failure.remote_outcome_unknown is True
    assert not [event for event in events if isinstance(event, GenerationCompleted)]
    # Clean ownership: client transport aclosed; the OS socket handle is
    # retrievable here because a response object existed before the disconnect.
    assert stream.ag_frame is None and capture.closed is True
    assert capture.socket is not None and capture.socket.fileno() == -1
    _witness(
        "TR-02",
        subsystem="generation.streaming/transport",
        lifecycle_point="mid-stream transport failure (pre-DONE disconnect)",
        fault_config={"scenario": "server_close_before_done", "sent_before_close": _PARTIAL.decode().strip()},
        reachability_witness={
            "server_sent": "server_sent_partial_then_closed_before_done",
            "client_transport_error": failure.error_message.split(":")[0],
        },
        expected_postcondition="GenerationFailed(ProviderError, remote_outcome_unknown=True) terminal event after the partial delta",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "error_type": failure.error_type,
            "remote_outcome_unknown": failure.remote_outcome_unknown,
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
            "client_socket_fd": capture.socket.fileno(),
        },
    )


def test_tr03_post_done_tail_produces_zero_events(loopback_only):
    """TR-03: garbage AFTER [DONE] is never parsed: the server proves the tail
    bytes were SENT, the client event stream proves they produced ZERO events."""
    observations = []

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(_HEADERS)
            writer.write(_chunk(_COMPLETE))
            writer.write(_chunk(_DONE))
            await writer.drain()
            writer.write(_chunk(_TAIL_GARBAGE))
            await writer.drain()
            observations.append("server_sent_done_then_tail_garbage")
            await _await_client_teardown(reader, observations)

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port))
            events = []
            # Consume to NATURAL end of stream: any event the parser produced
            # from the tail would have to appear here.
            async with aclosing(stream) as owned:
                async for event in owned:
                    events.append(event)
            await _stop_hostile_server(server, handler_tasks)
            return events, capture, stream
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    events, capture, stream = asyncio.run(scenario(), debug=True)
    # Reachability witness (server side): the tail bytes were provably SENT.
    assert observations[0] == "server_sent_done_then_tail_garbage"
    # Two-sided proof (client side): the stream ended at the completion with
    # exactly the pre-DONE events — the tail produced zero events.
    assert [type(event) for event in events] == [
        GenerationDispatched,
        GenerationMetadata,
        GenerationDelta,
        GenerationCompleted,
    ]
    assert events[1].returned_model == "local/torture-model"
    assert (events[1].prompt_tokens, events[1].completion_tokens, events[1].total_tokens) == (3, 2, 5)
    assert events[2].text == "partial"
    assert events[3].finish_reason == "stop"
    assert not [event for event in events if isinstance(event, GenerationFailed)]
    # Teardown: the client closed despite unread tail bytes (EOF, or RST when
    # the close races unread data — both are genuine teardown witnesses).
    assert observations[-2] == "client_transport_close_exit"
    assert observations[-1] in {"server_observed_client_eof", "server_observed_client_reset"}
    assert stream.ag_frame is None and capture.closed is True
    _witness(
        "TR-03",
        subsystem="generation.streaming/provider-parser",
        lifecycle_point="post-[DONE] response tail",
        fault_config={"scenario": "tail_garbage_after_done", "tail_bytes": _TAIL_GARBAGE.decode().strip(), "framing": "chunked"},
        reachability_witness={
            "server_sent": "server_sent_done_then_tail_garbage",
            "bytes_sent_after_done": _TAIL_GARBAGE.decode().strip(),
            "client_side": "stream ended naturally at GenerationCompleted",
        },
        expected_postcondition="GenerationCompleted terminal outcome; tail bytes parsed to zero events; connection torn down",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "failed_events": 0,
            "teardown": observations[-1],
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
        },
    )


def test_tr04_timeout_settles_deterministically(loopback_only):
    """TR-04a: a request deadline carried on GenerationRequest settles a
    never-responding server deterministically through the router stack."""
    observations = []

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            observations.append("server_read_request_never_responds")
            await _await_client_teardown(reader, observations)

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port, timeout_seconds=0.25))
            started = asyncio.get_running_loop().time()
            events = []
            try:
                async with aclosing(stream) as owned:
                    async for event in owned:
                        events.append(event)
                        if isinstance(event, (GenerationFailed, GenerationCompleted)):
                            break
            finally:
                await stream.aclose()
            elapsed = asyncio.get_running_loop().time() - started
            await _stop_hostile_server(server, handler_tasks)
            return events, capture, stream, elapsed
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    events, capture, stream, elapsed = asyncio.run(scenario(), debug=True)
    # Reachability witness: the server read the request and never responded;
    # the client settled anyway, inside the declared deadline's neighbourhood.
    assert observations == [
        "server_read_request_never_responds",
        "client_transport_close_exit",
        "server_observed_client_eof",
    ]
    assert [type(event) for event in events] == [GenerationDispatched, GenerationFailed]
    failure = events[1]
    assert failure.error_type == "ProviderError"
    assert failure.error_message.startswith("provider_transport_error")
    assert failure.remote_outcome_unknown is True
    assert 0.2 <= elapsed < 5.0
    assert stream.ag_frame is None and capture.closed is True
    _witness(
        "TR-04a",
        subsystem="generation.streaming/timeout-settlement",
        lifecycle_point="deadline expiry on a never-responding provider (router stack)",
        fault_config={"scenario": "server_never_responds", "generation_request_timeout_seconds": 0.25},
        reachability_witness={
            "server_sent": "server_read_request_never_responds",
            "settlement_error": failure.error_message.split(":")[0],
            "elapsed_seconds": round(elapsed, 3),
        },
        expected_postcondition="GenerationFailed(ProviderError, remote_outcome_unknown=True) terminal event within the declared deadline",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "error_type": failure.error_type,
            "remote_outcome_unknown": failure.remote_outcome_unknown,
            "elapsed_seconds": round(elapsed, 3),
            "teardown": observations,
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
        },
    )


def test_tr04_cancel_settles_deterministically(loopback_only):
    """TR-04b: cancelling the consumer mid-stream settles the whole ownership
    chain (generator -> backend -> provider -> client -> socket) deterministically."""
    observations = []
    events = []

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(_HEADERS)
            writer.write(_chunk(_PARTIAL))
            await writer.drain()
            observations.append("server_sent_partial_then_stalled")
            await _await_client_teardown(reader, observations)

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port))
            delta_seen = asyncio.Event()

            async def consume():
                async with aclosing(stream) as owned:
                    async for event in owned:
                        events.append(event)
                        if isinstance(event, GenerationDelta):
                            delta_seen.set()

            consumer = asyncio.create_task(consume(), name="torture-consumer")
            await asyncio.wait_for(delta_seen.wait(), 5)
            consumer.cancel()
            result = (await asyncio.gather(consumer, return_exceptions=True))[0]
            await _stop_hostile_server(server, handler_tasks)
            # Captured while the loop is still running: every surviving task
            # other than this scenario's own.
            leftover = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]
            return result, capture, stream, leftover
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    result, capture, stream, leftover = asyncio.run(scenario(), debug=True)
    assert isinstance(result, asyncio.CancelledError)
    # Reachability witness: the partial chunk was sent, the client consumed it,
    # then the consumer was cancelled while the stream was stalled open.
    assert observations == [
        "server_sent_partial_then_stalled",
        "client_transport_close_exit",
        "server_observed_client_eof",
    ]
    # Settlement: only the pre-cancel events exist; the whole chain unwound.
    assert [type(event) for event in events] == [GenerationDispatched, GenerationDelta]
    assert stream.ag_frame is None and capture.closed is True
    assert capture.socket is not None and capture.socket.fileno() == -1
    assert leftover == []
    _witness(
        "TR-04b",
        subsystem="generation.streaming/cancellation-settlement",
        lifecycle_point="consumer cancellation during an open stalled stream",
        fault_config={"scenario": "stall_after_partial", "cancel_after": "first GenerationDelta"},
        reachability_witness={
            "server_sent": "server_sent_partial_then_stalled",
            "client_teardown": observations[-1],
            "consumer_result": type(result).__name__,
        },
        expected_postcondition="consumer task ends cancelled; generator closed; transport closed; OS socket released; zero leftover tasks",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
            "client_socket_fd": capture.socket.fileno(),
            "teardown": observations[-1],
            "leftover_tasks": 0,
        },
    )


def test_tr05_healthy_control_owns_all_resources(loopback_only):
    """TR-05: the healthy control completes with content AND releases every
    owned task, transport and socket — the contrast baseline the withdrawn
    harness never proved (DR-018)."""
    observations = []
    # Baseline captured BEFORE any loop runs (module/process interpreter state).
    baseline_hooks = sys.get_asyncgen_hooks()

    async def scenario():
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(_HEADERS)
            writer.write(_chunk(_COMPLETE))
            writer.write(_chunk(_DONE))
            await writer.drain()
            observations.append("server_sent_complete_stream")
            await _await_client_teardown(reader, observations)

        server, port, handler_tasks = await _start_hostile_server(handler, observations)
        try:
            capture = _CaptureTransport(observations)
            stream = _router(capture).stream(_request(port))
            events = []
            async with aclosing(stream) as owned:
                async for event in owned:
                    events.append(event)
            await _stop_hostile_server(server, handler_tasks)
            leftover = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]
            return events, capture, stream, leftover
        except BaseException:
            await _stop_hostile_server(server, handler_tasks)
            raise

    events, capture, stream, leftover = asyncio.run(scenario(), debug=True)
    # Reachability witness: the server actually sent the complete stream.
    assert observations == [
        "server_sent_complete_stream",
        "client_transport_close_exit",
        "server_observed_client_eof",
    ]
    # Accepted event shape (cf. tests/test_provider_stream_cleanup.py:109).
    assert [type(event) for event in events] == [
        GenerationDispatched,
        GenerationMetadata,
        GenerationDelta,
        GenerationCompleted,
    ]
    assert events[1].returned_model == "local/torture-model"
    assert (events[1].prompt_tokens, events[1].completion_tokens, events[1].total_tokens) == (3, 2, 5)
    assert events[2].text == "partial"
    assert events[3].finish_reason == "stop"
    # Clean resource ownership: generator, transport, OS socket, tasks, hooks.
    assert stream.ag_frame is None and capture.closed is True
    assert capture.socket is not None and capture.socket.fileno() == -1
    assert leftover == []
    # asyncio.run restored the interpreter's hooks: nothing leaked out.
    assert sys.get_asyncgen_hooks() == baseline_hooks
    _witness(
        "TR-05",
        subsystem="generation.streaming/resource-ownership",
        lifecycle_point="healthy completion end-to-end (control case)",
        fault_config={"scenario": "healthy_control", "chunks": 2, "done": True},
        reachability_witness={
            "server_sent": "server_sent_complete_stream",
            "completed": events[3].finish_reason,
        },
        expected_postcondition="GenerationCompleted with content; generator/transport/socket released; zero leftover tasks; asyncgen hooks restored",
        observed_postcondition={
            "events": [type(event).__name__ for event in events],
            "finish_reason": events[3].finish_reason,
            "stream_ag_frame_none": stream.ag_frame is None,
            "client_transport_closed": capture.closed,
            "client_socket_fd": capture.socket.fileno(),
            "teardown": observations[-1],
            "leftover_tasks": 0,
            "asyncgen_hooks_restored": True,
        },
    )
