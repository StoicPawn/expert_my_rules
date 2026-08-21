from __future__ import annotations

from abc import ABC, abstractmethod


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, system: str, user: str) -> str:
        raise NotImplementedError
