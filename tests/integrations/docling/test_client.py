import asyncio
from io import BytesIO
from ipaddress import ip_address
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from aizk.integrations.docling import (
    ArtifactBytes,
    ArtifactReader,
    DoclingClient,
    DoclingConversionError,
    DoclingOptions,
    DoclingOutput,
    DoclingResponse,
    FileSource,
    UnsafeArtifactError,
    URISource,
    docling_client,
)


async def public_resolver(host: str, port: int):
    """Resolve test hosts without using the machine network."""
    del host, port
    return (ip_address("93.184.216.34"),)


def test_docling_client_sends_bounded_file_and_preserves_both_outputs(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-test")
    requests: list[httpx.Request] = []

    async def convert(request: httpx.Request) -> httpx.Response:
        await request.aread()
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "document": {
                    "md_content": "# Cafe\u0301  \r\n\r\nText\r\n",
                    "json_content": {"schema_name": "DoclingDocument", "texts": []},
                },
                "status": "success",
                "processing_time": 0.25,
                "timings": {"pipeline": 0.2},
                "errors": [],
            },
        )

    converter = httpx.AsyncClient(
        base_url="http://docling.test/",
        transport=httpx.MockTransport(convert),
    )
    result = asyncio.run(
        DoclingClient(http=converter).convert(
            ArtifactBytes(
                content=source.read_bytes(),
                filename="renamed.pdf",
                media_type="application/pdf",
            )
        )
    )

    assert result.markdown == "# Café\n\nText\n"
    assert result.native_json == {"schema_name": "DoclingDocument", "texts": []}
    assert result.details == {
        "status": "success",
        "processing_time": 0.25,
        "timings": {"pipeline": 0.2},
        "errors": [],
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/convert/file"
    assert request.headers["content-type"].startswith("multipart/form-data;")
    assert all(
        value in request.content for value in (b"renamed.pdf", b"json", b"md", b"%PDF-test")
    )


def test_docling_options_include_the_picture_preset_only_when_enabled() -> None:
    disabled = DoclingOptions().form_data()
    enabled = DoclingOptions(
        picture_description=True,
        picture_description_preset="smolvlm",
    ).form_data()

    assert "picture_description_preset" not in disabled
    assert enabled["picture_description_preset"] == "smolvlm"


def test_local_reader_rejects_escape_and_size_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    large = root / "large.pdf"
    large.write_bytes(b"12345")
    reader = ArtifactReader(
        http=httpx.AsyncClient(),
        file_root=root,
        max_bytes=4,
        max_redirects=0,
    )

    with pytest.raises(UnsafeArtifactError, match="outside"):
        asyncio.run(reader.read(FileSource(path=outside)))
    with pytest.raises(UnsafeArtifactError, match="byte limit"):
        asyncio.run(reader.read(FileSource(path=large)))

    exact = root / "exact"
    exact.write_bytes(b"1234")
    monkeypatch.setattr(Path, "open", lambda path, mode: BytesIO(b"12345"))
    with pytest.raises(UnsafeArtifactError, match="grew"):
        asyncio.run(reader.read(FileSource(path=exact)))


def test_local_reader_defaults_unknown_media_type(tmp_path: Path) -> None:
    source = tmp_path / "opaque.unknown-extension"
    source.write_bytes(b"data")
    reader = ArtifactReader(
        http=httpx.AsyncClient(), file_root=tmp_path, max_bytes=4, max_redirects=0
    )

    artifact = asyncio.run(reader.read(FileSource(path=source)))

    assert artifact.filename == source.name
    assert artifact.media_type == "application/octet-stream"


def test_remote_reader_revalidates_redirects_and_bounds_streams() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://files.test/final"})
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf; charset=binary"},
            content=b"pdf",
        )

    reader = ArtifactReader(
        http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        file_root=Path("/unused"),
        max_bytes=3,
        max_redirects=1,
        resolver=public_resolver,
    )
    artifact = asyncio.run(reader.read(URISource(uri="https://files.test/start")))

    assert seen == ["https://files.test/start", "https://files.test/final"]
    assert artifact.filename == "final"
    assert artifact.media_type == "application/pdf"
    assert artifact.content == b"pdf"

    oversized = ArtifactReader(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"four"))
        ),
        file_root=Path("/unused"),
        max_bytes=3,
        max_redirects=0,
        resolver=public_resolver,
    )
    with pytest.raises(UnsafeArtifactError, match="byte limit"):
        asyncio.run(oversized.read(URISource(uri="https://files.test/large")))


@pytest.mark.parametrize("headers", [{}, {"location": "https://files.test/again"}])
def test_remote_reader_rejects_missing_or_excess_redirects(headers: dict[str, str]) -> None:
    reader = ArtifactReader(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(302, headers=headers))
        ),
        file_root=Path("/unused"),
        max_bytes=10,
        max_redirects=0,
        resolver=public_resolver,
    )

    with pytest.raises(UnsafeArtifactError, match="redirect limit"):
        asyncio.run(reader.read(URISource(uri="https://files.test/start")))


def test_remote_reader_bounds_an_undeclared_stream() -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"four"

    reader = ArtifactReader(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        ),
        file_root=Path("/unused"),
        max_bytes=3,
        max_redirects=0,
        resolver=public_resolver,
    )

    with pytest.raises(UnsafeArtifactError, match="byte limit"):
        asyncio.run(reader.read(URISource(uri="https://files.test/stream")))


@pytest.mark.parametrize(
    "location",
    [
        "http://files.test/plain",
        "https://person:secret@files.test/credentialed",
    ],
)
def test_remote_reader_rejects_an_unsafe_redirect_target(location: str) -> None:
    reader = ArtifactReader(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"location": location})
            )
        ),
        file_root=Path("/unused"),
        max_bytes=10,
        max_redirects=1,
        resolver=public_resolver,
    )

    with pytest.raises(UnsafeArtifactError, match="HTTPS URI"):
        asyncio.run(reader.read(URISource(uri="https://files.test/start")))


def test_public_url_validation_rejects_a_missing_host() -> None:
    reader = ArtifactReader(
        http=httpx.AsyncClient(),
        file_root=Path("/unused"),
        max_bytes=10,
        max_redirects=0,
        resolver=public_resolver,
    )

    with pytest.raises(UnsafeArtifactError, match="HTTPS URI"):
        asyncio.run(reader.validate_public_url(httpx.URL("https:/missing-host")))


@pytest.mark.parametrize(
    "uri",
    [
        "http://files.test/file.pdf",
        "https://person:secret@files.test/file.pdf",
    ],
)
def test_remote_source_rejects_unsafe_transport_before_fetch(uri: str) -> None:
    with pytest.raises(ValidationError):
        URISource(uri=uri)


def _with_filename(value: str) -> None:
    FileSource(path=Path("/staging/paper.pdf"), filename=value)


def _with_media_type(value: str) -> None:
    FileSource(path=Path("/staging/paper.pdf"), media_type=value)


@given(
    field=st.sampled_from((_with_filename, _with_media_type)),
    control=st.characters(categories=["Cc"]),
    prefix=st.text(alphabet=st.characters(categories=["Ll"]), max_size=6),
)
def test_source_metadata_cannot_escape_the_multipart_part(
    field, control: str, prefix: str
) -> None:
    # Any control character, including DEL and the C1 range, is rejected from both parts.
    with pytest.raises(ValidationError):
        field(f"{prefix}{control}pdf")
    # The 255-byte cap is the last accepted length; one more byte is rejected.
    with pytest.raises(ValidationError):
        field("x" * 256)
    field("x" * 255)


@pytest.mark.parametrize("name", ["../paper.pdf", "sub/paper.pdf", "sub\\paper.pdf"])
def test_filename_rejects_path_separators(name: str) -> None:
    with pytest.raises(ValidationError):
        FileSource(path=Path("/staging/paper.pdf"), filename=name)


def test_optional_source_metadata_accepts_explicit_null_values() -> None:
    source = FileSource(
        path=Path("/staging/paper.pdf"),
        filename=None,
        media_type=None,
    )

    assert source.filename is None
    assert source.media_type is None


@pytest.mark.parametrize(
    "payload",
    [
        {"document": {}, "status": "failure"},
        {"document": {"md_content": "text"}, "status": "success"},
        {"document": {"json_content": {}}, "status": "success"},
    ],
)
def test_docling_output_rejects_failures_and_missing_requested_formats(
    payload: dict,
) -> None:
    response = DoclingResponse.model_validate(payload)

    with pytest.raises(DoclingConversionError):
        DoclingOutput.from_response(response)


def test_remote_reader_rejects_any_non_public_resolution() -> None:
    async def private_resolver(host: str, port: int):
        del host, port
        return (ip_address("93.184.216.34"), ip_address("127.0.0.1"))

    reader = ArtifactReader(
        http=httpx.AsyncClient(),
        file_root=Path("/unused"),
        max_bytes=1024,
        max_redirects=0,
        resolver=private_resolver,
    )
    with pytest.raises(UnsafeArtifactError, match="public"):
        asyncio.run(reader.read(URISource(uri="https://files.test/paper.pdf")))

    async def empty_resolver(host: str, port: int):
        del host, port
        return ()

    empty = ArtifactReader(
        http=httpx.AsyncClient(),
        file_root=Path("/unused"),
        max_bytes=1024,
        max_redirects=0,
        resolver=empty_resolver,
    )
    with pytest.raises(UnsafeArtifactError, match="public"):
        asyncio.run(empty.read(URISource(uri="https://files.test/paper.pdf")))


@pytest.mark.parametrize("api_key", ["", "secret"])
def test_docling_client_factory_reuses_configured_connection_pools(api_key: str) -> None:
    docling_client.cache_clear()
    client = docling_client(
        "http://docling.test/",
        api_key,
        30.0,
    )
    again = docling_client(
        "http://docling.test/",
        api_key,
        30.0,
    )

    assert client is again
    assert client.http.headers.get("X-Api-Key") == (api_key or None)
    asyncio.run(client.http.aclose())
    docling_client.cache_clear()
