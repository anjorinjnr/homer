"""Singleton PostHog client — no-op when POSTHOG_API_KEY is unset."""

from __future__ import annotations

import atexit
import os
from typing import Any


def _init_client():
    """Return a PostHog client or a no-op stub."""
    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").strip() or "https://us.i.posthog.com"
    if not api_key:
        return _NoOpClient()
    try:
        from posthog import Posthog

        client = Posthog(api_key, host=host)
        atexit.register(client.shutdown)
        return client
    except ImportError:
        return _NoOpClient()


class _NoOpClient:
    """Drop-in stub so callers never need None-checks."""

    def capture(self, *args: Any, **kwargs: Any) -> None:
        pass

    def identify(self, *args: Any, **kwargs: Any) -> None:
        pass

    def group_identify(self, *args: Any, **kwargs: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass


_client: _NoOpClient | None = None


def get_client():
    """Return the module-level PostHog client (lazy-init)."""
    global _client
    if _client is None:
        _client = _init_client()
    return _client
