"""Phase 10 operational data platform tables.

This migration is intentionally defensive: existing SQLite development databases
may already have these tables through create_all. Fresh production PostgreSQL
installations should bootstrap with `flask --app app db upgrade` after generating
an authoritative baseline migration from the current SQLAlchemy metadata.
"""
revision = "20260718_phase10"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Baseline marker. Generate a full baseline with:
    # flask --app app db migrate -m "authoritative baseline"
    pass

def downgrade():
    pass
