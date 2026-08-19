"""SQLite engine and session helpers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DATA_DIR = Path(os.environ.get("ES_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "episode-sorter.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _sql_default(column) -> str | None:
    """Literal for the column default so NOT NULL columns can be added afterwards."""
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if value in ([], {}):
        return "'[]'" if value == [] else "'{}'"
    return None


def ensure_schema(base) -> list[str]:
    """Add columns that new versions introduced. SQLite has no migrations here."""
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                definition = CreateColumn(column).compile(engine).string
                # SQLite cannot add a primary key or a unique column afterwards
                definition = definition.replace(" PRIMARY KEY", "").replace(" UNIQUE", "")
                if "NOT NULL" in definition:
                    literal = _sql_default(column)
                    if literal is None:
                        definition = definition.replace(" NOT NULL", "")
                    else:
                        definition = f"{definition} DEFAULT {literal}"
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {definition}'))
                added.append(f"{table.name}.{column.name}")
    return added


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
