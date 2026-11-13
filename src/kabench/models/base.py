"""ASR model interface."""

from abc import ABC, abstractmethod
from pathlib import Path


class ASRModel(ABC):
    name: str

    @abstractmethod
    def transcribe(self, paths: list[Path]) -> list[str]:
        """Transcribe audio files, returning one string per path."""
