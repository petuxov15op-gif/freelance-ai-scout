"""Record Telegram applications and replies."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outreach",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), primary_key=True, autoincrement=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("peer_id", sa.Integer()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("sent_message_id", sa.Integer()),
        sa.Column("problem", sa.String(100)),
    )
    op.create_index("ix_outreach_peer_id", "outreach", ["peer_id"])
    op.create_index("ix_outreach_started_at", "outreach", ["started_at"])
    op.create_table(
        "outreach_replies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("outreach.job_id"), nullable=False),
        sa.Column("peer_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("peer_id", "message_id"),
    )
    op.create_index("ix_outreach_replies_job_id", "outreach_replies", ["job_id"])
    op.create_index("ix_outreach_replies_received_at", "outreach_replies", ["received_at"])


def downgrade():
    for table in ("outreach_replies", "outreach"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Cannot downgrade while Telegram application records exist")
    op.drop_table("outreach_replies")
    op.drop_table("outreach")
