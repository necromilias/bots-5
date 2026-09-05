from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, MetaData, String, Table, Text, text


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
    Index("ux_generation_attempts_assistant", "assistant_message_id", unique=True),
    Index(
        "ux_generation_attempts_active_chat",
        "chat_id",
        unique=True,
        sqlite_where=text("state = 'running'"),
    ),
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
