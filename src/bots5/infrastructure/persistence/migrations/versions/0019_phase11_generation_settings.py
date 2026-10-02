"""Phase 11 scope amendment: the registry-driven generation-settings plane.

Adds three additive tables holding the normalized generation settings that
live OUTSIDE the four frozen legacy columns (``temperature``,
``max_output_tokens``, ``reasoning_effort``, ``timeout_seconds``) of the
existing application/model/chat generation-config tables:

- ``application_generation_settings_extra``  (singleton row, id = 1)
- ``model_generation_settings_extra``        (one row per model entry)
- ``chat_model_generation_settings_extra``   (one row per chat/model pair)

Each row stores its scope's registry-validated ``extra_settings_json`` payload
with an independent monotonic ``revision``.  The rows follow the exact same
foreign-key lifecycle as the pre-amendment generation-config rows: deleting a
model entry or a chat cascades to its extra-settings rows, so no orphans are
introduced.

``downgrade`` drops the three tables again, restoring the 0018 schema exactly.
The frozen legacy columns are not touched by this revision.
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_phase11_generation_settings"
down_revision = "0018_phase11_search_state"
branch_labels = None
depends_on = None


_JSON_OBJECT_CHECK = "extra_settings_json IS NULL OR json_valid(extra_settings_json)"
_REVISION_CHECK = "revision > 0"

# Registry-driven per-setting capability keys (manual overrides for the
# EXTENDED generation settings).  These live in their OWN table because the
# frozen Phase 5 capability_overrides triggers admit exactly the closed
# Phase 5 capability key set; the frozen migration is not touched and the
# frozen evidence schema keeps validating historical rows unchanged.  The
# three legacy capability keys stay in capability_overrides exclusively.
_SETTING_CAPABILITY_KEY_LIST = (
    "request.top_p", "request.top_k", "request.min_p", "request.typical_p",
    "request.tail_free_sampling", "request.smoothing_factor",
    "request.frequency_penalty", "request.presence_penalty",
    "request.repetition_penalty", "request.repetition_window",
    "request.stop_sequences", "request.ignore_eos", "request.seed",
    "request.reasoning_effort.level", "request.reasoning.token_budget",
    "request.logprobs", "request.top_logprobs", "request.logit_bias",
    "request.mirostat", "request.mirostat_tau", "request.mirostat_eta",
    "request.dynamic_temperature_range", "request.xtc_probability",
    "request.xtc_threshold", "request.dry_multiplier", "request.dry_base",
    "request.dry_allowed_length", "request.dry_penalty_last_n",
)


def _capability_key_sql() -> str:
    return ", ".join(f"'{key}'" for key in _SETTING_CAPABILITY_KEY_LIST)


def upgrade() -> None:
    op.create_table(
        "generation_setting_capabilities",
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("capability_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("model_entry_id", "capability_key"),
        sa.ForeignKeyConstraint(
            ["model_entry_id"],
            ["model_catalogue_entries.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("state IN ('supported', 'unsupported', 'unknown')", name="ck_generation_setting_capability_state"),
        sa.CheckConstraint(
            "reason IS NULL OR (typeof(reason) = 'text' AND length(reason) <= 256)",
            name="ck_generation_setting_capability_reason",
        ),
        sa.CheckConstraint("revision > 0", name="ck_generation_setting_capability_revision"),
    )
    op.create_index(
        "ix_generation_setting_capabilities_model",
        "generation_setting_capabilities",
        ["model_entry_id"],
    )
    _create_truth_triggers()
    op.create_table(
        "application_generation_settings_extra",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("extra_settings_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_application_generation_settings_extra_singleton"),
        sa.CheckConstraint(_JSON_OBJECT_CHECK, name="ck_application_generation_settings_extra_json"),
        sa.CheckConstraint(_REVISION_CHECK, name="ck_application_generation_settings_extra_revision"),
    )
    op.create_table(
        "model_generation_settings_extra",
        sa.Column(
            "model_entry_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("extra_settings_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("model_entry_id"),
        sa.ForeignKeyConstraint(
            ["model_entry_id"],
            ["model_catalogue_entries.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_JSON_OBJECT_CHECK, name="ck_model_generation_settings_extra_json"),
        sa.CheckConstraint(_REVISION_CHECK, name="ck_model_generation_settings_extra_revision"),
    )
    op.create_table(
        "chat_model_generation_settings_extra",
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("extra_settings_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "model_entry_id"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["model_entry_id"],
            ["model_catalogue_entries.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_JSON_OBJECT_CHECK, name="ck_chat_model_generation_settings_extra_json"),
        sa.CheckConstraint(_REVISION_CHECK, name="ck_chat_model_generation_settings_extra_revision"),
    )


def _create_truth_triggers() -> None:
    """SQL-level truth triggers for the extended capability override keys.

    Mirror the frozen Phase 5 override-truth discipline: only the registry's
    per-setting capability keys are admitted, non-limit capabilities never
    carry a value, and identity/reason/revision shapes stay bounded.
    """
    key_sql = _capability_key_sql()
    op.execute(sa.text(f"""
        CREATE TRIGGER phase11_setting_capability_truth_validate_insert
        BEFORE INSERT ON generation_setting_capabilities
        WHEN NEW.capability_key NOT IN ({key_sql})
          OR typeOf(NEW.revision) <> 'integer'
          OR NEW.revision < 1
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at
          OR NEW.state NOT IN ('supported', 'unsupported', 'unknown')
          OR (NEW.reason IS NOT NULL AND (
              typeof(NEW.reason) <> 'text' OR length(NEW.reason) > 256
          ))
        BEGIN SELECT RAISE(ABORT, 'Phase 11 setting capability override truth is malformed'); END
    """))
    op.execute(sa.text(f"""
        CREATE TRIGGER phase11_setting_capability_truth_validate_update
        BEFORE UPDATE OF capability_key, state, reason, revision, updated_at ON generation_setting_capabilities
        WHEN NEW.capability_key NOT IN ({key_sql})
          OR typeOf(NEW.revision) <> 'integer'
          OR NEW.revision < 1
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at
          OR (NEW.reason IS NOT NULL AND (
              typeof(NEW.reason) <> 'text' OR length(NEW.reason) > 256
          ))
        BEGIN SELECT RAISE(ABORT, 'Phase 11 setting capability override truth is malformed'); END
    """))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS phase11_setting_capability_truth_validate_update")
    op.execute("DROP TRIGGER IF EXISTS phase11_setting_capability_truth_validate_insert")
    op.drop_index("ix_generation_setting_capabilities_model")
    op.drop_table("generation_setting_capabilities")
    op.drop_table("chat_model_generation_settings_extra")
    op.drop_table("model_generation_settings_extra")
    op.drop_table("application_generation_settings_extra")
