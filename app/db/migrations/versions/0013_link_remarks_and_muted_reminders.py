"""Link remarks and muted reminders

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-01 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
import app.db.models


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('link_reminder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('muted', sa.Boolean(), nullable=False, server_default='0'))

    with op.batch_alter_table('steam_link', schema=None) as batch_op:
        batch_op.add_column(sa.Column('remark', sa.String(length=128), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('remark_only', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('steam_link', schema=None) as batch_op:
        batch_op.drop_column('remark_only')
        batch_op.drop_column('remark')

    with op.batch_alter_table('link_reminder', schema=None) as batch_op:
        batch_op.drop_column('muted')
