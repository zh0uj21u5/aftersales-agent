"""Keep unmarked tests offline even when credentials exist on the machine."""

import socket
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect, initialize
from aftersales.domain.seed import seed_data
from aftersales.domain.services import Services


@pytest.fixture
def shop() -> Iterator[Services]:
    db = connect()
    initialize(db)
    seed_data(db)
    yield Services(db, FixedClock())
    db.close()


@pytest.fixture(autouse=True)
def block_unit_test_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("llm") is None:
        original_connect = socket.socket.connect
        pair_code = getattr(socket.socketpair, "__code__", None)

        def blocked(connection: socket.socket, *args: Any, **kwargs: Any) -> None:
            # Windows implements asyncio's self-pipe with stdlib socketpair over loopback.
            # Exempt only that exact stdlib caller; ordinary loopback/network stays blocked.
            if pair_code is not None and sys._getframe(1).f_code is pair_code:
                return original_connect(connection, *args, **kwargs)
            raise AssertionError("Unit tests must not access the network")

        monkeypatch.setattr(socket.socket, "connect", blocked)
        monkeypatch.setattr(socket.socket, "connect_ex", blocked)
