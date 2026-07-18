import asyncio
from types import TracebackType
from typing import Self

import pytest

from aizk.integrations.clamav import (
    ClamAVClient,
    MalwareRejectedError,
    MalwareUnavailableError,
)


class FakeClamAV:
    """Small real TCP peer that records the complete ClamAV `INSTREAM` framing."""

    def __init__(self, reply: bytes | None, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.command = b""
        self.chunks: list[bytes] = []
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None and self.server.sockets
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self.command = await reader.readuntil(b"\n")
            while True:
                size = int.from_bytes(await reader.readexactly(4))
                if size == 0:
                    break
                self.chunks.append(await reader.readexactly(size))
            await asyncio.sleep(self.delay)
            if self.reply is not None:
                writer.write(self.reply)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def test_clean_scan_uses_the_library_command_and_chunked_frames() -> None:
    content = b"a" * 1024 + b"end"

    async def exercise():
        async with FakeClamAV(b"stream: OK\n") as daemon:
            result = await ClamAVClient(
                host="127.0.0.1", port=daemon.port, timeout=1.0, max_bytes=len(content)
            ).scan(content)
            return result, daemon.command, daemon.chunks

    result, command, chunks = asyncio.run(exercise())

    assert result.clean
    assert result.bytes_scanned == len(content)
    assert command == b"nINSTREAM\n"
    assert chunks == [b"a" * 1024, b"end"]


def test_empty_clean_scan_sends_only_the_zero_length_chunk() -> None:
    async def exercise():
        async with FakeClamAV(b"stream: OK\n") as daemon:
            result = await ClamAVClient(
                host="127.0.0.1", port=daemon.port, timeout=1.0, max_bytes=1
            ).scan(b"")
            return result, daemon.chunks

    result, chunks = asyncio.run(exercise())

    assert result.bytes_scanned == 0
    assert chunks == []


@pytest.mark.parametrize(
    ("reply", "signature", "message"),
    [
        (b"stream: Eicar-Signature FOUND\n", "Eicar-Signature", "detected"),
        (b"stream: FOUND\n", "unknown", "detected"),
        (b"INSTREAM size limit exceeded. ERROR\n", None, "too large"),
    ],
)
def test_malware_and_policy_rejections_never_reach_the_clean_path(
    reply: bytes,
    signature: str | None,
    message: str,
) -> None:
    async def exercise() -> None:
        async with FakeClamAV(reply) as daemon:
            with pytest.raises(MalwareRejectedError, match=message) as rejected:
                await ClamAVClient(
                    host="127.0.0.1", port=daemon.port, timeout=1.0, max_bytes=10
                ).scan(b"sample")
            assert rejected.value.signature == signature

    asyncio.run(exercise())


def test_client_rejects_oversized_content_before_connecting() -> None:
    with pytest.raises(MalwareRejectedError, match="byte limit") as rejected:
        asyncio.run(ClamAVClient(host="127.0.0.1", port=9, timeout=1.0, max_bytes=3).scan(b"four"))

    assert rejected.value.reason == "artifact exceeds the malware scan byte limit"
    assert rejected.value.signature is None


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (b"stream: scanner crashed ERROR\n", "scanner crashed"),
        (b"nonsense\n", "unexpected ClamAV reply"),
        (b"stream:  FOUND\n", "unexpected ClamAV reply"),
    ],
)
def test_daemon_errors_and_unknown_replies_fail_closed(reply: bytes, message: str) -> None:
    async def exercise() -> None:
        async with FakeClamAV(reply) as daemon:
            with pytest.raises(MalwareUnavailableError, match=message) as unavailable:
                await ClamAVClient(
                    host="127.0.0.1", port=daemon.port, timeout=1.0, max_bytes=10
                ).scan(b"sample")
            assert unavailable.value.reason

    asyncio.run(exercise())


def test_closed_stream_without_a_verdict_fails_closed() -> None:
    async def exercise() -> None:
        async with FakeClamAV(None) as daemon:
            with pytest.raises(MalwareUnavailableError, match="without a verdict"):
                await ClamAVClient(
                    host="127.0.0.1", port=daemon.port, timeout=1.0, max_bytes=10
                ).scan(b"sample")

    asyncio.run(exercise())


def test_timeout_fails_closed() -> None:
    async def exercise() -> None:
        async with FakeClamAV(b"stream: OK\n", delay=0.2) as daemon:
            with pytest.raises(MalwareUnavailableError, match="timed out"):
                await ClamAVClient(
                    host="127.0.0.1", port=daemon.port, timeout=0.02, max_bytes=10
                ).scan(b"sample")

    asyncio.run(exercise())


def test_refused_connection_fails_closed() -> None:
    async def exercise() -> None:
        server = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
        assert server.sockets
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        with pytest.raises(MalwareUnavailableError, match="unavailable"):
            await ClamAVClient(host="127.0.0.1", port=port, timeout=1.0, max_bytes=10).scan(
                b"sample"
            )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        {"host": "", "port": 3310, "timeout": 1.0, "max_bytes": 1},
        {"host": "localhost", "port": 0, "timeout": 1.0, "max_bytes": 1},
        {"host": "localhost", "port": 3310, "timeout": 0.0, "max_bytes": 1},
        {"host": "localhost", "port": 3310, "timeout": 1.0, "max_bytes": 0},
    ],
)
def test_constructor_rejects_unbounded_or_invalid_configuration(
    arguments: dict[str, str | int | float],
) -> None:
    with pytest.raises(ValueError):
        ClamAVClient.model_validate(arguments)
