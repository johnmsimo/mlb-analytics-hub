"""phase 12 credentialed integrations
Revision ID: 20260718_phase12
Revises: 20260718_phase11
"""
from alembic import op
import sqlalchemy as sa
revision="20260718_phase12"; down_revision="20260718_phase11"; branch_labels=None; depends_on=None

def upgrade():
    op.create_table("weather_observations", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("game_id",sa.Integer(),sa.ForeignKey("games.id",ondelete="CASCADE"),nullable=False), sa.Column("observed_at",sa.DateTime(timezone=True),nullable=False), sa.Column("temperature_f",sa.Float()), sa.Column("feels_like_f",sa.Float()), sa.Column("humidity_pct",sa.Float()), sa.Column("pressure_hpa",sa.Float()), sa.Column("wind_speed_mph",sa.Float()), sa.Column("wind_gust_mph",sa.Float()), sa.Column("wind_direction_deg",sa.Float()), sa.Column("precipitation_mm",sa.Float()), sa.Column("cloud_pct",sa.Float()), sa.Column("condition",sa.String(80)), sa.Column("source_key",sa.String(80),nullable=False), sa.Column("raw_payload",sa.JSON()), sa.Column("created_at",sa.DateTime(timezone=True),nullable=False), sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False), sa.UniqueConstraint("game_id","observed_at","source_key",name="uq_weather_observation"))
    op.create_index("ix_weather_game_time","weather_observations",["game_id","observed_at"])
    op.create_table("odds_snapshots", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("game_id",sa.Integer(),sa.ForeignKey("games.id",ondelete="CASCADE"),nullable=False), sa.Column("provider_event_id",sa.String(120)), sa.Column("bookmaker",sa.String(80),nullable=False), sa.Column("market",sa.String(40),nullable=False), sa.Column("outcome",sa.String(160),nullable=False), sa.Column("line",sa.Float()), sa.Column("price_american",sa.Integer()), sa.Column("captured_at",sa.DateTime(timezone=True),nullable=False), sa.Column("source_key",sa.String(80),nullable=False), sa.Column("raw_payload",sa.JSON()), sa.Column("created_at",sa.DateTime(timezone=True),nullable=False), sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False), sa.UniqueConstraint("game_id","bookmaker","market","outcome","captured_at",name="uq_odds_snapshot"))
    op.create_index("ix_odds_game_market_time","odds_snapshots",["game_id","market","captured_at"])

def downgrade():
    op.drop_table("odds_snapshots"); op.drop_table("weather_observations")
