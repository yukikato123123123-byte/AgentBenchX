"""Enforce version immutability even for direct SQL updates and deletes."""

from alembic import op

revision = "002_version_guards"
down_revision = "64a33946b0c4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION reject_version_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'Version records are immutable'; END;
    $$ LANGUAGE plpgsql""")
    for table in ("agent_versions", "problem_versions"):
        op.execute(
            f"CREATE TRIGGER immutable_version BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()"
        )


def downgrade():
    for table in ("agent_versions", "problem_versions"):
        op.execute(f"DROP TRIGGER immutable_version ON {table}")
    op.execute("DROP FUNCTION reject_version_mutation()")
