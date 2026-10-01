"""Guard for the INFRA-52 fast create_all in conftest.py.

The fast path replays DDL read back from sqlite_master. If the schema ever
gains something that replay cannot reproduce (AUTOINCREMENT, triggers, FTS,
create-time event hooks), the replayed schema drifts from the real one and
this test fails.
"""

from sqlalchemy import MetaData, create_engine

from app.models import Base


def _schema(engine) -> list[tuple]:
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).all()


def test_fast_create_all_matches_real_create_all() -> None:
    fast = create_engine("sqlite://")
    real = create_engine("sqlite://")
    with fast.begin() as conn:
        Base.metadata.create_all(conn)  # patched by the conftest fixture
    MetaData.create_all(Base.metadata, real)  # unpatched class method

    assert _schema(fast) == _schema(real)
    assert _schema(fast)  # the fast path really built something
