"""Create the Phase 1 desktop state tables."""

from alembic import op
import sqlalchemy as sa


revision = "0001_desktop_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["messages.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_messages_chat_sequence",
        "messages",
        ["chat_id", "sequence"],
        unique=True,
    )
    op.create_table(
        "generation_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("user_message_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=64), nullable=False),
        sa.Column("backend_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("request_snapshot", sa.Text(), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("ended_at", sa.String(length=40), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("generation_attempts")
    op.drop_index("ix_messages_chat_sequence", table_name="messages")
    op.drop_table("messages")
    op.drop_table("chats")
