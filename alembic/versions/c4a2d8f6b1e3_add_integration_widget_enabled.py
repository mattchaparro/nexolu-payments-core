"""add integrations.widget_enabled

Revision ID: c4a2d8f6b1e3
Revises: b3f7a1c9e5d2
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "c4a2d8f6b1e3"
down_revision: Union[str, None] = "b3f7a1c9e5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("integrations", sa.Column("widget_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("integrations", "widget_enabled")
