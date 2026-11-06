"""Offline tests must never use external network, including accidentally unpatched models."""
import socket
import pytest


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.node.get_closest_marker('integration'):
        return
    from backend.app import app
    monkeypatch.setattr(app.state, 'interactive_in_process', True, raising=False)
    # Inherited by subprocesses as a second barrier; fake ChatLike objects remain usable.
    monkeypatch.setenv('MAQA_DISABLE_MODEL_CALLS', '1')
    connect, resolve = socket.socket.connect, socket.getaddrinfo

    def local(host):
        return host in ('localhost', '127.0.0.1', '::1', None, '')

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            raise AssertionError('Offline test attempted an external connection')
        return connect(sock, address)

    def guarded_resolve(host, *args, **kwargs):
        if not local(host):
            raise AssertionError('Offline test attempted external DNS resolution')
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded_resolve)
