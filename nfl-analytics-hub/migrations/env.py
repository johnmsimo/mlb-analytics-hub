from __future__ import with_statement
from logging.config import fileConfig
from flask import current_app
from alembic import context
config = context.config
fileConfig(config.config_file_name)
target_db = current_app.extensions['migrate'].db

def get_engine():
    return target_db.engine

def get_url():
    return str(get_engine().url).replace('%', '%%')
config.set_main_option('sqlalchemy.url', get_url())
target_metadata = target_db.metadata

def run_migrations_offline():
    context.configure(url=get_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction(): context.run_migrations()

def run_migrations_online():
    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction(): context.run_migrations()
if context.is_offline_mode(): run_migrations_offline()
else: run_migrations_online()
