import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Annotated, ClassVar

from clamav_client.clamd import (
    BufferTooLongError,
    ClamdNetworkSocket,
    CommunicationError,
    ResponseError,
    ScanResults,
)
from patos import FrozenModel
from pydantic import Field, StringConstraints

from .models import CleanScan, MalwareRejectedError, MalwareUnavailableError


class ClamAVClient(FrozenModel):
    """Fail-closed asynchronous ClamAV `INSTREAM` client over an internal TCP socket.

    The maintained `clamav-client` package owns the wire protocol; this wrapper adds the
    byte bound, a total deadline, and the fail-closed error taxonomy.
    `MalwareRejectedError` is reserved for the byte limits and a successfully parsed
    `FOUND` verdict; a connection failure or an unparsable reply is never an
    authoritative verdict and maps to `MalwareUnavailableError`. ClamAV TCP is
    neither authenticated nor encrypted. Deploy this client and `clamd` on a private
    container network, and never expose the daemon port outside that boundary.

    Cancelling the deadline abandons the blocking scan rather than interrupting it, so
    scans run on a small dedicated pool: every socket operation carries `timeout`, a
    scan cancelled before it starts never runs, and a stranded slow peer can only pin
    one of these workers instead of growing the process-wide executor.
    """

    scanners: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="clamav"
    )

    host: Annotated[str, StringConstraints(min_length=1)]
    port: Annotated[int, Field(gt=0, lt=65_536)]
    timeout: Annotated[float, Field(gt=0)]
    max_bytes: Annotated[int, Field(gt=0)]

    async def scan(self, content: bytes) -> CleanScan:
        """Return only an authoritative clean result and reject every other outcome."""
        if len(content) > self.max_bytes:
            raise MalwareRejectedError("artifact exceeds the malware scan byte limit")
        daemon = ClamdNetworkSocket(self.host, self.port, self.timeout)
        stream = BytesIO(content)
        try:
            async with asyncio.timeout(self.timeout):
                results = await asyncio.get_running_loop().run_in_executor(
                    self.scanners, daemon.instream, stream
                )
        except TimeoutError as error:
            raise MalwareUnavailableError("ClamAV scan timed out") from error
        except BufferTooLongError as error:
            raise MalwareRejectedError(
                "ClamAV rejected the artifact because it is too large"
            ) from error
        except ResponseError as error:
            raise MalwareUnavailableError(f"unexpected ClamAV reply {error}") from error
        except CommunicationError as error:
            raise MalwareUnavailableError(f"ClamAV is unavailable: {error}") from error
        return self.verdict(results, len(content))

    @staticmethod
    def verdict(results: ScanResults, bytes_scanned: int) -> CleanScan:
        """Map the parsed ClamAV result grammar into clean, rejected, or unavailable."""
        status, signature = results.get(
            "stream", ("ERROR", "ClamAV closed the stream without a verdict")
        )
        if status == "OK":
            return CleanScan(bytes_scanned=bytes_scanned)
        if status == "FOUND":
            signature = signature or "unknown"
            raise MalwareRejectedError(f"ClamAV detected {signature}", signature=signature)
        raise MalwareUnavailableError(signature or "ClamAV reported a scan error")
