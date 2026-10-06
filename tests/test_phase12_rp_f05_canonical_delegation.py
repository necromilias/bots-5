"""RP-F-05 repair regression: the backend delegates the WHOLE classification to Rule D-9.

Classified finding RP-F-05 (phase12 linux-v0.1-torture 20261006-01, continuation
wave): after RP-F-01 aligned the ProviderHttpError branch, the backend's
``return True`` fall-through still disagreed with the canonical rule for
``ProviderResponseError`` — which carries ``definitive_rejection = True``
(failed-but-received, a KNOWN remote outcome) in ``src/bots5/errors.py``.

DIRECTION OF THE BUG (pinned explicitly below): RP-F-05 was the CONSERVATIVE
direction — a KNOWN outcome reported as unknown, so the D-9 fail-safe merely
declined to retry.  RP-F-01 was the dangerous direction (unknown reported as
known).  The repair flips ProviderResponseError from True to False.

The repair delegates everything except the deliberate ContextAdmissionError
short-circuit to ``provider_side_outcome_unknown`` (the canonical rule), so no
future exception class can diverge this way.  The short-circuit itself is NOT
wrong under the canonical rule — ContextAdmissionError.definitive_rejection is
True, so the canonical rule returns False for it too; the branch is kept as an
explicit, agreed seam decision.

Subject: S (source/runtime).  No network: pure classifier execution.
"""
from __future__ import annotations

import pytest

from bots5.errors import (
    ContextAdmissionError,
    ProviderError,
    ProviderHttpError,
    ProviderResponseError,
    ProviderTimeoutError,
    provider_side_outcome_unknown,
)
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend

classify = OpenAICompatibleStreamingBackend._provider_failure_is_uncertain


def _all_provider_error_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_provider_error_subclasses(sub)


def _instance(cls):
    """Construct a probe instance for any ProviderError subclass."""
    if cls is ProviderHttpError:
        return cls(400, "probe")
    if cls.__name__ == "ModelDiscoveryError":
        from bots5.providers.discovery import CatalogueRefreshFailureClass

        return cls(CatalogueRefreshFailureClass.PROTOCOL, "probe")
    return cls("probe")


def test_every_provider_error_class_agrees_with_canonical_rule():
    """Sweep EVERY loaded ProviderError subclass against the canonical rule.

    The known classes are enumerated explicitly (a missing class fails loudly)
    and the recursive ``__subclasses__()`` walk catches any class not in the
    list, so this class of divergence cannot recur silently.
    """
    known = [
        ProviderError,
        ProviderHttpError,
        ContextAdmissionError,
        ProviderResponseError,
        ProviderTimeoutError,
    ]
    from bots5.providers.discovery import ModelDiscoveryError

    known.append(ModelDiscoveryError)

    results = {}
    for cls in known:
        error = _instance(cls)
        results[cls.__name__] = (
            classify(error),
            provider_side_outcome_unknown(error),
        )
        assert classify(error) is provider_side_outcome_unknown(error), (
            f"{cls.__name__}: backend={classify(error)} "
            f"canonical={provider_side_outcome_unknown(error)}"
        )

    # Any ProviderError subclass loaded anywhere must agree too.
    for cls in _all_provider_error_subclasses(ProviderError):
        if cls in known:
            continue
        error = _instance(cls)
        assert classify(error) is provider_side_outcome_unknown(error), cls.__name__

    # The discovered subclass set covers exactly the six known classes.
    assert set(results) == {
        "ProviderError",
        "ProviderHttpError",
        "ContextAdmissionError",
        "ProviderResponseError",
        "ProviderTimeoutError",
        "ModelDiscoveryError",
    }


def test_provider_response_error_is_classified_known_with_direction_pin():
    """Direction pin: ProviderResponseError flipped True -> False (RP-F-05).

    Conservative direction repaired: a KNOWN outcome was reported unknown, so
    the D-9 fail-safe merely declined to retry.  This is the OPPOSITE of
    RP-F-01's dangerous direction (unknown reported as known), which is why
    the repaired value is asserted exactly, not just agreement.
    """
    error = ProviderResponseError("controlled probe")
    assert error.definitive_rejection is True
    assert classify(error) is False
    assert provider_side_outcome_unknown(error) is False

    # The conservative classes stay unknown — the repair must not over-correct.
    assert classify(ProviderError("probe")) is True
    assert classify(ProviderTimeoutError("probe")) is True
    # The short-circuit and the definitive 4xx rejection stay as repaired.
    assert classify(ContextAdmissionError("probe")) is False
    assert classify(ProviderHttpError(400, "probe")) is False
    assert classify(ProviderHttpError(408, "probe")) is True


def test_provider_response_error_terminal_event_is_known_end_to_end():
    """Terminal-event direction pin through the real backend seam."""
    import asyncio
    import sys
    from contextlib import aclosing

    from bots5.core.generation import GenerationDelta, GenerationDispatched, GenerationFailed
    from bots5.providers.base import CompletionStreamEvent

    class Provider:
        async def stream(self, request):
            yield CompletionStreamEvent(text="partial")
            raise ProviderResponseError("malformed_provider_response")

    from bots5.core.generation import GenerationRequest

    backend = OpenAICompatibleStreamingBackend(
        Provider(), provider_id="generic", base_url="http://127.0.0.1:9000/v1",
    )
    request = GenerationRequest(
        attempt_id="attempt-rpf05", chat_id="chat-rpf05",
        user_message_id="message-rpf05", backend_id="openai_compatible_http",
        model="local/rpf05-model", prompt="probe", provider_id="generic",
        base_url="http://127.0.0.1:9000/v1",
    )
    baseline_hooks = sys.get_asyncgen_hooks()

    async def scenario():
        events = []
        async with aclosing(backend.stream(request)) as stream:
            async for event in stream:
                events.append(event)
                if isinstance(event, GenerationFailed):
                    break
        assert stream.ag_frame is None
        return events

    events = asyncio.run(scenario(), debug=True)
    assert sys.get_asyncgen_hooks() == baseline_hooks
    # The partial delta was emitted before the failure; then the terminal event.
    assert [type(event) for event in events] == [
        GenerationDispatched,
        GenerationDelta,
        GenerationFailed,
    ]
    assert events[1].text == "partial"
    failure = events[2]
    assert failure.error_type == "ProviderResponseError"
    # The durable terminal event carries the REPAIRED, canonical direction.
    assert failure.remote_outcome_unknown is False
