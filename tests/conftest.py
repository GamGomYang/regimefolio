"""All automated tests run offline; localhost fake HTTP servers are allowed."""
import socket
import pytest


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise AssertionError('External network forbidden in automated tests')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
