import enum
from collections.abc import Iterator

from sqlalchemy import Enum, MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Deterministic constraint names so Alembic migrations can reference them
# (SQLite in particular cannot drop unnamed constraints).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def str_enum(enum_cls: type[enum.Enum], length: int = 20) -> Enum:
    """Column type storing enum *values* (e.g. ``"posted"``) as VARCHAR.

    SQLAlchemy would otherwise persist member names (``"POSTED"``). Storing the
    values keeps the database readable and lets the ledger triggers in the
    migrations refer to the same literals the API exposes.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


def make_engine(url: str) -> Engine:
    """Engine with the transaction semantics the services rely on.

    PostgreSQL: ordinary READ COMMITTED transactions; services take explicit row
    locks (``SELECT ... FOR UPDATE / FOR SHARE``) where validation must stay
    valid until commit (see ``app.services.locking``).

    SQLite: pysqlite's legacy mode starts a transaction only at the first DML
    statement, so reads run outside any transaction and "validate, then write"
    is not isolated. We therefore drive transactions ourselves and open every
    one with ``BEGIN IMMEDIATE``: the transaction holds SQLite's single write
    lock from its first statement, so competing writers wait (up to the busy
    timeout) instead of interleaving. Pure readers are serialised with writers
    too, which is acceptable for SQLite's role (development and tests).
    """
    if not url.startswith("sqlite"):
        return create_engine(url)

    engine = create_engine(
        url,
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            "isolation_level": None,  # disable pysqlite's implicit BEGIN
        },
    )

    @event.listens_for(engine, "begin")
    def begin_immediate(connection) -> None:  # pragma: no cover - trivial
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
