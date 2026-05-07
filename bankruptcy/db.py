"""Database engine and session helpers."""

from collections.abc import Iterator

from sqlmodel import Session, create_engine

from bankruptcy.config import settings

# `prepare_threshold=None` disables psycopg3 server-side prepared statements.
# Required when running through pgbouncer's transaction pooler (which
# Supabase's :6543 endpoint uses), because the pooler reuses connections
# across transactions and breaks the prepared-statement cache.
engine = create_engine(
    settings.sqlalchemy_database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={"prepare_threshold": None},
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a transactional DB session."""
    with Session(engine) as session:
        yield session
