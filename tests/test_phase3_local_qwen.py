from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from bots5.bootstrap.desktop import build_runtime
from bots5.core.events import EventSubscription
from bots5.domain.models import AttemptState, MessageState


def _required_environment() -> tuple[str, str, str | None]:
    base_url = os.environ.get("BOTS5_PHASE3_QWEN_BASE_URL")
    model = os.environ.get("BOTS5_PHASE3_QWEN_MODEL")
    api_key_env = os.environ.get("BOTS5_PHASE3_QWEN_API_KEY_ENV")
    if not base_url or not model:
        pytest.skip(
            "opt-in only: set BOTS5_PHASE3_QWEN_BASE_URL and "
            "BOTS5_PHASE3_QWEN_MODEL to run the local acceptance probe"
        )
    return base_url, model, api_key_env


async def _wait_for_terminal(subscription: EventSubscription, attempt_id: str) -> None:
    while True:
        event = await asyncio.wait_for(subscription.__anext__(), timeout=120)
        if event.payload.get("attempt_id") == attempt_id and event.kind in {
            "generation_completed",
            "generation_incomplete",
            "generation_failed",
            "generation_aborted",
        }:
            return


@pytest.mark.local_qwen_acceptance
def test_opt_in_local_qwen_acceptance_path(tmp_path: Path):
    base_url, model, api_key_env = _required_environment()
    if "OPENROUTER_API_KEY" in os.environ:
        pytest.fail("local Qwen acceptance must not use OpenRouter credentials")

    async def scenario():
        runtime = build_runtime(
            tmp_path / "data",
            backend="local_openai",
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
        )
        subscription = runtime.application.subscribe()
        try:
            chat = await runtime.application.create_chat()
            await subscription.__anext__()
            attempt = await runtime.application.send_message(
                chat.id,
                "Reply with a short sentence proving the local Qwen stream works.",
            )
            await _wait_for_terminal(subscription, attempt.id)
            final_attempt = (await runtime.application.list_generation_attempts(chat.id))[0]
            _, messages = await runtime.application.open_chat(chat.id)
            assert final_attempt.provider_id == "local_openai"
            assert final_attempt.backend_id == "openai_compatible_http"
            assert final_attempt.state is AttemptState.COMPLETE
            assert final_attempt.finish_reason == "stop"
            assert messages[-1].state is MessageState.COMPLETE
            assert messages[-1].content
        finally:
            await runtime.close()

    asyncio.run(scenario())
