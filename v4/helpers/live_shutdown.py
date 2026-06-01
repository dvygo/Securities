"""Cooperative shutdown for Databento ``db.Live()`` (SIGINT / SIGTERM)."""

from __future__ import annotations

import signal
import sys
import threading
from typing import Any

_lock = threading.Lock()
_clients: list[Any] = []
_handlers_installed = False


def register_live_client(client: Any) -> None:
    with _lock:
        _clients.append(client)
    _install_handlers()


def unregister_live_client(client: Any) -> None:
    with _lock:
        try:
            _clients.remove(client)
        except ValueError:
            pass


def shutdown_all_live_clients() -> None:
    with _lock:
        clients = list(_clients)
    for client in clients:
        for meth in ("stop", "terminate"):
            try:
                getattr(client, meth)()
            except Exception:
                pass


def _on_signal(signum: int, frame: Any) -> None:
    print("\ninterrupt: stopping Live session...", file=sys.stderr, flush=True)
    shutdown_all_live_clients()
    raise KeyboardInterrupt


def _install_handlers() -> None:
    global _handlers_installed
    if _handlers_installed:
        return
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _on_signal)
        except (OSError, ValueError):
            pass
    _handlers_installed = True
