import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.artifacts.formats import FormatPolicy, UnsupportedFormat

policy = FormatPolicy()

READABLE = (
    ("application/pdf", b"%PDF-1.7\nbody"),
    ("image/png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
    ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
    ("image/gif", b"GIF89a\x01\x00"),
    ("image/tiff", b"II*\x00\x08\x00\x00\x00"),
    ("image/tiff", b"MM\x00*\x00\x00\x00\x08"),
    ("image/bmp", b"BM\x36\x00\x00\x00"),
    ("image/webp", b"RIFF\x24\x00\x00\x00WEBPVP8 "),
    ("audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
    ("application/epub+zip", b"PK\x03\x04\x14\x00"),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04\x14\x00",
    ),
    ("text/markdown", b"# a note\n\nwith prose\n"),
    ("text/html", b"<!doctype html><title>page</title>"),
    ("text/plain", b"just words"),
    ("application/json", b'{"key": "value"}'),
    ("text/csv", b"a,b\n1,2\n"),
)


@pytest.mark.parametrize(("media_type", "content"), READABLE)
def test_every_readable_format_is_accepted_under_its_own_signature(
    media_type: str, content: bytes
) -> None:
    assert policy.accept(media_type, content) == media_type


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("APPLICATION/PDF", "application/pdf"),
        ("text/markdown; charset=utf-8", "text/markdown"),
        ("  image/jpg ", "image/jpeg"),
        ("text/x-markdown", "text/markdown"),
        ("application/x-pdf", "application/pdf"),
        ("image/x-ms-bmp", "image/bmp"),
    ],
)
def test_declared_spellings_fold_onto_one_stored_media_type(declared: str, expected: str) -> None:
    assert policy.normalize(declared) == expected
    assert policy.accept_declaration(declared) == expected


@pytest.mark.parametrize(
    "declared",
    [
        "application/octet-stream",
        "application/x-msdownload",
        "video/mp4",
        "font/woff2",
        "",
    ],
)
def test_a_format_aizk_cannot_read_never_reaches_the_object_store(declared: str) -> None:
    with pytest.raises(UnsupportedFormat, match="cannot read"):
        policy.accept(declared, b"%PDF-1.7 whatever")


def test_bytes_that_contradict_the_declaration_are_refused_rather_than_corrected() -> None:
    with pytest.raises(UnsupportedFormat, match="look like plain text, not the declared"):
        policy.accept("application/pdf", b"this is prose, not a pdf")
    with pytest.raises(UnsupportedFormat, match="look like application/pdf"):
        policy.accept("text/plain", b"%PDF-1.7\x00\x01\x02binary")


def test_unrecognizable_binary_is_refused_even_under_a_readable_declaration() -> None:
    with pytest.raises(UnsupportedFormat, match="not readable text/plain content"):
        policy.accept("text/plain", b"\x7fELF\x02\x01\x01\x00\x00")


def test_a_riff_container_aizk_has_no_reader_for_is_not_smuggled_in_as_webp() -> None:
    assert policy.riff.match(b"RIFF\x24\x00\x00\x00AVI LIST") is None
    assert policy.riff.match(b"RIFF\x01\x02") is None
    assert policy.riff.match(b"%PDF-1.7") is None
    with pytest.raises(UnsupportedFormat, match="not readable image/webp content"):
        policy.accept("image/webp", b"RIFF\x24\x00\x00\x00AVI LIST")


def test_utf8_split_across_the_sample_boundary_still_counts_as_text() -> None:
    # A multi-byte character straddling the sampled prefix must not read as binary.
    padded = b"a" * 8190 + "日本".encode()
    assert policy.textual(padded)
    assert policy.accept("text/plain", padded) == "text/plain"
    # A NUL byte inside real content is the reliable binary tell.
    assert not policy.textual(b"text\x00more")
    # Invalid UTF-8 well inside the sample is genuinely not text.
    assert not policy.textual(b"\xc3\x28 plain looking but invalid")


@given(st.text(min_size=1, max_size=200))
def test_any_authored_text_is_storable_as_a_note(text: str) -> None:
    content = text.encode()
    if "\x00" in text:
        with pytest.raises(UnsupportedFormat):
            policy.accept("text/markdown", content)
        return
    assert policy.accept("text/markdown", content) == "text/markdown"


def test_the_supported_set_is_exactly_what_the_deployment_can_open() -> None:
    assert "application/pdf" in policy.supported
    assert "audio/wav" in policy.supported
    # aizk has no video reader today, so video is refused rather than stored blind.
    assert not any(media.startswith("video/") for media in policy.supported)
