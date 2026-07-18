import asyncio
import mimetypes
import socket
from collections.abc import Awaitable, Callable, Sequence
from functools import cache
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path, PurePosixPath

import httpx
from patos import FrozenFlexModel
from pydantic import Field

from .models import (
    ArtifactBytes,
    ArtifactSource,
    DoclingOptions,
    DoclingResponse,
    FileSource,
    URISource,
)

type IPAddress = IPv4Address | IPv6Address
type HostResolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

_REDIRECTS = frozenset({301, 302, 303, 307, 308})


async def resolve_host(host: str, port: int) -> tuple[IPAddress, ...]:
    """Resolve every address for one URI host without blocking the event loop."""

    def resolve() -> tuple[IPAddress, ...]:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(ip_address(record[4][0]) for record in records))

    return await asyncio.to_thread(resolve)


class UnsafeArtifactError(ValueError):
    """An artifact source violates the local path, public network, or size boundary."""


class ArtifactReader(FrozenFlexModel):
    """Read bounded local files or public HTTPS resources before conversion.

    Network deployments must also enforce an egress policy around the AIZK process. DNS
    validation here blocks ordinary SSRF and every redirect is revalidated, while the network
    boundary closes the remaining DNS rebinding race.
    """

    http: httpx.AsyncClient
    file_root: Path
    max_bytes: int
    max_redirects: int
    resolver: HostResolver = resolve_host

    async def read(self, source: ArtifactSource) -> ArtifactBytes:
        """Load one source after enforcing its path or public-network boundary."""
        if isinstance(source, FileSource):
            return await self.read_file(source)
        return await self.read_uri(source)

    async def read_file(self, source: FileSource) -> ArtifactBytes:
        """Read one regular file whose resolved path remains inside the staging root."""
        root = self.file_root.resolve(strict=True)
        path = source.path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise UnsafeArtifactError("local artifact is outside the staging root")
        if path.stat().st_size > self.max_bytes:
            raise UnsafeArtifactError("artifact exceeds the configured byte limit")

        def read() -> bytes:
            with path.open("rb") as stream:
                content = stream.read(self.max_bytes + 1)
            return content

        content = await asyncio.to_thread(read)
        if len(content) > self.max_bytes:
            raise UnsafeArtifactError("artifact grew beyond the configured byte limit")
        filename = source.filename or path.name
        media_type = source.media_type or mimetypes.guess_type(filename)[0]
        return ArtifactBytes(
            content=content,
            filename=filename,
            media_type=media_type or "application/octet-stream",
        )

    async def read_uri(self, source: URISource) -> ArtifactBytes:
        """Fetch one public HTTPS URI with bounded redirects, DNS, time, and response bytes."""
        url = httpx.URL(str(source.uri))
        redirects = 0
        while True:
            await self.validate_public_url(url)
            async with self.http.stream("GET", url, follow_redirects=False) as response:
                if response.status_code not in _REDIRECTS:
                    return await self.collect(source, url, response)
            location = response.headers.get("location")
            if location is None or redirects == self.max_redirects:
                raise UnsafeArtifactError("remote artifact exceeded the redirect limit")
            url = url.join(location)
            redirects += 1

    async def collect(
        self,
        source: URISource,
        url: httpx.URL,
        response: httpx.Response,
    ) -> ArtifactBytes:
        """Stream one accepted response into bounded bytes with its name and media type."""
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared is not None and int(declared) > self.max_bytes:
            raise UnsafeArtifactError("artifact exceeds the configured byte limit")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > self.max_bytes:
                raise UnsafeArtifactError("artifact exceeds the configured byte limit")
        filename = source.filename or PurePosixPath(url.path).name or "artifact"
        media_type = source.media_type or response.headers.get("content-type", "")
        return ArtifactBytes(
            content=bytes(content),
            filename=filename,
            media_type=media_type.partition(";")[0] or "application/octet-stream",
        )

    async def validate_public_url(self, url: httpx.URL) -> None:
        """Reject non-HTTPS, credentialed, unresolved, private, and special-purpose targets."""
        if url.scheme != "https" or url.userinfo or not url.host:
            raise UnsafeArtifactError("remote artifact must be an uncredentialed HTTPS URI")
        addresses = await self.resolver(url.host, url.port or 443)
        if not addresses or any(not address.is_global for address in addresses):
            raise UnsafeArtifactError("remote artifact host must resolve only to public addresses")


class DoclingClient(FrozenFlexModel):
    """Convert already accepted bytes without loading Docling in AIZK."""

    http: httpx.AsyncClient
    options: DoclingOptions = Field(default_factory=DoclingOptions)

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Request native Docling JSON and Markdown for one scanned immutable original."""
        response = await self.http.post(
            "v1/convert/file",
            data=self.options.form_data(),
            files={
                "files": (
                    artifact.filename,
                    artifact.content,
                    artifact.media_type,
                )
            },
        )
        response.raise_for_status()
        return DoclingResponse.model_validate(response.json())


@cache
def docling_client(
    url: str,
    api_key: str,
    timeout: float,
    options: DoclingOptions | None = None,
) -> DoclingClient:
    """Reuse the internal converter connection pool for one configuration."""
    headers = {"X-Api-Key": api_key} if api_key else {}
    return DoclingClient(
        http=httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/",
            headers=headers,
            timeout=timeout,
        ),
        options=options or DoclingOptions(),
    )
