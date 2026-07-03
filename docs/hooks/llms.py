"""MkDocs post-build hook that writes english `llms.txt` and `llms-full.txt`.

This replaces `mkdocs-llmstxt`, which produces empty output when
`mkdocs-static-i18n` is also active (its per-locale rebuilds wipe the
plugin's page cache). The hook runs once after the whole build and reads
the english source markdown straight from the `docs/` tree, so it is
immune to the i18n rebuild dance and always emits english only.

`SECTIONS` mirrors the section layout this package wants in its index.
Each entry maps a section name to the english doc paths under `docs/`,
in order. Translated copies carry a locale suffix (`.es.md`, `.ja.md`)
and are ignored.
"""

import re
from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig

SECTIONS: dict[str, list[str]] = {
    "Usage": ["index.md"],
    "Reference": ["api.md", "release.md"],
}

LOCALE_SUFFIXES = (".pt-BR", ".es", ".ja", ".zh")

FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
FENCED_CODE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def title_for(markdown: str, doc_path: str) -> str:
    """Pick a link title: the page's first `# ` heading, else the file stem.

    Headings inside fenced code blocks (like `# paste ...` comments) do not
    count, so a hero-only page with no real heading falls back to the stem.

    markdown: full source text of the page.
    doc_path: path of the page relative to `docs/`, used for the fallback.
    """
    match = H1.search(FENCED_CODE.sub("", markdown))
    if match:
        return match.group(1)
    stem = Path(doc_path).stem
    return "Home" if stem == "index" else stem


def page_url(site_url: str, doc_path: str) -> str:
    """Build the english page URL the way `mkdocs-llmstxt` does.

    With directory URLs, `foo/bar.md` is served at `foo/bar/` and the
    markdown twin lives at `foo/bar/index.md`. `index.md` maps to the
    section root, so its twin is `index.md` at the site root.

    site_url: the configured `site_url`, with a trailing slash.
    doc_path: path of the page relative to `docs/`.
    """
    base = site_url if site_url.endswith("/") else f"{site_url}/"
    stem = doc_path[: -len(".md")]
    twin = "index.md" if stem == "index" else f"{stem}/index.md"
    return f"{base}{twin}"


def read_page(docs_dir: Path, doc_path: str) -> str:
    """Read a source page and strip any YAML front-matter.

    docs_dir: the `docs/` directory.
    doc_path: path of the page relative to `docs/`.
    """
    text = (docs_dir / doc_path).read_text(encoding="utf-8")
    return FRONT_MATTER.sub("", text).strip()


def on_post_build(config: MkDocsConfig) -> None:
    """Write english `llms.txt` and `llms-full.txt` into the built `site/`."""
    site_dir = Path(config.site_dir)
    docs_dir = Path(config.docs_dir)
    site_url = config.site_url or ""
    name = config.site_name
    description = config.site_description or ""

    header = f"# {name}\n\n> {description}\n"

    index_lines = [header]
    full_lines = [header]
    for section, doc_paths in SECTIONS.items():
        index_lines.append(f"\n## {section}\n")
        for doc_path in doc_paths:
            if any(Path(doc_path).stem.endswith(suffix) for suffix in LOCALE_SUFFIXES):
                continue
            markdown = read_page(docs_dir, doc_path)
            title = title_for(markdown, doc_path)
            index_lines.append(f"- [{title}]({page_url(site_url, doc_path)})")
            full_lines.append(f"\n# {title}\n\n{markdown}\n")

    (site_dir / "llms.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    (site_dir / "llms-full.txt").write_text("\n".join(full_lines) + "\n", encoding="utf-8")
