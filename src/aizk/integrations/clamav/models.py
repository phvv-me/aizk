from typing import Literal

from patos import FrozenModel


class CleanScan(FrozenModel):
    """A completed ClamAV scan that found no known malware."""

    clean: Literal[True] = True
    bytes_scanned: int


class MalwareRejectedError(RuntimeError):
    """The artifact violates the malware policy or carries a detected signature."""

    def __init__(self, reason: str, signature: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.signature = signature


class MalwareUnavailableError(ConnectionError):
    """ClamAV did not provide an authoritative clean or infected result."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
