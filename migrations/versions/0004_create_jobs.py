"""empty message

Revision ID: c929d81b9e4a
Revises: 0003_create_tokens
Create Date: 2016-01-15 10:12:02.381160

"""

# revision identifiers, used by Alembic.
revision = '0004_create_jobs'
down_revision = '0003_create_tokens'

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.create_table('jobs',
    sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('original_file_name', sa.String(), nullable=False),
    sa.Column('service_id', sa.BigInteger(), nullable=True),
    sa.Column('template_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ),
    sa.ForeignKeyConstraint(['template_id'], ['templates.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_service_id'), 'jobs', ['service_id'], unique=False)
    op.create_index(op.f('ix_jobs_template_id'), 'jobs', ['template_id'], unique=False)
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_jobs_template_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_service_id'), table_name='jobs')
    op.drop_table('jobs')
    ### end Alembic commands ###
