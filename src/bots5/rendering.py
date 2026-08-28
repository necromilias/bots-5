from __future__ import annotations


def render_worker_user_message(inputs: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"=== INPUT: {label} ===\n{text}\n=== END INPUT: {label} ==="
        for label, text in inputs
    )


def render_synthesis_user_message(dependencies: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"=== WORKER OUTPUT: {worker_id} ===\n{text}\n=== END WORKER OUTPUT: {worker_id} ==="
        for worker_id, text in dependencies
    )
