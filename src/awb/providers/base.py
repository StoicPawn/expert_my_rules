from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


ProgressCallback = Callable[[dict[str, Any]], None]


class ModelProvider(ABC):
    """Model provider contract.

    Providers may emit coarse liveness/progress metadata through a callback. The
    callback must never contain hidden chain-of-thought or generated token text; it
    is intended only for operational observability such as elapsed time, chunks and
    health state. Providers that do not support progress remain fully compatible.
    """

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        callback = getattr(self, '_progress_callback', None)
        if callback is not None:
            callback(dict(payload))

    @abstractmethod
    def generate(self, system: str, user: str) -> str:
        raise NotImplementedError
