from compression import zstd
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import dbutil
import pytest
from obstore.store import MemoryStore, S3Store
from patos import sql

if TYPE_CHECKING:
    from obstore import GetOptions

from aizk.config import Settings
from aizk.storage import (
    ByteLimitExceeded,
    ByteStore,
    DownloadUnavailable,
    IntegrityMismatch,
    StoredBytes,
    s3_backend,
)
from aizk.store import Blob


def memory_store(limit: int = 1024) -> ByteStore:
    return ByteStore(
        backend=MemoryStore(),
        upload_byte_limit=limit,
        internal_download_lifetime=timedelta(minutes=5),
    )


def test_settings_define_the_bounded_object_store_contract() -> None:
    configured = Settings(
        object_store_endpoint="https://objects.test",
        object_store_bucket="bucket",
        object_store_access_key="access",
        object_store_secret_key="secret",
        object_store_upload_byte_limit=17,
        object_store_internal_download_lifetime_seconds=23,
    )

    assert str(configured.object_store_endpoint) == "https://objects.test/"
    assert configured.object_store_bucket == "bucket"
    assert configured.object_store_access_key.get_secret_value() == "access"
    assert configured.object_store_secret_key.get_secret_value() == "secret"
    assert configured.object_store_upload_byte_limit == 17
    assert configured.object_store_compression_level == 3
    assert configured.object_store_compression_min_savings == 0.05
    assert configured.object_store_internal_download_lifetime_seconds == 23


def test_memory_store_round_trips_immutable_randomly_keyed_bytes() -> None:
    async def body() -> None:
        store = memory_store()
        payload = b"same content"
        first = await store.put(payload)
        second = await store.put(payload)

        assert first == StoredBytes(
            key=first.key,
            content_hash=sql.uuid8(payload),
            size=len(payload),
            stored_size=len(payload),
            encoding=Blob.Encoding.identity,
            etag="0",
        )
        assert first.key.startswith("objects/")
        assert first.key != second.key
        assert sql.hex(payload) not in first.key
        assert await store.get(first.key, expected_hash=first.content_hash) == payload
        assert await store.get(second.key) == payload

    dbutil.run(body())


def test_size_limit_applies_before_upload_and_before_download_materialization() -> None:
    async def body() -> None:
        backend = MemoryStore()
        store = ByteStore(
            backend=backend,
            upload_byte_limit=3,
            internal_download_lifetime=timedelta(seconds=1),
        )
        with pytest.raises(ByteLimitExceeded, match="4 bytes, limit is 3"):
            await store.put(b"four")

        await backend.put_async("foreign", b"four", mode="create")
        with pytest.raises(ByteLimitExceeded, match="4 bytes, limit is 3"):
            await store.get("foreign")

    dbutil.run(body())


def test_decompression_is_bounded_before_the_full_output_is_materialized() -> None:
    async def body() -> None:
        backend = MemoryStore()
        store = ByteStore(
            backend=backend,
            upload_byte_limit=32,
            internal_download_lifetime=timedelta(seconds=1),
        )
        compressed = zstd.compress(b"x" * 4096)
        assert len(compressed) <= store.upload_byte_limit
        await backend.put_async("compressed", compressed, mode="create")

        with pytest.raises(ByteLimitExceeded, match="decompression limit"):
            await store.get("compressed", encoding=Blob.Encoding.zstd)

    dbutil.run(body())


def test_get_pins_the_recorded_object_version() -> None:
    class RecordingStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            object.__setattr__(self, "seen", [])

        async def get_async(self, key: str, *, options: GetOptions | None = None):  # type: ignore[override]
            cast(list["GetOptions | None"], self.seen).append(options)
            return await super().get_async(key, options=options)

    async def body() -> None:
        backend = RecordingStore()
        store = ByteStore(
            backend=backend,
            upload_byte_limit=1024,
            internal_download_lifetime=timedelta(minutes=5),
        )
        stored = await store.put(b"immutable")

        assert await store.get(stored.key, version="rev-7") == b"immutable"
        assert await store.get(stored.key) == b"immutable"
        assert cast(list[dict[str, str] | None], backend.seen) == [{"version": "rev-7"}, None]

    dbutil.run(body())


def test_get_rejects_bytes_that_do_not_match_postgres() -> None:
    async def body() -> None:
        store = memory_store()
        stored = await store.put(b"trusted")
        with pytest.raises(IntegrityMismatch, match="does not match"):
            await store.get(stored.key, expected_hash=sql.uuid8(b"different"))
        with pytest.raises(IntegrityMismatch, match="stored size"):
            await store.get(stored.key, expected_size=stored.size + 1)

    dbutil.run(body())


def test_compressible_original_is_transparently_restored_and_bounded() -> None:
    async def body() -> None:
        store = memory_store()
        payload = b"repeat " * 100
        stored = await store.put(payload)

        assert stored.encoding is Blob.Encoding.zstd
        assert stored.stored_size < stored.size
        assert (
            await store.get(
                stored.key,
                encoding=stored.encoding,
                expected_size=stored.size,
                expected_hash=stored.content_hash,
            )
            == payload
        )
        with pytest.raises(ValueError, match="unsupported object encoding"):
            store.decode(payload, cast(Blob.Encoding, "brotli"))

    dbutil.run(body())


def test_memory_store_cannot_mint_an_external_download_capability() -> None:
    async def body() -> None:
        store = memory_store()
        with pytest.raises(DownloadUnavailable, match="cannot sign"):
            await store.download_url("objects/private")

    dbutil.run(body())


def test_delete_is_idempotent_compensation_for_failed_intake() -> None:
    async def body() -> None:
        backend = MemoryStore()
        store = ByteStore(
            backend=backend,
            upload_byte_limit=1024,
            internal_download_lifetime=timedelta(minutes=5),
        )
        stored = await store.put(b"orphan")

        await store.delete(stored.key)
        with pytest.raises(FileNotFoundError):
            await backend.get_async(stored.key)
        await store.delete(stored.key)

    dbutil.run(body())


@pytest.mark.parametrize(
    ("endpoint", "allow_http"),
    [
        ("http://objects.internal:9000", "true"),
        ("https://objects.internal", "false"),
    ],
)
def test_s3_factory_uses_configured_credentials_and_short_lived_signing(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    allow_http: str,
) -> None:
    calls: list[tuple[S3Store, str, str, timedelta]] = []

    async def sign(
        store: S3Store,
        method: str,
        key: str,
        lifetime: timedelta,
    ) -> str:
        calls.append((store, method, key, lifetime))
        return "https://objects.test/signed"

    monkeypatch.setattr("aizk.storage.sign_async", sign)
    lifetime = timedelta(seconds=29)
    signer = s3_backend(
        endpoint=endpoint,
        bucket="aizk",
        access_key="access",
        secret_key="secret",
    )
    store = ByteStore(
        backend=signer,
        signer=signer,
        upload_byte_limit=10,
        internal_download_lifetime=lifetime,
    )

    async def body() -> None:
        assert await store.download_url("objects/key") == "https://objects.test/signed"

    dbutil.run(body())
    backend, method, key, signed_lifetime = calls.pop()
    assert isinstance(backend, S3Store)
    assert backend.config["endpoint"] == endpoint
    assert backend.config["access_key_id"] == "access"
    assert backend.config["secret_access_key"] == "secret"
    assert backend.config["checksum_algorithm"] == "SHA256"
    assert backend.client_options == {"allow_http": allow_http}
    assert (method, key, signed_lifetime) == ("GET", "objects/key", lifetime)
    assert calls == []
