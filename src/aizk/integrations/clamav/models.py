from typing import Literal, Protocol, runtime_checkable

from patos import FrozenModel


class CleanScan(FrozenModel):
    """A completed ClamAV scan that found no known malware."""

    clean: Literal[True] = True
    bytes_scanned: int


@runtime_checkable
class ContentScanner(Protocol):
    """The one scan call a consumer of the malware boundary makes.

    Typed as the used surface so a recording double validates in place of the real
    `ClamAVClient` without weakening field validation.
    """

    async def scan(self, content: bytes) -> CleanScan: ...


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
