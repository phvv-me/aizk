import asyncio
import secrets
from compression import zstd
from datetime import timedelta
from typing import TYPE_CHECKING

from obstore import sign_async
from obstore.store import ObjectStoreMethods, S3Store
from patos import FrozenFlexModel, FrozenModel, sql
from pydantic import UUID7, UUID8

from .store.models.tables.blob import Blob

if TYPE_CHECKING:
    from obstore import GetOptions


class ByteLimitExceeded(ValueError):
    """The object is larger than this process is allowed to materialize."""


class IntegrityMismatch(ValueError):
    """Downloaded bytes do not match the checksum stored in PostgreSQL."""


class DownloadUnavailable(RuntimeError):
    """The injected backend cannot issue a short-lived download URL."""


class StoredBytes(FrozenModel):
    """Metadata needed to create one immutable PostgreSQL `Blob` row."""

    key: str
    content_hash: UUID8
    size: int
    stored_size: int
    encoding: Blob.Encoding
    etag: str | None = None
    version: str | None = None


class StoredObject(FrozenModel):
    """Materialize one PostgreSQL Blob reference for periodic integrity verification."""

    id: UUID7
    key: str
    content_hash: UUID8
    size: int
    encoding: Blob.Encoding
    version: str | None = None


class IntegrityCheck(FrozenModel):
    """Record one completed object verification without exposing its storage key."""

    id: UUID7
    error: str | None = None


def s3_backend(endpoint: str, bucket: str, access_key: str, secret_key: str) -> S3Store:
    """Build an S3-compatible backend with path-style URLs and SHA-256 upload checks."""
    return S3Store(
        bucket,
        endpoint=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        checksum_algorithm="SHA256",
        client_options={"allow_http": endpoint.startswith("http://")},
    )


class ByteStore(FrozenFlexModel):
    """Asynchronous immutable bytes over one obstore backend.

    Keys contain 256 bits of fresh randomness and never contain a checksum, file
    name, scope, or media type. Callers authorize the corresponding PostgreSQL
    `Blob` before reading or signing its key because object storage has no tenant
    context of its own.
    """

    backend: ObjectStoreMethods
    upload_byte_limit: int
    internal_download_lifetime: timedelta
    compression_level: int = 3
    compression_min_savings: float = 0.05
    signer: S3Store | None = None

    async def put(self, data: bytes) -> StoredBytes:
        """Store a bounded original with adaptive lossless Zstandard encoding."""
        self._check_size(len(data))
        encoded, encoding = await asyncio.to_thread(self.encode, data)
        key = f"objects/{secrets.token_urlsafe(32)}"
        result = await self.backend.put_async(key, encoded, mode="create")
        return StoredBytes(
            key=key,
            content_hash=sql.uuid8(data),
            size=len(data),
            stored_size=len(encoded),
            encoding=encoding,
            etag=result["e_tag"],
            version=result["version"],
        )

    async def get(
        self,
        key: str,
        *,
        encoding: Blob.Encoding = Blob.Encoding.identity,
        expected_size: int | None = None,
        expected_hash: UUID8 | None = None,
        version: str | None = None,
    ) -> bytes:
        """Read, decode, bound, and verify one already-authorized original.

        version: the recorded object-store revision, so a versioned backend serves the
        exact immutable bytes rather than whatever currently sits at the key.
        """
        if expected_size is not None:
            self._check_size(expected_size)
        options: GetOptions | None = {"version": version} if version is not None else None
        result = await self.backend.get_async(key, options=options)
        self._check_size(result.meta["size"])
        stored = bytes(await result.bytes_async())
        data = await asyncio.to_thread(self.decode, stored, encoding)
        self._check_size(len(data))
        if expected_size is not None and len(data) != expected_size:
            raise IntegrityMismatch(f"object {key!r} does not match its stored size")
        if expected_hash is not None and sql.uuid8(data) != expected_hash:
            raise IntegrityMismatch(f"object {key!r} does not match its stored content hash")
        return data

    def encode(self, data: bytes) -> tuple[bytes, Blob.Encoding]:
        """Use Zstandard only when it saves the configured fraction of stored bytes."""
        compressed = zstd.compress(data, level=self.compression_level)
        if len(compressed) <= len(data) * (1.0 - self.compression_min_savings):
            return compressed, Blob.Encoding.zstd
        return data, Blob.Encoding.identity

    def decode(self, data: bytes, encoding: Blob.Encoding) -> bytes:
        """Restore exact bytes while bounding decompression before allocation can run away."""
        match encoding:
            case Blob.Encoding.identity:
                return data
            case Blob.Encoding.zstd:
                decompressor = zstd.ZstdDecompressor()
                restored = decompressor.decompress(data, self.upload_byte_limit + 1)
                if len(restored) > self.upload_byte_limit or not decompressor.eof:
                    raise ByteLimitExceeded(
                        f"object exceeds the {self.upload_byte_limit} byte decompression limit"
                    )
                return restored
        raise ValueError(f"unsupported object encoding {encoding!r}")

    async def download_url(self, key: str) -> str:
        """Sign one already-authorized key for the configured internal lifetime."""
        if self.signer is None:
            raise DownloadUnavailable("the injected byte store cannot sign download URLs")
        return await sign_async(
            self.signer,
            "GET",
            key,
            self.internal_download_lifetime,
        )

    async def delete(self, key: str) -> None:
        """Idempotently remove one orphan as compensation for a failed intake transaction."""
        await self.backend.delete_async(key)

    def _check_size(self, size: int) -> None:
        """Reject a byte count above the configured in-process materialization limit."""
        if size > self.upload_byte_limit:
            raise ByteLimitExceeded(
                f"object has {size} bytes, limit is {self.upload_byte_limit} bytes"
            )


__all__ = [
    "ByteLimitExceeded",
    "ByteStore",
    "DownloadUnavailable",
    "IntegrityMismatch",
    "IntegrityCheck",
    "StoredBytes",
    "StoredObject",
    "s3_backend",
]
