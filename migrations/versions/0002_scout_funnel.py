"""Preserve existing jobs while adding funnel, contact and CRM fields.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("search_queries") as table:
        for name in ("searches", "new_sources", "suitable_jobs", "duplicates", "applications", "replies"):
            table.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        table.add_column(sa.Column("group", sa.String(30), nullable=False, server_default="development"))
        table.add_column(sa.Column("last_success_at", sa.DateTime(timezone=True)))
        table.add_column(sa.Column("last_error", sa.Text()))
    with op.batch_alter_table("messages") as table:
        table.add_column(sa.Column("rejection_reason", sa.String(60)))
    with op.batch_alter_table("jobs") as table:
        table.add_column(sa.Column("employment_type", sa.String(30), nullable=False, server_default="one_time_project"))
        table.add_column(sa.Column("contact_status", sa.String(20), nullable=False, server_default="unknown"))
        table.add_column(sa.Column("contact_method", sa.String(20), nullable=False, server_default="Другое"))
        table.add_column(sa.Column("contact_url", sa.Text()))
        table.add_column(sa.Column("applied_at", sa.DateTime(timezone=True)))
        table.add_column(sa.Column("replied_at", sa.DateTime(timezone=True)))
        table.add_column(sa.Column("amount", sa.String(100)))
        table.add_column(sa.Column("note", sa.Text()))
    op.execute(sa.text("UPDATE jobs SET applied_at = (SELECT MIN(created_at) FROM feedback "
                       "WHERE feedback.job_id = jobs.id AND feedback.action = 'applied') "
                       "WHERE EXISTS (SELECT 1 FROM feedback WHERE feedback.job_id = jobs.id "
                       "AND feedback.action = 'applied')"))


def downgrade():
    with op.batch_alter_table("jobs") as table:
        for name in ("note", "amount", "replied_at", "applied_at", "contact_url", "contact_method", "contact_status", "employment_type"):
            table.drop_column(name)
    with op.batch_alter_table("messages") as table:
        table.drop_column("rejection_reason")
    with op.batch_alter_table("search_queries") as table:
        for name in ("last_error", "last_success_at", "replies", "applications", "duplicates", "suitable_jobs", "new_sources", "searches", "group"):
            table.drop_column(name)
