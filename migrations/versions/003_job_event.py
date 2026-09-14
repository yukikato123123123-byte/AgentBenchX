"""Remember job phase across API processes without Redis heartbeat writes."""

import sqlalchemy as sa
from alembic import op

revision = "003_job_event"
down_revision = "002_version_guards"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runner_jobs", sa.Column("last_event", sa.String(50), nullable=True))


def downgrade():
    op.drop_column("runner_jobs", "last_event")
