"""Alembic environment: always run from app.db.migrate with an open connection (``config.attributes``)."""

from alembic import context

from app.db.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through app.db.migrate (python -m app migrate), not the alembic CLI")

context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True,
                  compare_type=True)
with context.begin_transaction():
    context.run_migrations()
