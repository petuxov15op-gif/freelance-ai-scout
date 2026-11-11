"""Keep widget save and hide flags separate from application status.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("jobs") as table:
        table.add_column(sa.Column("saved", sa.Boolean(), nullable=False, server_default="0"))
        table.add_column(sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE jobs SET saved = 1 WHERE status = 'interesting'"))
    op.execute(sa.text("UPDATE jobs SET hidden = 1 WHERE status = 'irrelevant'"))


def downgrade():
    with op.batch_alter_table("jobs") as table:
        table.drop_column("hidden")
        table.drop_column("saved")
