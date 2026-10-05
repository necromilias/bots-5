"""Explicit provider ownership, including the supported qasync/TCP boundary."""
from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import weakref

import anyio
import httpx
import pytest

from bots5.core.errors import StateError
from bots5.core.execution import ExecutionManager
from bots5.core.generation import GenerationCompleted, GenerationDelta, GenerationDispatched
from bots5.domain.provider import (
    BackendType, CapabilityFact, CapabilityOverride, CapabilitySource,
    CapabilityState, CredentialSource, GenerationSettings, ProviderProfile,
)
from bots5.infrastructure.generation.router import BuiltinProviderRouter
from bots5.infrastructure.secrets import FakeSecretStore
from bots5.errors import ProviderError
from bots5.providers.base import CompletionRequest
from bots5.providers.openai_compatible import OpenAICompatibleProvider, _owned_response_lines
from bots5.providers.openrouter import OpenRouterProvider
from tests._authority_test_support import SQLiteAppStateStore
from tests.test_phase5_provider_model import _configured_application


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex


@pytest.fixture
def loopback_only(monkeypatch):
    """Permit the explicitly local regression, retaining the external guard."""
    def connect(self, address):
        assert isinstance(address, tuple) and address[0] == "127.0.0.1"
        return _CONNECT(self, address)

    def connect_ex(self, address):
        assert isinstance(address, tuple) and address[0] == "127.0.0.1"
        return _CONNECT_EX(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


def _run(scenario, loop_kind="asyncio"):
    if loop_kind == "asyncio":
        return asyncio.run(scenario, debug=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from qasync import QEventLoop

    qt = QApplication.instance() or QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    loop = QEventLoop(qt)
    loop.set_debug(True)
    asyncio.set_event_loop(loop)
    try:
        with loop:
            return loop.run_until_complete(scenario)
    finally:
        asyncio.set_event_loop(None)


@pytest.mark.parametrize("provider", ["openrouter", "generic"])
def test_backend_failure_terminal_event_closes_owned_router(provider):
    from bots5.core.application import _settle_generation_stream
    from bots5.core.generation import GenerationFailed, GenerationMetadata, GenerationRequest
    from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
    from bots5.providers.base import CompletionStreamEvent

    async def scenario():
        provider_closed = asyncio.Event()

        class Provider:
            async def stream(self, request):
                try:
                    yield CompletionStreamEvent(text="partial", returned_model="local/returned",
                        request_id="local-request", prompt_tokens=3, completion_tokens=2, total_tokens=5)
                    raise ProviderError("controlled sanitized provider failure")
                finally:
                    provider_closed.set()

        backend = OpenAICompatibleStreamingBackend(Provider(), provider_id=provider,
            base_url="http://127.0.0.1:9000/v1")
        generators = {}
        _observe_generator(backend, "backend", generators)
        _observe_generator(backend._provider, "provider", generators)
        router = BuiltinProviderRouter()
        router._openai_backend = lambda request: backend
        request = GenerationRequest(attempt_id="local-attempt", chat_id="local-chat",
            user_message_id="local-message", backend_id="openai_compatible_http",
            model="local/model", prompt="local failure", provider_id=provider,
            provider_profile=provider, base_url="http://127.0.0.1:9000/v1")
        stream = router.stream(request)
        events = []
        while True:
            event = await anext(stream)
            events.append(event)
            if isinstance(event, GenerationFailed):
                break
        assert [type(event) for event in events] == [GenerationDispatched,
            GenerationMetadata, GenerationDelta, GenerationFailed]
        assert events[1].returned_model == "local/returned"
        assert events[1].request_id == "local-request"
        assert (events[1].prompt_tokens, events[1].completion_tokens, events[1].total_tokens) == (3, 2, 5)
        assert events[2].text == "partial"
        assert event == GenerationFailed("local-attempt", "ProviderError",
            "controlled sanitized provider failure", True)
        assert provider_closed.is_set()
        manager = ExecutionManager()
        release = manager.start(_settle_generation_stream(stream, None, child_consumed=True),
            name="local-terminal-failure-cleanup", cleanup=True)
        await manager.shutdown()
        assert release.done() and release.exception() is None
        assert stream.ag_frame is None
        assert all(ref() is None or ref().ag_frame is None for ref in generators.values())
        assert not manager._tasks and not manager._cleanup_failures
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    _run(scenario())


def test_shutdown_owns_cleanup_handed_off_by_cancelled_work():
    async def scenario():
        manager = ExecutionManager()
        work_started, release_started, release = (asyncio.Event() for _ in range(3))

        async def cleanup():
            release_started.set()
            await release.wait()

        async def work():
            try:
                work_started.set()
                await asyncio.Event().wait()
            finally:
                manager.start(cleanup(), name="owned-release", cleanup=True)

        manager.start(work(), name="owned-work")
        await work_started.wait()
        shutdown = asyncio.create_task(manager.shutdown())
        await release_started.wait()
        assert not shutdown.done()
        assert len(manager._tasks) >= 1
        release.set()
        await shutdown
        assert not manager._tasks

    _run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_finished_cleanup_failure_prevents_successful_shutdown(cancel):
    async def scenario():
        manager = ExecutionManager()
        entered = asyncio.Event()

        async def cleanup():
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise StateError("controlled cleanup failure")

        task = manager.start(cleanup(), name="failed-release", cleanup=True)
        await entered.wait()
        if cancel:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(StateError, match="cleanup"):
            await manager.shutdown()
        assert not manager._tasks

    _run(scenario())


@pytest.mark.parametrize("mode", ["deadline", "stop"])
def test_resistant_deadline_terminalizes_before_cleanup_and_close_drains(tmp_path, mode):
    async def scenario():
        cancelled, release, generator_closed = (asyncio.Event() for _ in range(3))
        reading = asyncio.Event()

        class Backend:
            async def stream(self, request):
                try:
                    yield GenerationDispatched(request.attempt_id)
                    yield GenerationDelta(request.attempt_id, "partial")
                    try:
                        reading.set()
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                finally:
                    generator_closed.set()

        app, store = _configured_application(tmp_path, Backend())
        subscription = app.subscribe()
        await app.set_application_generation_settings(GenerationSettings(timeout_seconds=0.05 if mode == "deadline" else 90))
        chat = await app.create_chat()
        attempt = await app.send_message(chat.id, "resistant cleanup")
        work = app._generation_tasks[attempt.id]
        await asyncio.wait_for(reading.wait(), 2)
        stopping = asyncio.create_task(app.cancel_generation(attempt.id)) if mode == "stop" else None
        await asyncio.wait_for(cancelled.wait(), 2)
        async for event in subscription:
            if event.kind == ("generation_failed" if mode == "deadline" else "generation_aborted") and event.payload.get("attempt_id") == attempt.id:
                break
        # Publication precedes the finally/handoff. Join the terminal work,
        # rather than treating subscriber wakeup as a cleanup-registration barrier.
        work_result, = await asyncio.gather(work, return_exceptions=True)
        assert isinstance(work_result, asyncio.CancelledError) if mode == "stop" else work_result is None
        if stopping is not None:
            await stopping
        stored = store.get_generation_attempt(attempt.id)
        assert stored.state.value == ("failed" if mode == "deadline" else "aborted")
        assert stored.error_type == ("timeout" if mode == "deadline" else "aborted")
        assert stored.remote_outcome_unknown is True
        assert store.get_message(attempt.assistant_message_id).content == "partial"
        assert not generator_closed.is_set()
        assert any(app._execution._tasks.values()), "deferred release must have the execution owner"
        store_release = asyncio.Event()
        close = store.close

        def close_store():
            assert generator_closed.is_set()
            store_release.set()
            close()

        store.close = close_store
        closing = asyncio.create_task(app.close())
        # The existing release obligation is still held at this point. No
        # sleep/GC turn is used to make the cleanup succeed.
        assert not store_release.is_set()
        release.set()
        await closing
        assert generator_closed.is_set() and store_release.is_set()
        assert not app._execution._tasks

    _run(scenario())


def test_application_close_surfaces_generator_cleanup_failure(tmp_path):
    async def scenario():
        class Backend:
            async def stream(self, request):
                try:
                    yield GenerationDelta(request.attempt_id, "complete")
                    yield GenerationCompleted(request.attempt_id)
                finally:
                    raise StateError("controlled generator close failure")

        app, store = _configured_application(tmp_path, Backend())
        subscription = app.subscribe()
        chat = await app.create_chat()
        attempt = await app.send_message(chat.id, "close failure")
        async for event in subscription:
            if event.kind == "generation_completed":
                break
        assert store.get_generation_attempt(attempt.id).state.value == "complete"
        with pytest.raises(StateError, match="execution"):
            await app.close()
        assert [error.stage for error in app._close_result.errors] == ["execution"]
        with pytest.raises(StateError, match="execution"):
            await app.close()
        assert not app._execution._tasks

    _run(scenario())


def test_http_release_unwinds_inner_iterator_and_surfaces_close_failure():
    async def scenario():
        exited = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    yield b"data: partial\n\n"
                    await asyncio.Event().wait()
                finally:
                    exited.set()

            async def aclose(self):
                raise StateError("controlled response close failure")

        response = httpx.Response(200, stream=Body())
        with pytest.raises(StateError, match="response close"):
            async with _owned_response_lines(response) as lines:
                assert await anext(lines) == "data: partial"
        assert exited.is_set() and lines.ag_frame is None
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    _run(scenario())


def _chunk(text=None, finish=None, usage=None):
    payload = {"choices": [{"delta": {} if text is None else {"content": text}, "finish_reason": finish}]}
    if usage:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload) + "\n\n").encode()


class _Body(httpx.AsyncByteStream):
    def __init__(self, case, observations, inner=None, release=None):
        self.case, self.observations, self.inner, self.release = case, observations, inner, release
        self.close_started = asyncio.Event()
        self.closed = False
        self.tail_reads = 0

    async def __aiter__(self):
        if self.inner is not None:
            iterator = self.inner.__aiter__()
            suffix = b""
            while True:
                if self.case == "normal-tail" and b"data: [DONE]\n\n" in suffix:
                    self.tail_reads += 1
                    raise httpx.ReadError("controlled unused response tail failure")
                try:
                    part = await anext(iterator)
                except StopAsyncIteration:
                    return
                suffix = (suffix + part)[-128:]
                yield part
        yield _chunk("partial")
        if self.case in ("normal", "normal-tail"):
            yield _chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}) + b"data: [DONE]\n\n"
            if self.case == "normal-tail":
                self.tail_reads += 1
                raise httpx.ReadError("controlled unused response tail failure")
        elif self.case == "malformed":
            yield b"data: not-json\n\n"
        elif self.case == "conflict":
            yield _chunk(finish="stop")
            yield _chunk(finish="length")
        elif self.case == "failure":
            raise httpx.ReadError("controlled local stream failure")
        else:
            await asyncio.Event().wait()

    async def aclose(self):
        self.observations.append("body_close_enter")
        self.close_started.set()
        if self.release is not None:
            with anyio.CancelScope(shield=True):
                await self.release.wait()
        if self.inner is not None:
            await self.inner.aclose()
        self.closed = True
        self.observations.append("body_close_exit")


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, case, observations, *, tcp=False, delayed=False):
        self.case, self.observations = case, observations
        self.inner = httpx.AsyncHTTPTransport(retries=0) if tcp else None
        self.release = asyncio.Event() if delayed else None
        self.body = None
        self.socket = None
        self.closed = False

    async def handle_async_request(self, request):
        assert request.url.host == "127.0.0.1" and request.method == "POST"
        if self.inner is not None:
            response = await self.inner.handle_async_request(request)
            self.socket = response.extensions["network_stream"].get_extra_info("socket")
            if self.release is not None or self.case == "normal-tail":
                self.body = _Body(self.case, self.observations, response.stream, self.release)
                response.stream = self.body
            return response  # bare HTTPcore iterator in every undelayed TCP case
        self.body = _Body(self.case, self.observations)
        return httpx.Response(200, stream=self.body, headers={"content-type": "text/event-stream"})

    async def aclose(self):
        self.observations.append("transport_close_enter")
        if self.inner is not None:
            await self.inner.aclose()
        self.closed = True
        self.observations.append("transport_close_exit")


def _observe_generator(owner, key, generators):
    original = owner.stream

    def stream(*args, **kwargs):
        generator = original(*args, **kwargs)
        generators[key] = weakref.ref(generator)
        return generator

    owner.stream = stream


@contextmanager
def _client_observation(monkeypatch, observations, clients, responses, generators):
    original_client = httpx.AsyncClient
    original_close = httpx.Response.aclose

    class Client(original_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(weakref.ref(self))

        async def send(self, *args, **kwargs):
            response = await super().send(*args, **kwargs)
            responses.append(weakref.ref(response))
            return response

        async def __aexit__(self, *args):
            observations.append("client_close_enter")
            result = await super().__aexit__(*args)
            observations.append("client_close_exit")
            return result

    async def response_close(self):
        result = await original_close(self)
        observations.append("response_close_exit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "AsyncClient", Client)
        patch.setattr(httpx.Response, "aclose", response_close)
        for method in ("aiter_lines", "aiter_text", "aiter_bytes", "aiter_raw"):
            original = getattr(httpx.Response, method)

            def iterate(self, *args, _original=original, _method=method, **kwargs):
                generator = _original(self, *args, **kwargs)
                generators[_method] = weakref.ref(generator)
                return generator

            patch.setattr(httpx.Response, method, iterate)
        yield


async def _lifetime_scenario(tmp_path, provider, case, tcp, loop_kind, monkeypatch, desktop=False):
    observations, generators, clients, responses, errors, unraisables = [], {}, [], [], [], []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: errors.append(str(context)))
    original_hook = sys.unraisablehook

    def hook(item):
        unraisables.append(str(item.exc_value))
        original_hook(item)

    monkeypatch.setattr(sys, "unraisablehook", hook)
    server = None
    server_tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        server_tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")

            def send(data):
                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

            send(_chunk("partial"))
            if case in ("normal", "normal-tail"):
                send(_chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}) + b"data: [DONE]\n\n")
            elif case == "malformed":
                send(b"data: not-json\n\n")
            elif case == "conflict":
                send(_chunk(finish="stop") + _chunk(finish="length"))
            await writer.drain()
            if case != "failure":
                await reader.read()  # EOF is observed, rather than forcing peer close.
                observations.append("server_saw_client_eof")
        finally:
            writer.close()
            await writer.wait_closed()
            server_tasks.discard(task)

    if tcp:
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1] if server else 9000
    endpoint = f"http://127.0.0.1:{port}/v1"
    delayed = case == "shutdown-slow"
    transport = _Transport(case, observations, tcp=tcp, delayed=delayed)
    secrets = FakeSecretStore({"local-ref": "synthetic-local-only"})
    router = BuiltinProviderRouter(secret_stores={"secret_service": secrets})
    factory = router._openai_backend

    def backend(request):
        backend = factory(request)
        _observe_generator(backend, "backend", generators)
        _observe_generator(backend._provider, "provider", generators)
        return backend

    router._openai_backend = backend
    _observe_generator(router, "router", generators)
    runtime = None
    if desktop:
        from bots5.bootstrap.desktop import build_runtime
        runtime = build_runtime(tmp_path / "desktop")
        app, store = runtime.application, runtime.application._store
        app._backend = router
        app._configuration.phase6_enabled = False
        app._configuration.secret_stores[CredentialSource.SECRET_SERVICE] = secrets
    else:
        app, store = _configured_application(tmp_path, router, secret_store=secrets)
        database = tmp_path / "state.sqlite3"
    connection = await app.create_provider_connection(name="Disposable local cleanup regression", backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile(provider), endpoint=endpoint,
        credential_source=CredentialSource.SECRET_SERVICE if provider == "openrouter" else CredentialSource.NONE,
        credential_reference="local-ref" if provider == "openrouter" else None)
    model = await app.add_manual_model(connection_id=connection.id, provider_model_id="local/lifetime-test")
    for key in ("generation.streaming", "request.temperature", "request.max_output_tokens"):
        await app.set_capability_override(CapabilityOverride(model.id, key, CapabilityState.SUPPORTED))
    if provider == "openrouter":
        app._configuration.phase6_enabled = True
        with store.command_admission():
            store.set_capability_fact(CapabilityFact(model.id, "limits.context_tokens", CapabilityState.SUPPORTED,
                CapabilitySource.TRUSTED_REGISTRY, source_revision=1, value=32000,
                provenance={"field": "context_length"}, observed_at=app._clock.now()))
    router._transports[connection.id] = transport
    chat = await app.create_chat()
    await app.select_model(chat.id, model.id)
    await app.set_chat_generation_settings(chat.id, GenerationSettings(max_output_tokens=32,
        timeout_seconds=0.1 if case == "timeout" else 90 if case != "shutdown-no-deadline" else None))
    delta, terminal, ready = (asyncio.Event() for _ in range(3))
    original_publish = app._publish_after_persistence

    async def publish(kind, **payload):
        if kind == "message_delta":
            delta.set()
        if kind in ("generation_completed", "generation_failed", "generation_aborted"):
            terminal.set()
        await original_publish(kind, **payload)

    app._publish_after_persistence = publish
    subscription = None
    pump = None
    if case in ("cancel-backpressure", "shutdown-backpressure", "shutdown-no-deadline"):
        app._events._queue_size = 1
        subscription = app.subscribe()

        async def consume_startup():
            async for event in subscription:
                if event.kind == "generation_started":
                    ready.set()
                    return

        pump = asyncio.create_task(consume_startup(), name="test-startup-events")

    store_closed = False
    original_store_close = store.close

    def close_store():
        nonlocal store_closed
        assert transport.closed and "client_close_exit" in observations
        if transport.socket is not None:
            assert transport.socket.fileno() == -1
        observations.append("store_release")
        store_closed = True
        original_store_close()

    store.close = close_store
    if runtime is not None:
        original_authority_release = runtime.authority.release

        def release_authority():
            assert store_closed and transport.closed
            observations.append("outer_authority_release")
            original_authority_release()

        runtime.authority.release = release_authority
    close_owner = app.close if runtime is None else runtime.close
    try:
        with _client_observation(monkeypatch, observations, clients, responses, generators):
            attempt = await app.send_message(chat.id, "local cleanup regression")
            await asyncio.wait_for(delta.wait(), 3)
            if subscription is not None:
                await ready.wait()
                await pump
                assert subscription._queue.full()
            if case.startswith("cancel"):
                cancelling = asyncio.create_task(app.cancel_generation(attempt.id))
                await asyncio.wait_for(terminal.wait(), 3)
                if subscription is not None:
                    subscription.close()
                result = await cancelling
                assert result.state.value == "aborted"
            elif not case.startswith("shutdown"):
                await asyncio.wait_for(terminal.wait(), 3)
            closing = asyncio.create_task(close_owner())
            if delayed:
                await asyncio.wait_for(transport.body.close_started.wait(), 3)
                assert not closing.done() and not store_closed
                transport.release.set()
            await asyncio.wait_for(closing, 3)
            assert store_closed and transport.closed
            assert not app._execution._tasks
            assert all(ref() is None or ref().ag_frame is None for ref in generators.values())
            assert all(ref() is None or ref().is_closed for ref in clients + responses)
            if case == "normal-tail":
                assert transport.body.closed and transport.body.tail_reads == 0
            # Observe immediately at the close-return boundary. No GC, sleep
            # or shutdown_asyncgens is allowed to manufacture a passing state.
            product_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and task not in server_tasks]
            assert not product_tasks, [(task.get_name(), repr(task.get_coro())) for task in product_tasks]
        reopened_authority = None
        if runtime is not None:
            from bots5.infrastructure.authority_lock import AuthorityLock
            reopened_authority = AuthorityLock(runtime.paths.data_root).acquire()
            reopened = reopened_authority.open_store()
        else:
            reopened = SQLiteAppStateStore.open(database)
        try:
            stored = reopened.get_generation_attempt(attempt.id)
            message = reopened.get_message(attempt.assistant_message_id)
            expected = "aborted" if case.startswith(("cancel", "shutdown")) else "failed" if case in ("failure", "malformed", "conflict", "timeout") else "complete"
            assert stored.state.value == expected and message.state.value == expected
            assert message.content == "partial"
            assert stored.remote_outcome_unknown is (expected != "complete")
            if case == "timeout":
                assert stored.error_type == "timeout"
            if expected == "complete":
                assert stored.finish_reason == "stop"
                assert (stored.prompt_tokens, stored.completion_tokens, stored.total_tokens) == (3, 2, 5)
        finally:
            reopened.close()
            if reopened_authority is not None:
                reopened_authority.release()
        assert not errors and not unraisables
        output = os.environ.get("BOTS5_CLEANUP_OBSERVATIONS")
        if output:
            with Path(output).open("a") as file:
                file.write(json.dumps({"provider": provider, "case": case, "tcp": tcp, "loop": loop_kind,
                    "desktop_runtime": desktop,
                    "tail_reads": None if transport.body is None else transport.body.tail_reads,
                    "observations": observations, "socket_fd": None if transport.socket is None else transport.socket.fileno(),
                    "generators_settled": True, "execution_tasks": 0, "product_tasks_after_close": 0,
                    "durable_state": expected, "remote_outcome_unknown": stored.remote_outcome_unknown,
                    "loop_errors": errors, "unraisables": unraisables}) + "\n")
    finally:
        if transport.release is not None:
            transport.release.set()
        if not store_closed:
            # Restore the observer assertion for cleanup of a failing test;
            # the failure and its original resource state remain recorded.
            store.close = original_store_close
            await close_owner()
        if server is not None:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*tuple(server_tasks), return_exceptions=True)


@pytest.mark.parametrize("provider", ["openrouter", "generic"])
@pytest.mark.parametrize("case", ["normal", "cancel", "shutdown", "failure", "malformed", "conflict"])
def test_instrumented_provider_ownership(tmp_path, monkeypatch, provider, case):
    _run(_lifetime_scenario(tmp_path, provider, case, False, "asyncio", monkeypatch))


@pytest.mark.parametrize("loop_kind", ["asyncio", "qasync"])
@pytest.mark.parametrize("provider", ["openrouter", "generic"])
@pytest.mark.parametrize("case", ["normal", "cancel-backpressure", "shutdown-backpressure", "shutdown-slow", "shutdown-no-deadline", "failure", "timeout"])
def test_real_tcp_application_cleanup(tmp_path, monkeypatch, loopback_only, provider, case, loop_kind):
    _run(_lifetime_scenario(tmp_path, provider, case, True, loop_kind, monkeypatch), loop_kind)


@pytest.mark.parametrize("provider", ["openrouter", "generic"])
@pytest.mark.parametrize("case", ["normal", "shutdown-slow"])
def test_qasync_desktop_provider_cleanup(tmp_path, monkeypatch, loopback_only, provider, case):
    _run(_lifetime_scenario(tmp_path, provider, case, True, "qasync", monkeypatch, desktop=True), "qasync")


@pytest.mark.parametrize("loop_kind", ["asyncio", "qasync"])
@pytest.mark.parametrize("provider", ["openrouter", "generic"])
@pytest.mark.parametrize("case", ["done-tail", "pre-done-failure", "early-return"])
def test_terminal_tail_contract(monkeypatch, provider, case, loop_kind):
    async def scenario():
        hooks = sys.get_asyncgen_hooks()
        observations, clients, responses, generators, errors = [], [], [], {}, []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(str(context)))

        class Body(httpx.AsyncByteStream):
            def __init__(self):
                self.tail_reads = self.required_read_errors = 0
                self.closed = self.iterator_closed = False

            async def __aiter__(self):
                try:
                    yield _chunk("partial")
                    if case == "pre-done-failure":
                        self.required_read_errors += 1
                        raise httpx.ReadError("controlled pre-DONE transport failure")
                    yield _chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}) + b"data: [DONE]\n\n"
                    self.tail_reads += 1
                    raise httpx.ReadError("controlled unused response tail failure")
                finally:
                    self.iterator_closed = True
                    observations.append("body_iterator_close")

            async def aclose(self):
                self.closed = True
                observations.append("body_close")

        body = Body()

        class Transport(httpx.AsyncBaseTransport):
            closed = False

            async def handle_async_request(self, request):
                assert request.url.host == "127.0.0.1"
                return httpx.Response(200, stream=body)

            async def aclose(self):
                self.closed = True
                observations.append("transport_close")

        transport = Transport()
        adapter = (OpenRouterProvider("synthetic-local-only", base_url="http://127.0.0.1:9000/v1", _transport=transport)
                   if provider == "openrouter" else OpenAICompatibleProvider("http://127.0.0.1:9000/v1", _transport=transport))
        request = CompletionRequest("local/test", "", "synthetic", 0.0, 32, 90)
        chunks = []
        failure = None
        with _client_observation(monkeypatch, observations, clients, responses, generators):
            stream = adapter.stream(request)
            try:
                async with aclosing(stream):
                    async for chunk in stream:
                        chunks.append(chunk)
                        if case == "early-return" and chunk.finish_reason == "stop":
                            break
            except ProviderError as error:
                failure = error
            assert stream.ag_frame is None
            assert body.closed and body.iterator_closed and transport.closed
            assert "response_close_exit" in observations and "client_close_exit" in observations
            assert all(ref() is None or ref().ag_frame is None for ref in generators.values())
            assert all(ref() is None or ref().is_closed for ref in clients + responses)
            assert body.tail_reads == 0
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert sys.get_asyncgen_hooks() == hooks
        assert not errors
        assert "".join(chunk.text for chunk in chunks) == "partial"
        if case == "pre-done-failure":
            assert failure is not None and "pre-DONE transport failure" in str(failure)
            assert body.required_read_errors == 1
            assert all(chunk.finish_reason is None for chunk in chunks)
        else:
            assert failure is None and body.required_read_errors == 0
            assert chunks[-1].finish_reason == "stop"
            assert (chunks[-1].prompt_tokens, chunks[-1].completion_tokens, chunks[-1].total_tokens) == (3, 2, 5)
        output = os.environ.get("BOTS5_CLEANUP_ORACLE_OBSERVATIONS")
        if output:
            with Path(output).open("a") as file:
                file.write(json.dumps({"provider": provider, "case": case, "loop": loop_kind,
                    "tail_reads": body.tail_reads, "required_read_errors": body.required_read_errors,
                    "failure": None if failure is None else str(failure), "observations": observations,
                    "body_iterator_closed": body.iterator_closed, "response_client_transport_closed": True,
                    "provider_generator_closed": True, "product_tasks": 0, "hooks_restored": True}) + "\n")

    _run(scenario(), loop_kind)


@pytest.mark.parametrize("loop_kind", ["asyncio", "qasync"])
def test_response_owner_hooks_restore_before_suspend_and_exclude_other_generators(loop_kind):
    async def scenario():
        hooks = sys.get_asyncgen_hooks()
        reading, release = asyncio.Event(), asyncio.Event()

        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                reading.set()
                await release.wait()
                yield b"data: partial\n\n"
                raise AssertionError("owner must not resume unused bytes")

            async def aclose(self):
                self.closed = True

        async def unrelated():
            yield "other"
            yield "still-owned-by-caller"

        body = Body()
        other = unrelated()
        try:
            async with _owned_response_lines(httpx.Response(200, stream=body)) as lines:
                reading_task = asyncio.create_task(anext(lines))
                await reading.wait()
                # The body is suspended inside the read. No capture hook can
                # leak to this other task or to its unrelated generator.
                assert sys.get_asyncgen_hooks() == hooks
                assert await anext(other) == "other"
                release.set()
                assert await reading_task == "data: partial"
            assert body.closed and lines.ag_frame is None
            assert sys.get_asyncgen_hooks() == hooks
            assert await anext(other) == "still-owned-by-caller"
        finally:
            await other.aclose()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    _run(scenario(), loop_kind)


@pytest.mark.parametrize("loop_kind", ["asyncio", "qasync"])
@pytest.mark.parametrize("provider", ["openrouter", "generic"])
def test_real_tcp_terminal_tail_cleanup(tmp_path, monkeypatch, loopback_only, provider, loop_kind):
    _run(_lifetime_scenario(tmp_path, provider, "normal-tail", True, loop_kind, monkeypatch), loop_kind)
