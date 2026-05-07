"""Database engine and session helpers."""

from collections.abc import Iterator

from sqlmodel import Session, create_engine

from bankruptcy.config import settings

engine = create_engine(
    settings.sqlalchemy_database_url,
    echo=False,
    pool_pre_ping=True,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a transactional DB session."""
    with Session(engine) as session:
        yield session
