"""add transactions.redirect_url

Revision ID: b3f7a1c9e5d2
Revises: 9d1c3a7f2b10
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b3f7a1c9e5d2"
down_revision: Union[str, None] = "9d1c3a7f2b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("redirect_url", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "redirect_url")
