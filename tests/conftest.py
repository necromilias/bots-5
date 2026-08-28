from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("ordinary unit tests must not use live network sockets")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
