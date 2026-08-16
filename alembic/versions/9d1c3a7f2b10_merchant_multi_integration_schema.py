"""merchant ownership and Core-generated transaction references

Revision ID: 9d1c3a7f2b10
Revises:
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import nexolu_payments_core.core.security.crypto

revision: str = "9d1c3a7f2b10"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("merchants", sa.Column("id", sa.String(32), nullable=False), sa.Column("slug", sa.String(64), nullable=False), sa.Column("name", sa.String(128), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("slug"))
    op.create_table("integrations", sa.Column("id", sa.String(32), nullable=False), sa.Column("merchant_id", sa.String(32), nullable=False), sa.Column("slug", sa.String(64), nullable=False), sa.Column("name", sa.String(128), nullable=False), sa.Column("environment", sa.String(16), nullable=False), sa.Column("api_key", nexolu_payments_core.core.security.crypto.EncryptedString(255), nullable=False), sa.Column("api_key_hash", sa.String(64), nullable=False), sa.Column("webhook_url", sa.String(512), nullable=True), sa.Column("webhook_secret", nexolu_payments_core.core.security.crypto.EncryptedString(255), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("api_key_hash"), sa.UniqueConstraint("slug"))
    op.create_index("ix_integrations_merchant_id", "integrations", ["merchant_id"], unique=False)
    op.create_table("provider_credentials", sa.Column("id", sa.String(32), nullable=False), sa.Column("merchant_id", sa.String(32), nullable=False), sa.Column("provider_slug", sa.String(32), nullable=False), sa.Column("environment", sa.String(16), nullable=False), sa.Column("public_key", sa.String(255), nullable=False), sa.Column("private_key", nexolu_payments_core.core.security.crypto.EncryptedString(255), nullable=False), sa.Column("integrity_secret", nexolu_payments_core.core.security.crypto.EncryptedString(255), nullable=False), sa.Column("events_secret", nexolu_payments_core.core.security.crypto.EncryptedString(255), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("merchant_id", "provider_slug", "environment", name="uq_credential_merchant_provider_env"))
    op.create_index("ix_provider_credentials_merchant_id", "provider_credentials", ["merchant_id"], unique=False)
    op.create_table("fee_schedules", sa.Column("id", sa.String(32), nullable=False), sa.Column("merchant_id", sa.String(32), nullable=False), sa.Column("integration_id", sa.String(32), nullable=True), sa.Column("provider_slug", sa.String(32), nullable=False), sa.Column("percent_fee", sa.Float(), nullable=False), sa.Column("fixed_fee_cop", sa.Integer(), nullable=False), sa.Column("iva_percent", sa.Float(), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False), sa.Column("effective_from", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]), sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"]), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_fee_schedules_merchant", "fee_schedules", ["merchant_id", "provider_slug", "is_active"], unique=False)
    op.create_index("ix_fee_schedules_integration_id", "fee_schedules", ["integration_id"], unique=False)
    op.create_table("transactions", sa.Column("id", sa.String(32), nullable=False), sa.Column("merchant_id", sa.String(32), nullable=False), sa.Column("integration_id", sa.String(32), nullable=False), sa.Column("provider_slug", sa.String(32), nullable=False), sa.Column("reference", sa.String(128), nullable=False), sa.Column("provider_transaction_id", sa.String(128), nullable=True), sa.Column("amount_cop", sa.Integer(), nullable=False), sa.Column("currency", sa.String(8), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("fee_cop", sa.Integer(), nullable=True), sa.Column("net_amount_cop", sa.Integer(), nullable=True), sa.Column("customer_email", sa.String(255), nullable=True), sa.Column("extra_metadata", sa.JSON(), nullable=False), sa.Column("payload", sa.JSON(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("confirmed_at", sa.DateTime(), nullable=True), sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]), sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"]), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("reference", name="uq_transaction_reference"))
    op.create_index("ix_transactions_reference", "transactions", ["reference"], unique=False)
    op.create_index("ix_transactions_merchant_id", "transactions", ["merchant_id"], unique=False)
    op.create_index("ix_transactions_integration_id", "transactions", ["integration_id"], unique=False)
    op.create_index("ix_transactions_merchant_status", "transactions", ["merchant_id", "status"], unique=False)
    op.create_index("ix_transactions_integration_status", "transactions", ["integration_id", "status"], unique=False)
    op.create_table("webhook_deliveries", sa.Column("id", sa.String(32), nullable=False), sa.Column("transaction_id", sa.String(32), nullable=False), sa.Column("integration_id", sa.String(32), nullable=False), sa.Column("event", sa.String(32), nullable=False), sa.Column("url", sa.String(512), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("attempt_count", sa.Integer(), nullable=False), sa.Column("last_status_code", sa.Integer(), nullable=True), sa.Column("last_error", sa.Text(), nullable=True), sa.Column("delivered_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]), sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"]), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_webhook_deliveries_transaction", "webhook_deliveries", ["transaction_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_transaction", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_transactions_integration_status", table_name="transactions")
    op.drop_index("ix_transactions_merchant_status", table_name="transactions")
    op.drop_index("ix_transactions_integration_id", table_name="transactions")
    op.drop_index("ix_transactions_merchant_id", table_name="transactions")
    op.drop_index("ix_transactions_reference", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_fee_schedules_integration_id", table_name="fee_schedules")
    op.drop_index("ix_fee_schedules_merchant", table_name="fee_schedules")
    op.drop_table("fee_schedules")
    op.drop_index("ix_provider_credentials_merchant_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
    op.drop_index("ix_integrations_merchant_id", table_name="integrations")
    op.drop_table("integrations")
    op.drop_table("merchants")
