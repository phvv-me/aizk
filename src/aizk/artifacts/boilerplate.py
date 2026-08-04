import re
from collections import Counter
from collections.abc import Iterator, Sequence
from typing import Self
from urllib.parse import urlsplit

from patos import FrozenModel
from pydantic import NonNegativeInt, PositiveFloat, PositiveInt

_LINK = re.compile(r"!?\[(?P<label>[^\]\n]*)\]\((?P<destination>[^)\s]*)\)")
_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+)$")
_FENCE = re.compile(r"^ {0,3}(?P<marker>```|~~~)")
_BARE_URL = re.compile(r"<?\bhttps?://\S+")
_WEB_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# A real heading is one short line, so a longer block opening with a hash is prose or code.
_MAX_HEADING_CHARS = 120

# Section headings a converted page carries around its article rather than inside it. Each one
# is matched whole, so an article heading that merely mentions a word here still survives.
_CHROME_HEADINGS = frozenset(
    {
        "breadcrumb",
        "breadcrumbs",
        "cookie preferences",
        "cookie settings",
        "footer",
        "footer navigation",
        "main navigation",
        "navigation",
        "navigation menu",
        "page navigation",
        "primary navigation",
        "repository files navigation",
        "saved searches",
        "site footer",
        "site navigation",
        "skip to content",
    }
)


class MarkdownBlock(FrozenModel):
    """One blank-line separated Markdown block measured for readable text and link density."""

    text: str
    fenced: bool = False
    heading_level: PositiveInt | None = None
    title: str | None = None
    links: tuple[str, ...] = ()
    label_chars: NonNegativeInt = 0
    prose_chars: NonNegativeInt = 0

    @classmethod
    def parse(cls, text: str, fenced: bool = False) -> Self:
        """Measure one block's destinations, link-label text, and free prose."""
        heading = None if fenced else cls.heading(text)
        links = tuple(_LINK.finditer(text))
        return cls(
            text=text,
            fenced=fenced,
            heading_level=len(heading.group("hashes")) if heading else None,
            title=" ".join(heading.group("title").split()).casefold() if heading else None,
            links=tuple(link.group("destination") for link in links),
            label_chars=sum(cls.readable(link.group("label")) for link in links),
            prose_chars=cls.readable(_BARE_URL.sub(" ", _LINK.sub(" ", text))),
        )

    @classmethod
    def split(cls, markdown: str) -> Iterator[Self]:
        """Split Markdown on blank lines while a fenced code block stays one atomic block."""
        pending: list[str] = []
        fence: str | None = None
        for line in markdown.split("\n"):
            opening = _FENCE.match(line)
            if opening and fence is None:
                fence = opening.group("marker")
            elif opening and opening.group("marker") == fence:
                yield cls.parse("\n".join([*pending, line]), fenced=True)
                pending, fence = [], None
                continue
            if fence is None and not line.strip():
                if pending:
                    yield cls.parse("\n".join(pending))
                pending = []
                continue
            pending.append(line)
        if pending:
            yield cls.parse("\n".join(pending), fenced=fence is not None)

    @classmethod
    def heading(cls, text: str) -> re.Match[str] | None:
        """Match a block that is one short line opening with hashes, as a real heading is."""
        line = text.strip()
        match = None if "\n" in line else _HEADING.match(line)
        return (
            match if match and cls.readable(match.group("title")) <= _MAX_HEADING_CHARS else None
        )

    @staticmethod
    def readable(text: str) -> int:
        """Count the characters a reader actually reads, ignoring markup and punctuation."""
        return sum(character.isalnum() for character in text)

    @property
    def visible_chars(self) -> int:
        """Readable characters anywhere in the block, inside a link label or outside one."""
        return self.label_chars + self.prose_chars

    @property
    def link_share(self) -> float:
        """Share of the readable characters that sit inside link labels."""
        return self.label_chars / self.visible_chars if self.visible_chars else 1.0

    @property
    def signature(self) -> str:
        """Whitespace-insensitive identity used to spot a block the page repeats."""
        return " ".join(self.text.split()).casefold()

    def site_local(self, host: str) -> bool:
        """Whether most destinations stay inside the page's own site, as a menu does."""
        local = sum(self.same_site(destination, host) for destination in self.links)
        return local * 2 >= len(self.links)

    @staticmethod
    def same_site(destination: str, host: str) -> bool:
        """Whether one destination is relative to the page or points at its own site."""
        target = urlsplit(destination.strip("<>")).hostname
        if target is None:
            return True
        return target == host or target.endswith(f".{host}") or host.endswith(f".{target}")


class BlockPlacement(FrozenModel):
    """Where one block sits on the page, which is evidence its own shape cannot carry.

    A menu repeats, or wraps around the article rather than sitting inside it, or stands under
    a chrome heading. A one-off list of links inside the article is the document's own
    substance, such as a documentation index, so it needs one of these to read as chrome.
    """

    repeated: bool
    outside: bool
    sectioned: bool

    @property
    def corroborates(self) -> bool:
        """Whether the page's layout backs a block that merely looks like a menu."""
        return self.outside or self.sectioned


class WebBoilerplateCleaner(FrozenModel):
    """Drop the navigation, menu, and footer chrome of a converted web page.

    Converting a page yields the site's header, menus, dialogs, and footer beside the article,
    and those blocks become chunks that outrank real notes because they are dense in the words
    a query shares with any page. They are separated by measuring how much of a block a reader
    would actually read, so a link block that carries its own prose, such as a reference list,
    stays while a short block of bare menu links goes.
    """

    link_density: PositiveFloat = 0.6
    min_prose_chars: PositiveInt = 200
    min_intro_chars: PositiveInt = 24
    min_menu_links: PositiveInt = 3
    max_menu_chars: PositiveInt = 36
    chrome_headings: frozenset[str] = _CHROME_HEADINGS

    def clean_page(self, markdown: str, media_type: str, source_uri: str | None) -> str:
        """Clean only Markdown converted from a fetched web page and pass anything else through."""
        source = urlsplit(source_uri or "")
        page = source.scheme in ("http", "https") and source.hostname is not None
        if not page or media_type.partition(";")[0].strip().lower() not in _WEB_MEDIA_TYPES:
            return markdown
        return self.clean(markdown, str(source.hostname))

    def clean(self, markdown: str, host: str) -> str:
        """Drop the page's chrome and keep every content block in its original order.

        A chrome heading opens a section this discards outright, but only until the first block
        that reads like content at all, so a short article introduction under a menu survives.
        The rest of that section stays chrome context, which together with repetition and the
        span the article occupies is the evidence a menu-shaped block needs before it is dropped.
        """
        blocks = tuple(MarkdownBlock.split(markdown))
        seen = Counter(block.signature for block in blocks if block.links and not block.fenced)
        repeated = frozenset(signature for signature, count in seen.items() if count > 1)
        kept: list[MarkdownBlock] = []
        chrome_level: int | None = None
        discarding = False
        article = self.article(blocks)
        for index, block in enumerate(blocks):
            if chrome_level is not None and self.outranks(block, chrome_level):
                chrome_level, discarding = None, False
            if block.title in self.chrome_headings:
                chrome_level, discarding = block.heading_level, True
                continue
            if discarding and not self.content_like(block):
                continue
            discarding = False
            placement = BlockPlacement(
                repeated=block.signature in repeated,
                outside=index not in article,
                sectioned=chrome_level is not None,
            )
            if not self.chrome(block, host, placement):
                kept.append(block)
        return self.render(self.prune(kept))

    def article(self, blocks: Sequence[MarkdownBlock]) -> range:
        """The span from the page's first substantial paragraph to its last one.

        Header and footer chrome wraps around that span, so a block outside it is placed like
        chrome. A page that never writes a paragraph, such as a link index, has no such span
        and is treated as article throughout rather than as one long menu.
        """
        body = [
            index
            for index, block in enumerate(blocks)
            if block.prose_chars >= self.min_prose_chars
        ]
        return range(body[0], body[-1] + 1) if body else range(len(blocks))

    @staticmethod
    def outranks(block: MarkdownBlock, chrome_level: int) -> bool:
        """Whether one heading closes the chrome section a shallower heading opened."""
        return block.heading_level is not None and block.heading_level <= chrome_level

    def content_like(self, block: MarkdownBlock) -> bool:
        """Whether one block reads as content rather than as another entry of a menu.

        A sentence is enough, since a page may open its article right under a menu with no
        heading of its own, and a few dozen readable characters is already a sentence in a
        language that writes densely.
        """
        if block.heading_level is not None:
            return False
        return block.fenced or (
            block.prose_chars >= self.min_intro_chars and block.link_share < self.link_density
        )

    def chrome(self, block: MarkdownBlock, host: str, placement: BlockPlacement) -> bool:
        """Whether one block reads as site chrome instead of content worth recalling.

        Prose value comes first, so a long block survives whatever its links look like. A block
        whose readable text is mostly link labels pointing back into the same site is only a
        menu when the page's own layout says so, which keeps a documentation index or a release
        note full of internal links intact.
        """
        if block.fenced or block.heading_level is not None or not block.links:
            return False
        if block.prose_chars >= self.min_prose_chars:
            return False
        if not block.visible_chars or placement.repeated:
            return True
        if block.link_share < self.link_density or not block.site_local(host):
            return False
        if not placement.corroborates:
            return False
        return (
            len(block.links) >= self.min_menu_links or block.visible_chars <= self.max_menu_chars
        )

    @staticmethod
    def prune(blocks: Sequence[MarkdownBlock]) -> list[MarkdownBlock]:
        """Drop every heading whose whole section became empty once its chrome blocks left."""
        kept: list[MarkdownBlock] = []
        for block in reversed(blocks):
            if block.heading_level is not None:
                following = kept[-1].heading_level if kept else None
                if not kept or (following is not None and following <= block.heading_level):
                    continue
            kept.append(block)
        kept.reverse()
        return kept

    @staticmethod
    def render(blocks: Sequence[MarkdownBlock]) -> str:
        """Join the kept blocks back into deterministic Markdown with one trailing newline."""
        body = "\n\n".join(block.text for block in blocks)
        return f"{body}\n" if body else ""
