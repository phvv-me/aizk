from patos import FrozenModel

_TEXT_SAMPLE = 8192

# One signature per binary family aizk can open. A caller's declared media type is a claim,
# these leading bytes are the evidence, and the two have to agree before anything is stored.
_SIGNATURES: tuple[tuple[bytes, frozenset[str]], ...] = (
    (b"%PDF-", frozenset({"application/pdf"})),
    (b"\x89PNG\r\n\x1a\n", frozenset({"image/png"})),
    (b"\xff\xd8\xff", frozenset({"image/jpeg"})),
    (b"GIF87a", frozenset({"image/gif"})),
    (b"GIF89a", frozenset({"image/gif"})),
    (b"II*\x00", frozenset({"image/tiff"})),
    (b"MM\x00*", frozenset({"image/tiff"})),
    (b"BM", frozenset({"image/bmp"})),
    # Every OOXML document and every EPUB is a Zip container, so the container proves only
    # that the family is right and the declared type decides which member of it this is.
    (
        b"PK\x03\x04",
        frozenset(
            {
                "application/epub+zip",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
    ),
)

# Formats whose bytes are text, recognized by decoding rather than by a leading signature.
_TEXTUAL = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
        "text/asciidoc",
        "text/csv",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
        "text/xml",
    }
)

# A handful of spellings clients send for formats aizk already reads.
_ALIASES = {
    "application/x-pdf": "application/pdf",
    "image/jpg": "image/jpeg",
    "image/x-ms-bmp": "image/bmp",
    "text/x-markdown": "text/markdown",
}


class UnsupportedFormat(ValueError):
    """The artifact is in a format this deployment cannot read, so it is never stored."""


class RIFFSignature(FrozenModel):
    """Match the RIFF container families whose kind sits four bytes past the header."""

    forms: dict[str, str] = {"WEBP": "image/webp"}

    def match(self, content: bytes) -> str | None:
        """Return the media type this RIFF payload actually carries, if it is one aizk reads."""
        if not content.startswith(b"RIFF") or len(content) < 12:
            return None
        return self.forms.get(content[8:12].decode("ascii", "replace"))


class FormatPolicy(FrozenModel):
    """Accept only formats aizk can open, deciding from the bytes rather than the claim.

    An artifact aizk cannot read is one it cannot convert, index, compress deliberately, or
    ever recall, so it is refused while the caller still holds the file rather than kept as
    an opaque blob that only costs storage. The check runs before the bytes reach the object
    store, which is what makes the refusal free.
    """

    riff: RIFFSignature = RIFFSignature()

    @property
    def supported(self) -> frozenset[str]:
        """Every media type this deployment accepts."""
        signatures = frozenset().union(*(types for _, types in _SIGNATURES))
        return signatures | _TEXTUAL | frozenset(self.riff.forms.values())

    def normalize(self, media_type: str) -> str:
        """Drop parameters and casing from a declared media type and fold known spellings."""
        bare = media_type.partition(";")[0].strip().casefold()
        return _ALIASES.get(bare, bare)

    def accept_declaration(self, media_type: str) -> str:
        """Return the normalized media type, or refuse a format aizk has no reader for."""
        declared = self.normalize(media_type)
        if declared not in self.supported:
            raise UnsupportedFormat(
                f"aizk cannot read {declared or 'an undeclared format'},"
                " so it was not stored. Convert it to a supported format first."
            )
        return declared

    def accept(self, media_type: str, content: bytes) -> str:
        """Return the media type to store, or refuse an artifact aizk cannot read.

        A declared type that the leading bytes contradict is refused rather than quietly
        corrected, because the disagreement means the caller and the store hold different
        beliefs about the same file and only the caller can settle which one is wrong.
        """
        declared = self.accept_declaration(media_type)
        observed = self.sniff(content)
        if observed is None:
            raise UnsupportedFormat(
                f"the uploaded bytes are not readable {declared} content, so nothing was stored."
            )
        if declared not in observed:
            raise UnsupportedFormat(
                f"the uploaded bytes look like {self.describe(observed)},"
                f" not the declared {declared}, so nothing was stored."
            )
        return declared

    def describe(self, observed: frozenset[str]) -> str:
        """Name what the bytes turned out to be, without reciting every textual spelling."""
        if observed == _TEXTUAL:
            return "plain text"
        return "/".join(sorted(observed))

    def sniff(self, content: bytes) -> frozenset[str] | None:
        """Report which supported media types these bytes could be, or nothing recognizable."""
        for signature, types in _SIGNATURES:
            if content.startswith(signature):
                return types
        riff = self.riff.match(content)
        if riff is not None:
            return frozenset({riff})
        return _TEXTUAL if self.textual(content) else None

    def textual(self, content: bytes) -> bool:
        """Whether a leading sample decodes as UTF-8 text without embedded NUL bytes.

        Decoding a prefix can split a multi-byte character, so a sample that fails only at
        its own tail still counts as text.
        """
        sample = content[:_TEXT_SAMPLE]
        if b"\x00" in sample:
            return False
        try:
            sample.decode()
        except UnicodeDecodeError as split:
            return split.start >= len(sample) - 3
        return True


__all__ = ["FormatPolicy", "RIFFSignature", "UnsupportedFormat"]
