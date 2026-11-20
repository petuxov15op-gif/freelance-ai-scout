"""Store individual announcements within a Telegram publication.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    naming = {"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"}
    with op.batch_alter_table("messages", naming_convention=naming) as table:
        table.add_column(sa.Column("part_number", sa.Integer(), nullable=False, server_default="0"))
        table.drop_constraint("uq_messages_channel_id_telegram_message_id", type_="unique")
        table.create_unique_constraint("uq_messages_post_part", ["channel_id", "telegram_message_id", "part_number"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM messages WHERE part_number > 0 LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while separately stored announcements exist")
    with op.batch_alter_table("messages") as table:
        table.drop_constraint("uq_messages_post_part", type_="unique")
        table.create_unique_constraint("uq_messages_channel_id_telegram_message_id", ["channel_id", "telegram_message_id"])
        table.drop_column("part_number")
