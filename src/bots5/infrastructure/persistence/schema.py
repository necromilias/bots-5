from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)


metadata = MetaData()

chats = Table(
    "chats",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("title", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("head_message_id", String(64), ForeignKey("messages.id", ondelete="SET NULL")),
    Column("revision", Integer, nullable=False, default=0),
    Column("archived_at", Text),
)

# Phase 7 authoritative coordination metadata.  Search rows below are derived;
# this singleton is not.
search_source_state = Table(
    "search_source_state",
    metadata,
    Column("singleton_id", Integer, primary_key=True),
    Column("source_revision", Integer, nullable=False),
    CheckConstraint("singleton_id = 1", name="ck_search_source_singleton"),
    CheckConstraint("source_revision >= 0", name="ck_search_source_revision"),
)

search_index_state = Table(
    "search_index_state",
    metadata,
    Column("singleton_id", Integer, primary_key=True),
    Column("condition", String(16), nullable=False),
    Column("checkpoint_revision", Integer, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("tokenizer_version", String(64), nullable=False),
    Column("detail", Text),
    Column("updated_at", String(40), nullable=False),
    CheckConstraint("singleton_id = 1", name="ck_search_index_singleton"),
    CheckConstraint(
        "condition IN ('VALID', 'REBUILDING', 'INVALID')",
        name="ck_search_index_condition",
    ),
    CheckConstraint("checkpoint_revision >= 0", name="ck_search_checkpoint_revision"),
    CheckConstraint("generation >= 0", name="ck_search_generation"),
    CheckConstraint("schema_version = 1", name="ck_search_schema_version"),
    CheckConstraint(
        "tokenizer_version = 'unicode61-v1'",
        name="ck_search_tokenizer_version",
    ),
)

search_document_keys = Table(
    "search_document_keys",
    metadata,
    Column("fts_rowid", Integer, primary_key=True),
    Column("document_kind", String(16), nullable=False),
    Column("document_id", String(64), nullable=False),
    CheckConstraint(
        "document_kind IN ('chat', 'message', 'attachment')",
        name="ck_search_document_kind",
    ),
    UniqueConstraint(
        "document_kind", "document_id", name="ux_search_document_identity"
    ),
)

messages = Table(
    "messages",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("chat_id", String(64), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
    Column("parent_id", String(64), ForeignKey("messages.id", ondelete="SET NULL")),
    Column("sequence", Integer, nullable=False),
    Column("role", String(32), nullable=False),
    Column("state", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("lineage_id", String(64), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("supersedes_id", String(64), ForeignKey("messages.id", ondelete="RESTRICT")),
    Index("ix_messages_chat_sequence", "chat_id", "sequence", unique=True),
    Index("ix_messages_chat_lineage_revision", "chat_id", "lineage_id", "revision", unique=True),
    Index("ix_messages_chat_parent_sequence", "chat_id", "parent_id", "sequence"),
)

generation_attempts = Table(
    "generation_attempts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("chat_id", String(64), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
    Column(
        "user_message_id",
        String(64),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "assistant_message_id",
        String(64),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("backend_id", String(64), nullable=False),
    Column("model", Text, nullable=False),
    Column("state", String(32), nullable=False),
    Column("request_snapshot", Text, nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("ended_at", String(40)),
    Column("error_type", String(128)),
    Column("error_message", Text),
    Column("provider_id", String(64)),
    Column("returned_model", Text),
    Column("request_id", Text),
    Column("finish_reason", String(128)),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("reasoning_tokens", Integer),
    Column("total_tokens", Integer),
    Column("known_cost_usd", Text),
    Column("remote_outcome_unknown", Boolean),
    Column("connection_id", String(64), ForeignKey("provider_connections.id", ondelete="RESTRICT")),
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="RESTRICT")),
    Index("ux_generation_attempts_assistant", "assistant_message_id", unique=True),
    Index(
        "ux_generation_attempts_active_chat",
        "chat_id",
        unique=True,
        sqlite_where=text("state = 'running'"),
    ),
)

provider_connections = Table(
    "provider_connections",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", Text, nullable=False),
    Column("name_key", String(256), nullable=False, unique=True),
    Column("backend_type", String(64), nullable=False),
    Column("profile", String(64), nullable=False),
    Column("endpoint", Text),
    Column("credential_source", String(64), nullable=False),
    Column("credential_reference", Text),
    Column("enabled", Boolean, nullable=False, server_default=text("1")),
    Column("retired", Boolean, nullable=False, server_default=text("0")),
    Column("revision", Integer, nullable=False),
    Column("catalogue_revision", Integer, nullable=False, server_default=text("0")),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("ix_provider_connections_enabled", "enabled", "retired"),
)

catalogue_refresh_state = Table(
    "catalogue_refresh_state",
    metadata,
    Column(
        "connection_id",
        String(64),
        ForeignKey("provider_connections.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("status", String(32), nullable=False),
    Column("refresh_revision", Integer, nullable=False),
    Column("failure_class", String(32)),
    Column("failure_message", Text),
    Column("updated_at", String(40), nullable=False),
    CheckConstraint(
        "status IN ('never', 'succeeded', 'failed')",
        name="ck_catalogue_refresh_status",
    ),
    CheckConstraint(
        "refresh_revision >= 0",
        name="ck_catalogue_refresh_revision",
    ),
    CheckConstraint(
        "failure_class IS NULL OR failure_class IN ('transport', 'timeout', 'provider_http', 'protocol', 'unknown')",
        name="ck_catalogue_refresh_failure_class",
    ),
    CheckConstraint(
        "failure_message IS NULL OR failure_message IN ('provider is unreachable', 'provider discovery timed out', 'provider returned an HTTP error', 'provider returned an invalid model catalogue', 'provider discovery failed')",
        name="ck_catalogue_refresh_failure_message",
    ),
    CheckConstraint(
        "(status = 'failed' AND failure_class IS NOT NULL AND failure_message IS NOT NULL) "
        "OR (status <> 'failed' AND failure_class IS NULL AND failure_message IS NULL)",
        name="ck_catalogue_refresh_failure_consistency",
    ),
    CheckConstraint(
        "(status = 'never' AND refresh_revision = 0) "
        "OR (status <> 'never' AND refresh_revision > 0)",
        name="ck_catalogue_refresh_revision_consistency",
    ),
    CheckConstraint(
        "(failure_class = 'transport' AND failure_message = 'provider is unreachable') "
        "OR (failure_class = 'timeout' AND failure_message = 'provider discovery timed out') "
        "OR (failure_class = 'provider_http' AND failure_message = 'provider returned an HTTP error') "
        "OR (failure_class = 'protocol' AND failure_message = 'provider returned an invalid model catalogue') "
        "OR (failure_class = 'unknown' AND failure_message = 'provider discovery failed') "
        "OR (failure_class IS NULL AND failure_message IS NULL)",
        name="ck_catalogue_refresh_failure_diagnostic",
    ),
)

model_catalogue_entries = Table(
    "model_catalogue_entries",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("connection_id", String(64), ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False),
    Column("provider_model_id", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("origin", String(64), nullable=False),
    Column("availability", String(64), nullable=False),
    Column("discovery_revision", Integer),
    Column("discovered_at", String(40)),
    Column("metadata_json", Text, nullable=False, server_default=text("'{}'")),
    Column("revision", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("connection_id", "provider_model_id", name="ux_model_catalogue_connection_model"),
    Index("ix_model_catalogue_connection_availability", "connection_id", "availability"),
)

capability_facts = Table(
    "capability_facts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="CASCADE"), nullable=False),
    Column("capability_key", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("source", String(64), nullable=False),
    Column("source_revision", Integer),
    Column("value", Integer),
    Column("provenance_json", Text, nullable=False, server_default=text("'{}'")),
    Column("observed_at", String(40), nullable=False),
    Index("ix_capability_facts_model_key", "model_entry_id", "capability_key"),
)

capability_overrides = Table(
    "capability_overrides",
    metadata,
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="CASCADE"), nullable=False),
    Column("capability_key", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("value", Integer),
    Column("reason", Text),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("model_entry_id", "capability_key", name="ux_capability_override_model_key"),
)

capability_observations = Table(
    "capability_observations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="CASCADE"), nullable=False),
    Column("capability_key", String(128), nullable=False),
    Column("observed_state", String(32), nullable=False),
    Column("detail", Text, nullable=False),
    Column("observed_at", String(40), nullable=False),
)

application_generation_config = Table(
    "application_generation_config",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("default_model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="RESTRICT")),
    Column("temperature", Text, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("reasoning_effort", String(32)),
    Column("timeout_seconds", Text),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

model_generation_config = Table(
    "model_generation_config",
    metadata,
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("temperature", Text),
    Column("max_output_tokens", Integer),
    Column("reasoning_effort", String(32)),
    Column("timeout_seconds", Text),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

chat_model_generation_config = Table(
    "chat_model_generation_config",
    metadata,
    Column("chat_id", String(64), ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("temperature", Text),
    Column("max_output_tokens", Integer),
    Column("reasoning_effort", String(32)),
    Column("timeout_seconds", Text),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

# Phase 6: durable content-addressed attachment identity and frozen context
# evidence.  These tables are additive; Phase 1-5 rows remain readable.
attachment_blobs = Table(
    "attachment_blobs",
    metadata,
    Column("digest", LargeBinary(32), primary_key=True),
    Column("byte_size", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    Column("operation_id", String(36)),
    Column("stage_name", String(36)),
    Column("gc_id", String(64)),
    Column("created_at", String(40), nullable=False),
)

attachments = Table(
    "attachments",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("blob_digest", LargeBinary(32), ForeignKey("attachment_blobs.digest", ondelete="RESTRICT"), nullable=False),
    Column("filename", Text, nullable=False),
    Column("source_kind", String(32), nullable=False),
    Column("source_name", Text, nullable=False),
    Column("text_representation_id", LargeBinary(32)),
    Column("text_digest", LargeBinary(32)),
    Column("ineligibility_reason", String(64)),
    Column("created_at", String(40), nullable=False),
)

message_attachments = Table(
    "message_attachments",
    metadata,
    Column("message_id", String(64), ForeignKey("messages.id", ondelete="RESTRICT"), primary_key=True),
    Column("attachment_id", String(64), ForeignKey("attachments.id", ondelete="RESTRICT"), primary_key=True),
    Column("ordinal", Integer, nullable=False),
)

attempt_attachments = Table(
    "attempt_attachments",
    metadata,
    Column("attempt_id", String(64), ForeignKey("generation_attempts.id", ondelete="RESTRICT"), primary_key=True),
    Column("attachment_id", String(64), ForeignKey("attachments.id", ondelete="RESTRICT"), primary_key=True),
    Column("ordinal", Integer, nullable=False),
)

context_plans = Table(
    "context_plans",
    metadata,
    Column("attempt_id", String(64), ForeignKey("generation_attempts.id", ondelete="RESTRICT"), primary_key=True),
    Column("plan_version", Integer, nullable=False),
    Column("canonical_representation", Text, nullable=False),
    Column("canonical_digest", String(64), nullable=False),
    Column("wire_representation_digest", String(64), nullable=False),
    Column("budget_limit", Integer, nullable=False),
    Column("budget_provenance", Text, nullable=False),
    Column("budget_semantics", Text, nullable=False),
    Column("adapter_id", Text, nullable=False),
    Column("adapter_version", Text, nullable=False),
    Column("input_counts", Text, nullable=False),
    Column("envelope_overhead", Integer, nullable=False),
    Column("output_reserve", Integer, nullable=False),
    Column("input_units", Integer, nullable=False),
    Column("total_units", Integer, nullable=False),
    Column("headroom", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
)


chat_model_selection = Table(
    "chat_model_selection",
    metadata,
    Column("chat_id", String(64), ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
    Column("model_entry_id", String(64), ForeignKey("model_catalogue_entries.id", ondelete="RESTRICT")),
    Column("selection_required", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

workspace_windows = Table(
    "workspace_windows",
    metadata,
    Column("window_id", String(128), primary_key=True),
    Column("ordinal", Integer, nullable=False),
    Column("geometry_json", Text, nullable=True),
    Column("selected_chat_id", String(64), ForeignKey("chats.id", ondelete="SET NULL")),
    Column("rail_collapsed", Boolean, nullable=False, default=False),
    Column("restore_open", Boolean, nullable=False, default=True),
    Column("updated_at", String(40), nullable=False),
)
