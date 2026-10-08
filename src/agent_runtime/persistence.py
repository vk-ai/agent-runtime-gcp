"""Checkpointer + long-term store factory.

backend = "memory"   InMemorySaver + InMemoryStore (tests)
          "sqlite"   AsyncSqliteSaver + AsyncSqliteStore (single-file, demo)
          "postgres" AsyncPostgresSaver + AsyncPostgresStore over a psycopg pool
                     (needs the `postgres` extra; docker-compose provides a DB)
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore


@dataclass
class Persistence:
    backend: str
    checkpointer: BaseCheckpointSaver
    store: BaseStore


@contextlib.asynccontextmanager
async def open_persistence(backend: str = "memory", *, sqlite_path: str = "agent_runtime.sqlite",
                           pg_dsn: str | None = None, pool_size: int = 10) -> AsyncIterator[Persistence]:
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        yield Persistence(backend, InMemorySaver(), InMemoryStore())
        return

    if backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langgraph.store.sqlite.aio import AsyncSqliteStore

        async with AsyncSqliteSaver.from_conn_string(sqlite_path) as saver, \
                AsyncSqliteStore.from_conn_string(sqlite_path) as store:
            await saver.setup()
            await store.setup()
            yield Persistence(backend, saver, store)
        return

    if backend == "postgres":
        if not pg_dsn:
            raise ValueError("postgres backend needs AGENTRT_PG_DSN")
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.store.postgres.aio import AsyncPostgresStore
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("install the postgres extra: pip install 'agent-runtime-gcp[postgres]'") from e

        pool = AsyncConnectionPool(
            pg_dsn, max_size=pool_size, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open(wait=True)
        try:
            saver = AsyncPostgresSaver(pool)
            store = AsyncPostgresStore(pool)
            await saver.setup()
            await store.setup()
            yield Persistence(backend, saver, store)
        finally:
            await pool.close()
        return

    raise ValueError(f"unknown persistence backend: {backend!r}")
