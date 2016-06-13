"""empty message

Revision ID: 0027_update_provider_rates
Revises: 0026_rename_notify_service
Create Date: 2016-06-08 01:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = '0027_update_provider_rates'
down_revision = '0026_rename_notify_service'

from alembic import op
import sqlalchemy as sa
from datetime import datetime
import uuid


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.get_bind()
    op.execute((
        "INSERT INTO provider_rates (id, valid_from, rate, provider_id) VALUES ('{}', '{}', 1.8, "
        "(SELECT id FROM provider_details WHERE identifier = 'mmg'))").format(uuid.uuid4(), datetime.utcnow()))
    op.execute((
        "INSERT INTO provider_rates (id, valid_from, rate, provider_id) VALUES ('{}', '{}', 2.5, "
        "(SELECT id FROM provider_details WHERE identifier = 'firetext'))").format(uuid.uuid4(), datetime.utcnow()))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.get_bind()
    op.execute("DELETE FROM provider_rates")
    ### end Alembic commands ###