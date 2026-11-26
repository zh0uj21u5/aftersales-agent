"""Async PostgreSQL I/O for the shared database-only service work units."""

from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import datetime
from typing import Any, TypeVar

from greenlet import getcurrent, greenlet
from psycopg import AsyncConnection, AsyncCursor
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads
from psycopg_pool import AsyncConnectionPool

T = TypeVar("T")


async def _run(work: Callable[[], T]) -> T:
    """Yield driver coroutines to asyncio and deliver exceptions back to the SQL scope."""
    worker = greenlet(work)
    worker.gr_context = getcurrent().gr_context
    pending = worker.switch()
    while not worker.dead:
        try:
            result = await pending
        except BaseException as exc:  # noqa: BLE001 - unwind the worker on cancellation too
            pending = worker.throw(exc)
        else:
            pending = worker.switch(result)
    return pending


def _wait(operation: Awaitable[T]) -> T:
    parent = getcurrent().parent
    if parent is None:
        if isinstance(operation, Coroutine):
            operation.close()
        raise RuntimeError("PostgreSQL access requires an async database work unit")
    return parent.switch(operation)


class Row(dict[str, Any]):
    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor: AsyncCursor[dict[str, Any]]) -> None:
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    @staticmethod
    def _row(values: dict[str, Any]) -> Row:
        # Match the SQLite JSON/timestamp projection at the existing service boundary.
        return Row(
            (key, value.isoformat() if isinstance(value, datetime) else value)
            for key, value in values.items()
        )

    def fetchone(self) -> Row | None:
        values = _wait(self.cursor.fetchone())
        return self._row(values) if values is not None else None

    def fetchall(self) -> list[Row]:
        return [self._row(values) for values in _wait(self.cursor.fetchall())]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


class PgConnection:
    dialect = "postgresql"

    def __init__(self, connection: AsyncConnection[dict[str, Any]]) -> None:
        self.connection = connection

    @property
    def in_transaction(self) -> bool:
        return self.connection.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        # These are static repository queries with qmark bindings, never user SQL.
        return Cursor(_wait(self.connection.execute(sql.replace("?", "%s"), params or None)))

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> None:
        _wait(self.connection.cursor().executemany(sql.replace("?", "%s"), params))

    @contextmanager
    def pipeline(self) -> Iterator[None]:
        """Batch writes whose cursors are unused."""
        with self._scope(self.connection.pipeline()):
            yield

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._scope(self.connection.transaction()):
            yield

    @staticmethod
    @contextmanager
    def _scope(scope: AbstractAsyncContextManager[Any]) -> Iterator[None]:
        _wait(scope.__aenter__())
        try:
            yield
        except BaseException as exc:
            _wait(scope.__aexit__(type(exc), exc, exc.__traceback__))
            raise
        else:
            _wait(scope.__aexit__(None, None, None))


class PostgresStorage:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 20) -> None:
        self.pool = AsyncConnectionPool(
            url,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=self._configure,
            min_size=min_size,
            max_size=max_size,
            open=False,
        )

    @staticmethod
    async def _configure(connection: AsyncConnection[dict[str, Any]]) -> None:
        set_json_loads(lambda value: bytes(value).decode("utf-8"), connection)
        await connection.execute("SET TIME ZONE 'Asia/Shanghai'")

    async def run(self, work: Callable[[PgConnection], T]) -> T:
        async with self.pool.connection() as connection:
            return await _run(lambda: work(PgConnection(connection)))
