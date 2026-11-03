"""Keep unmarked tests offline even when credentials exist on the machine."""

import socket
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

        def blocked(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("Unit tests must not access the network")

        monkeypatch.setattr(socket.socket, "connect", blocked)
        monkeypatch.setattr(socket.socket, "connect_ex", blocked)
