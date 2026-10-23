"""Explicit latest-only mode; existing subscriptions keep their list behavior."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"


def upgrade():
    op.add_column("feeds", sa.Column("collection_mode", sa.String(), nullable=False, server_default="list"))


def downgrade():
    raise RuntimeError("Restore a backup to roll back this migration.")
