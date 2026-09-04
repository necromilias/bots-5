"""Add nullable provider outcome metadata to generation attempts."""

import json

from alembic import op
import sqlalchemy as sa

from bots5.domain.models import AttemptState
from bots5.infrastructure.persistence.phase3_validation import (
    is_phase3_record,
    validate_outcome_fields,
    validate_request_snapshot,
)


revision = "0005_generation_outcomes"
down_revision = "0004_integrity_guard_function"
branch_labels = None
depends_on = None


_OUTCOME_COLUMNS = (
    sa.Column("provider_id", sa.String(length=64), nullable=True),
    sa.Column("returned_model", sa.Text(), nullable=True),
    sa.Column("request_id", sa.Text(), nullable=True),
    sa.Column("finish_reason", sa.String(length=128), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), nullable=True),
    sa.Column("completion_tokens", sa.Integer(), nullable=True),
    sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    sa.Column("total_tokens", sa.Integer(), nullable=True),
    sa.Column("known_cost_usd", sa.Text(), nullable=True),
    sa.Column("remote_outcome_unknown", sa.Boolean(), nullable=True),
)


def _validate_outcome_rows(connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT a.id, a.chat_id, a.user_message_id, a.backend_id, a.model, a.state, "
            "a.request_snapshot, a.provider_id, a.returned_model, a.request_id, "
            "a.error_type, a.error_message, "
            "a.finish_reason, a.prompt_tokens, a.completion_tokens, a.reasoning_tokens, "
            "a.total_tokens, a.known_cost_usd, a.remote_outcome_unknown, "
            "u.content AS user_message_content "
            "FROM generation_attempts AS a "
            "LEFT JOIN messages AS u ON u.id = a.user_message_id"
        )
    ).mappings()
    for row in rows:
        try:
            snapshot_for_classification = json.loads(row["request_snapshot"])
        except (TypeError, ValueError):
            snapshot_for_classification = {}
        if not isinstance(snapshot_for_classification, dict):
            snapshot_for_classification = {}
        phase3 = is_phase3_record(
            backend_id=row["backend_id"],
            provider_id=row["provider_id"],
            snapshot=snapshot_for_classification,
            include_backend_marker=False,
        )
        snapshot = validate_request_snapshot(
            attempt_id=row["id"],
            chat_id=row["chat_id"],
            user_message_id=row["user_message_id"],
            backend_id=row["backend_id"],
            model=row["model"],
            provider_id=row["provider_id"],
            request_snapshot=row["request_snapshot"],
            user_message_content=row["user_message_content"],
            phase3=phase3,
            error_type=RuntimeError,
        )
        validate_outcome_fields(
            state=row["state"],
            provider_id=row["provider_id"],
            finish_reason=row["finish_reason"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            total_tokens=row["total_tokens"],
            known_cost_usd=row["known_cost_usd"],
            remote_outcome_unknown=row["remote_outcome_unknown"],
            returned_model=row["returned_model"],
            request_id=row["request_id"],
            outcome_error_type=row["error_type"],
            outcome_error_message=row["error_message"],
            phase3=phase3,
            error_type=RuntimeError,
        )


def _replace_attempt_triggers(connection) -> None:
    connection.execute(sa.text("DROP TRIGGER IF EXISTS generation_attempt_validate_insert"))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS generation_attempt_validate_update"))
    insert_phase3_marker = "(NEW.provider_id IS NOT NULL OR NEW.backend_id = 'openai_compatible_http')"
    update_phase3_marker = (
        "COALESCE((OLD.provider_id IS NOT NULL OR NEW.provider_id IS NOT NULL OR "
        "json_extract(NEW.request_snapshot, '$.provider_id') IN ('local_openai', 'openrouter') OR "
        "json_extract(NEW.request_snapshot, '$.backend_id') = 'openai_compatible_http'), 0)"
    )
    insert_snapshot_validation = (
        "bots5_valid_request_snapshot(NEW.request_snapshot, NEW.id, NEW.chat_id, "
        "NEW.user_message_id, NEW.backend_id, NEW.model, NEW.provider_id, "
        "(SELECT content FROM messages WHERE id = NEW.user_message_id AND role = 'user'), "
        f"{insert_phase3_marker}) = 0"
    )
    update_snapshot_validation = (
        "bots5_valid_request_snapshot(NEW.request_snapshot, NEW.id, NEW.chat_id, "
        "NEW.user_message_id, NEW.backend_id, NEW.model, NEW.provider_id, "
        "(SELECT content FROM messages WHERE id = NEW.user_message_id AND role = 'user'), "
        f"{update_phase3_marker}) = 0"
    )
    insert_outcome_validation = (
        "bots5_valid_outcome_fields(NEW.state, NEW.provider_id, NEW.finish_reason, "
        "NEW.prompt_tokens, NEW.completion_tokens, NEW.reasoning_tokens, NEW.total_tokens, "
        "NEW.known_cost_usd, NEW.remote_outcome_unknown, NEW.returned_model, NEW.request_id, "
        "NEW.error_type, NEW.error_message, "
        f"{insert_phase3_marker}) = 0"
    )
    update_outcome_validation = (
        "bots5_valid_outcome_fields(NEW.state, NEW.provider_id, NEW.finish_reason, "
        "NEW.prompt_tokens, NEW.completion_tokens, NEW.reasoning_tokens, NEW.total_tokens, "
        "NEW.known_cost_usd, NEW.remote_outcome_unknown, NEW.returned_model, NEW.request_id, "
        "NEW.error_type, NEW.error_message, "
        f"{update_phase3_marker}) = 0"
    )
    remote_transition_validation = (
        "bots5_valid_remote_outcome_transition(OLD.state, OLD.remote_outcome_unknown, "
        "NEW.state, NEW.remote_outcome_unknown, NEW.finish_reason, "
        f"{update_phase3_marker}) = 0"
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_validate_insert BEFORE INSERT ON generation_attempts "
            "WHEN bots5_internal_transition(NEW.assistant_message_id, NEW.id, 'start-attempt') = 0 "
            "OR NEW.state NOT IN ('running', 'complete', 'incomplete', 'failed', 'aborted') "
            "OR (NEW.state = 'running' AND NEW.ended_at IS NOT NULL) "
            "OR (NEW.state <> 'running' AND NEW.ended_at IS NULL) "
            "OR bots5_valid_timestamp(NEW.started_at) = 0 "
            "OR (NEW.ended_at IS NOT NULL AND bots5_valid_timestamp(NEW.ended_at) = 0) "
            "OR json_valid(NEW.request_snapshot) = 0 "
            "OR (json_type(NEW.request_snapshot, '$.attempt_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.attempt_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.attempt_id') <> NEW.id)) "
            "OR (json_type(NEW.request_snapshot, '$.chat_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.chat_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.chat_id') <> NEW.chat_id)) "
            "OR (json_type(NEW.request_snapshot, '$.user_message_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.user_message_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.user_message_id') <> NEW.user_message_id)) "
            "OR (json_type(NEW.request_snapshot, '$.backend_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.backend_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.backend_id') <> NEW.backend_id)) "
            "OR (json_type(NEW.request_snapshot, '$.model') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.model') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.model') <> NEW.model)) "
            "OR (NEW.provider_id IS NOT NULL AND NEW.provider_id NOT IN ('local_openai', 'openrouter')) "
            "OR " + insert_snapshot_validation + " "
            "OR " + insert_outcome_validation + " "
            "OR (" + insert_phase3_marker + " AND NEW.remote_outcome_unknown IS NULL) "
            "OR (NEW.remote_outcome_unknown IS NOT NULL AND "
            "(typeof(NEW.remote_outcome_unknown) <> 'integer' OR NEW.remote_outcome_unknown NOT IN (0, 1))) "
            "OR (NEW.prompt_tokens IS NOT NULL AND "
            "(typeof(NEW.prompt_tokens) <> 'integer' OR NEW.prompt_tokens < 0)) "
            "OR (NEW.completion_tokens IS NOT NULL AND "
            "(typeof(NEW.completion_tokens) <> 'integer' OR NEW.completion_tokens < 0)) "
            "OR (NEW.reasoning_tokens IS NOT NULL AND "
            "(typeof(NEW.reasoning_tokens) <> 'integer' OR NEW.reasoning_tokens < 0)) "
            "OR (NEW.total_tokens IS NOT NULL AND "
            "(typeof(NEW.total_tokens) <> 'integer' OR NEW.total_tokens < 0)) "
            "OR bots5_valid_cost(NEW.known_cost_usd) = 0 "
            "OR NOT EXISTS (SELECT 1 FROM messages AS u JOIN messages AS a ON a.id = NEW.assistant_message_id "
            "WHERE u.id = NEW.user_message_id AND u.chat_id = NEW.chat_id AND a.chat_id = NEW.chat_id "
            "AND u.role = 'user' AND a.role = 'assistant' AND a.parent_id = u.id) "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt fields are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_validate_update BEFORE UPDATE OF "
            "id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
            "request_snapshot, started_at, state, ended_at, error_type, error_message, "
            "provider_id, returned_model, request_id, finish_reason, prompt_tokens, "
            "completion_tokens, reasoning_tokens, total_tokens, known_cost_usd, "
            "remote_outcome_unknown ON generation_attempts "
            "WHEN NEW.state NOT IN ('running', 'complete', 'incomplete', 'failed', 'aborted') "
            "OR (NEW.state = 'running' AND NEW.ended_at IS NOT NULL) "
            "OR (NEW.state <> 'running' AND NEW.ended_at IS NULL) "
            "OR (NEW.ended_at IS NOT NULL AND bots5_valid_timestamp(NEW.ended_at) = 0) "
            "OR (NEW.provider_id IS NOT OLD.provider_id OR NEW.chat_id IS NOT OLD.chat_id "
            "OR NEW.user_message_id IS NOT OLD.user_message_id "
            "OR NEW.assistant_message_id IS NOT OLD.assistant_message_id "
            "OR NEW.backend_id IS NOT OLD.backend_id OR NEW.model IS NOT OLD.model "
            "OR NEW.request_snapshot IS NOT OLD.request_snapshot "
            "OR NEW.started_at IS NOT OLD.started_at) "
            "OR (NEW.provider_id IS NOT NULL AND NEW.provider_id NOT IN ('local_openai', 'openrouter')) "
            "OR " + update_snapshot_validation + " "
            "OR " + update_outcome_validation + " "
            "OR " + remote_transition_validation + " "
            "OR (" + update_phase3_marker + " AND NEW.remote_outcome_unknown IS NULL) "
            "OR (NEW.remote_outcome_unknown IS NOT NULL AND "
            "(typeof(NEW.remote_outcome_unknown) <> 'integer' OR NEW.remote_outcome_unknown NOT IN (0, 1))) "
            "OR (NEW.prompt_tokens IS NOT NULL AND "
            "(typeof(NEW.prompt_tokens) <> 'integer' OR NEW.prompt_tokens < 0)) "
            "OR (NEW.completion_tokens IS NOT NULL AND "
            "(typeof(NEW.completion_tokens) <> 'integer' OR NEW.completion_tokens < 0)) "
            "OR (NEW.reasoning_tokens IS NOT NULL AND "
            "(typeof(NEW.reasoning_tokens) <> 'integer' OR NEW.reasoning_tokens < 0)) "
            "OR (NEW.total_tokens IS NOT NULL AND "
            "(typeof(NEW.total_tokens) <> 'integer' OR NEW.total_tokens < 0)) "
            "OR bots5_valid_cost(NEW.known_cost_usd) = 0 "
            "OR (OLD.state <> 'running' AND (NEW.state <> OLD.state "
            "OR NEW.ended_at IS NOT OLD.ended_at OR NEW.error_type IS NOT OLD.error_type "
            "OR NEW.error_message IS NOT OLD.error_message "
            "OR NEW.returned_model IS NOT OLD.returned_model "
            "OR NEW.request_id IS NOT OLD.request_id "
            "OR NEW.finish_reason IS NOT OLD.finish_reason "
            "OR NEW.prompt_tokens IS NOT OLD.prompt_tokens "
            "OR NEW.completion_tokens IS NOT OLD.completion_tokens "
            "OR NEW.reasoning_tokens IS NOT OLD.reasoning_tokens "
            "OR NEW.total_tokens IS NOT OLD.total_tokens "
            "OR NEW.known_cost_usd IS NOT OLD.known_cost_usd "
            "OR NEW.remote_outcome_unknown IS NOT OLD.remote_outcome_unknown)) "
            "OR (OLD.state = 'running' AND NEW.state <> 'running' "
            "AND bots5_internal_transition(NEW.assistant_message_id, NEW.id, 'finalize-attempt') = 0) "
            "OR (OLD.state = 'running' AND NEW.state <> 'running' AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND role = 'assistant' "
            "AND parent_id = NEW.user_message_id AND state = 'streaming')) "
            "OR (NEW.state = 'running' AND OLD.state <> 'running') "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt lifecycle transition is invalid'); END"
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    for column in _OUTCOME_COLUMNS:
        op.add_column("generation_attempts", column)
    _validate_outcome_rows(connection)
    _replace_attempt_triggers(connection)


def downgrade() -> None:
    raise RuntimeError("generation outcome metadata cannot be downgraded")
