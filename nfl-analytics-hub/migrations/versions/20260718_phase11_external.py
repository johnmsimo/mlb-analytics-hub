"""phase 11 external datasets

Revision ID: 20260718_phase11
Revises: 20260718_phase10
"""
from alembic import op
import sqlalchemy as sa
revision = "20260718_phase11"
down_revision = "20260718_phase10"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("injury_reports",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False), sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False), sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("game_status", sa.String(40)), sa.Column("practice_status", sa.String(40)),
        sa.Column("primary_injury", sa.String(120)), sa.Column("secondary_injury", sa.String(120)),
        sa.Column("source_key", sa.String(80), nullable=False), sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["season"], ["seasons.year"]), sa.UniqueConstraint("player_id", "team_id", "season", "week", "report_date", name="uq_injury_report"))
    op.create_index("ix_injury_current", "injury_reports", ["season", "week", "team_id", "game_status"])
    op.create_table("depth_chart_entries",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("player_id", sa.Integer(), nullable=False), sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False), sa.Column("week", sa.Integer()), sa.Column("chart_date", sa.Date(), nullable=False),
        sa.Column("position", sa.String(16)), sa.Column("depth_position", sa.String(24)), sa.Column("depth_rank", sa.Integer()),
        sa.Column("source_key", sa.String(80), nullable=False), sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["season"], ["seasons.year"]), sa.UniqueConstraint("player_id", "team_id", "chart_date", "depth_position", name="uq_depth_chart_entry"))
    op.create_index("ix_depth_chart_current", "depth_chart_entries", ["season", "week", "team_id", "depth_position", "depth_rank"])
    op.create_table("snap_counts",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("game_id", sa.Integer(), nullable=False), sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False), sa.Column("season", sa.Integer(), nullable=False), sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("offense_snaps", sa.Integer()), sa.Column("offense_pct", sa.Float()), sa.Column("defense_snaps", sa.Integer()), sa.Column("defense_pct", sa.Float()),
        sa.Column("special_teams_snaps", sa.Integer()), sa.Column("special_teams_pct", sa.Float()), sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("raw_payload", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]), sa.UniqueConstraint("game_id", "player_id", name="uq_snap_count"))
    op.create_index("ix_snap_player_season", "snap_counts", ["player_id", "season", "week"])
    op.create_table("league_transactions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("external_id", sa.String(120), nullable=False), sa.Column("player_id", sa.Integer()),
        sa.Column("team_id", sa.Integer()), sa.Column("transaction_date", sa.Date(), nullable=False), sa.Column("transaction_type", sa.String(80)),
        sa.Column("description", sa.Text()), sa.Column("source_key", sa.String(80), nullable=False), sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("external_id"))
    op.create_index("ix_league_transactions_external_id", "league_transactions", ["external_id"], unique=True)

def downgrade():
    op.drop_table("league_transactions"); op.drop_table("snap_counts"); op.drop_table("depth_chart_entries"); op.drop_table("injury_reports")
