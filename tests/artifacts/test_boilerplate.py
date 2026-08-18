import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.artifacts.boilerplate import MarkdownBlock, WebBoilerplateCleaner

# A GitHub project page as Docling converts it, with the header menu, the saved-search dialog,
# the file list, the sidebar and the footer wrapped around the README a reader came for.
GITHUB_PAGE = """[Skip to content](https://github.com/#start-of-content)

## Navigation Menu

Toggle navigation

[ Sign in ](https://github.com/login?return_to=%2Fdatahub-project%2Fdatahub)

Platform

- [GitHub CopilotWrite better code with AI](https://github.com/features/copilot)

- [ActionsAutomate any workflow](https://github.com/features/actions)

- [Code securitySecure your code as you build](https://github.com/security)

Solutions

- [Enterprises](https://github.com/enterprise)

- [Small and medium teams](https://github.com/team)

- [Startups](https://github.com/enterprise/startups)

Pricing

- [Plans](https://github.com/pricing)

- [Compare plans](https://github.com/pricing#compare-features)

- [Contact Sales](https://github.com/enterprise/contact)

# Saved searches

## Use saved searches to filter your results more quickly

To see all available qualifiers, see our [documentation](https://docs.github.com/search-github).

Cancel Create saved search

[ Sign up ](https://github.com/signup?ref_cta=Sign+up&source=header-repo)

[README.md](https://github.com/datahub-project/datahub/blob/master/README.md)

[LICENSE](https://github.com/datahub-project/datahub/blob/master/LICENSE)

[build.gradle](https://github.com/datahub-project/datahub/blob/master/build.gradle)

## Repository files navigation

- [README](https://github.com/datahub-project/datahub#readme)

- [Code of conduct](https://github.com/datahub-project/datahub#coc)

- [Apache-2.0 license](https://github.com/datahub-project/datahub#license)

# The #1 Open Source AI Data Catalog

[ ](https://github.com/datahub-project/datahub/actions) [ ](https://pypi.org/p/acryl-datahub)

[Free Cloud Trial](https://datahub.com/trial) - [Quick Start](https://docs.datahub.com/start)
- [Live Demo](https://demo.datahub.com)

DataHub is the open-source AI data catalog that enables discovery, governance and observability
across your entire data ecosystem. Originally built at LinkedIn, DataHub now powers data discovery
at thousands of organizations worldwide, managing millions of data assets every single day.

The DataHub hackathon plan starts from the metadata graph, connecting every warehouse, lake and
BI tool through real time streaming ingestion so both human teams and agents read one fresh
catalog instead of a dozen stale ones. Ingestion recipes live beside the deployment guides.

```yaml
source:
  type: snowflake
  config:
    account_id: "xy12345.us-east-1"
```

## Footer

[ ](https://github.com) 2026 GitHub, Inc.

### Footer navigation

- [Terms](https://docs.github.com/site-policy/github-terms)

- [Privacy](https://docs.github.com/site-policy/privacy)

- [Security](https://github.com/security)

- [Status](https://www.githubstatus.com/)

- [Docs](https://docs.github.com/)

You can't perform that action at this time.
"""

# A paper landing page whose reference list is link dense yet entirely legitimate content.
PAPER_PAGE = """# Attention Is All You Need

We propose the Transformer, a model architecture eschewing recurrence and instead relying
entirely on an attention mechanism to draw global dependencies between input and output.

## References

[1] Bahdanau, D., Cho, K., and Bengio, Y. Neural machine translation by jointly learning to align
and translate. In ICLR, 2015. [arXiv:1409.0473](https://arxiv.org/abs/1409.0473)

[2] Hochreiter, S. and Schmidhuber, J. Long short-term memory. Neural Computation, 1997.
[doi:10.1162/neco.1997.9.8.1735](https://doi.org/10.1162/neco.1997.9.8.1735)

[3] Kingma, D. and Ba, J. Adam, a method for stochastic optimization. In ICLR, 2015.
[arXiv:1412.6980](https://arxiv.org/abs/1412.6980)

## Code and data

- [Reference implementation](https://github.com/tensorflow/tensor2tensor)

- [Trained checkpoints](https://storage.example.org/transformer/checkpoints)

- [Evaluation harness](https://gitlab.example.net/nlp/eval-harness)
"""


# One substantial paragraph, which is what tells the cleaner where a page's article begins.
ARTICLE = (
    "The engine keeps every conversion deterministic, so the same page converted twice produces "
    "the same text, the same chunks and therefore the same find ordering for any question an "
    "agent later asks about it, which is the whole point of preserving a source at all."
)


def cleaned_github() -> str:
    return WebBoilerplateCleaner().clean_page(GITHUB_PAGE, "text/html", "https://github.com/x/y")


def test_page_chrome_leaves_and_the_article_survives_intact() -> None:
    cleaned = cleaned_github()

    for chrome in (
        "Sign in",
        "Sign up",
        "Toggle navigation",
        "GitHub Copilot",
        "Contact Sales",
        "Saved searches",
        "build.gradle",
        "Repository files navigation",
        "Footer navigation",
        "githubstatus",
        "Terms",
    ):
        assert chrome not in cleaned

    assert "# The #1 Open Source AI Data Catalog" in cleaned
    assert "DataHub is the open-source AI data catalog" in cleaned
    assert "The DataHub hackathon plan starts from the metadata graph" in cleaned
    assert "account_id" in cleaned
    assert cleaned.endswith("\n")


def test_link_rows_survive_by_prose_or_by_pointing_off_the_page_site() -> None:
    cleaned = cleaned_github()

    # Curated rows of external links carry information a menu never does.
    assert "[Free Cloud Trial](https://datahub.com/trial)" in cleaned
    # Badge rows read as nothing at all, so they go whatever they link to.
    assert "https://pypi.org/p/acryl-datahub" not in cleaned


def test_a_reference_list_and_a_project_link_list_survive_untouched() -> None:
    cleaned = WebBoilerplateCleaner().clean_page(
        PAPER_PAGE, "text/html", "https://arxiv.org/abs/1706.03762"
    )

    assert "## References" in cleaned
    assert "Neural machine translation by jointly learning to align" in cleaned
    assert (
        "[doi:10.1162/neco.1997.9.8.1735](https://doi.org/10.1162/neco.1997.9.8.1735)" in cleaned
    )
    assert "## Code and data" in cleaned
    assert "[Reference implementation](https://github.com/tensorflow/tensor2tensor)" in cleaned
    assert "[Evaluation harness](https://gitlab.example.net/nlp/eval-harness)" in cleaned


def test_a_long_paragraph_survives_however_many_links_it_carries() -> None:
    paragraph = (
        "Finding the right project? This is the open-source catalog at "
        "[example.org](https://example.org/) which was previously hosted at "
        "[the old domain](https://example.org/legacy) and now redirects here. It is unrelated "
        "to [the other one](https://example.org/other), a separate hosting service, as the "
        "[FAQ](https://example.org/faq) explains at some length for anyone still unsure about "
        "which of the two projects a search engine happened to put in front of them today."
    )
    page = f"{paragraph}\n"

    assert WebBoilerplateCleaner().clean(page, "example.org") == page


def test_one_pointer_link_with_a_descriptive_label_outlives_a_bare_menu_entry() -> None:
    pointer = "[See all deployment guides for AWS, Azure and GCP](https://example.org/docs/deploy)"
    page = f"{ARTICLE}\n\n[Docs](https://example.org/docs)\n\n{pointer}\n"

    cleaned = WebBoilerplateCleaner().clean(page, "example.org")

    assert cleaned == f"{ARTICLE}\n\n{pointer}\n"


def test_a_documentation_index_of_internal_links_is_the_page_and_survives_whole() -> None:
    index = (
        "# Reference index\n\n"
        "## Guides\n\n"
        "- [Getting started](https://docs.example.org/start)\n"
        "- [Configuration](https://docs.example.org/config)\n"
        "- [Deployment](https://docs.example.org/deploy)\n\n"
        "## API\n\n"
        "- [Sessions](https://docs.example.org/api/sessions)\n"
        "- [Documents](https://docs.example.org/api/documents)\n"
        "- [Find](https://docs.example.org/api/find)\n"
    )

    cleaned = WebBoilerplateCleaner().clean_page(
        index, "text/html", "https://docs.example.org/reference"
    )

    assert cleaned == index


def test_a_menu_repeated_across_the_page_goes_wherever_it_appears() -> None:
    menu = "[Docs](https://example.org/docs) and nothing else worth reading here"
    page = f"{menu}\n\n# Title\n\nSome ordinary prose about the subject.\n\n{menu}\n"

    cleaned = WebBoilerplateCleaner().clean(page, "example.org")

    assert menu not in cleaned
    assert "Some ordinary prose about the subject." in cleaned


def test_only_a_fetched_web_page_is_cleaned_at_all() -> None:
    cleaner = WebBoilerplateCleaner()
    menu = (
        "## Footer\n\n"
        "- [Home](https://example.org/)\n- [Docs](https://example.org/docs)\n- [Blog](/blog)\n"
    )

    assert cleaner.clean_page(menu, "application/pdf", "https://example.org/paper.pdf") == menu
    assert cleaner.clean_page(menu, "text/html", None) == menu
    assert cleaner.clean_page(menu, "text/html", "file:///tmp/page.html") == menu
    assert cleaner.clean_page(menu, "text/html; charset=utf-8", "https://example.org/") == ""


def test_an_article_opening_under_a_menu_heading_ends_the_chrome_section() -> None:
    article = (
        "The engine keeps every conversion deterministic, so the same page converted twice "
        "produces the same text, the same chunks and therefore the same find ordering for "
        "any question an agent later asks about it, which is the whole point of the pipeline."
    )
    page = f"## Navigation Menu\n\n[Home](https://example.org/)\n\n{article}\n"

    cleaned = WebBoilerplateCleaner().clean(page, "example.org")

    assert cleaned == f"{article}\n"


@pytest.mark.parametrize(
    "intro",
    [
        "This page explains how the catalog indexes a warehouse.",
        "このページはカタログが倉庫を索引する方法を説明します。",
        "```\nfrom catalog import index\n```",
    ],
)
def test_a_short_introduction_under_a_menu_heading_is_never_eaten(intro: str) -> None:
    page = (
        "## Navigation Menu\n\n[Home](https://example.org/)\n\n[Docs](https://example.org/docs)\n\n"
        f"{intro}\n\n[Contact](https://example.org/contact)\n"
    )

    cleaned = WebBoilerplateCleaner().clean(page, "example.org")

    assert intro in cleaned
    assert "[Home](https://example.org/)" not in cleaned


def test_an_emptied_section_takes_its_heading_with_it_but_a_filled_one_stays() -> None:
    page = (
        f"## About\n\n{ARTICLE}\n\n"
        "### Topics\n\n[data-catalog](https://example.org/topics/data-catalog)"
        "[metadata](https://example.org/topics/metadata)\n\n"
        "### Releases\n"
    )

    cleaned = WebBoilerplateCleaner().clean(page, "example.org")

    assert cleaned == f"## About\n\n{ARTICLE}\n"


@pytest.mark.parametrize(
    ("markdown", "fenced", "blocks"),
    [
        ("```py\nx = 1\n\ny = 2\n```", True, 1),
        ("```py\nx = 1\n\ny = 2", True, 1),
        ("~~~\ncode\n~~~\n\ntext", True, 2),
        ("plain text", False, 1),
    ],
)
def test_fenced_code_stays_one_atomic_block_even_when_it_is_never_closed(
    markdown: str, fenced: bool, blocks: int
) -> None:
    parsed = tuple(MarkdownBlock.split(markdown))

    assert len(parsed) == blocks
    assert parsed[0].fenced is fenced
    assert WebBoilerplateCleaner().clean(markdown, "example.org").startswith(markdown[:6])


def test_a_block_measures_its_labels_destinations_and_free_prose() -> None:
    block = MarkdownBlock.parse(
        "See [the design doc](<https://docs.example.org/x>) at https://a.b"
    )

    assert block.links == ("<https://docs.example.org/x>",)
    assert block.label_chars == MarkdownBlock.readable("the design doc")
    assert block.prose_chars == MarkdownBlock.readable("See at")
    assert block.site_local("docs.example.org")
    assert block.site_local("example.org")
    assert not block.site_local("example.net")
    # A destination the page wrote as a path is always its own site.
    assert MarkdownBlock.parse("[Docs](/docs)").site_local("example.net")
    assert MarkdownBlock.parse("[ ](https://example.org/logo.png)").link_share == 1.0


def test_a_long_line_opening_with_a_hash_is_prose_rather_than_a_heading() -> None:
    comment = "# " + "shell command output that just happens to start with a hash " * 3

    assert MarkdownBlock.parse(comment).heading_level is None
    assert MarkdownBlock.parse("# Real Heading").heading_level == 1
    assert MarkdownBlock.parse("## Real Heading\ntrailing line").heading_level is None


@given(
    st.lists(
        st.sampled_from(
            [
                "- [Home](https://example.org/)\n- [Docs](https://example.org/docs)",
                "[ ](https://example.org/logo.png)",
                "# A heading",
                "```\ncode fence\n```",
                "Ordinary prose worth keeping in the document, several words long.",
                "See [the paper](https://arxiv.org/abs/1) for the derivation and the proofs.",
            ]
        ),
        max_size=12,
    )
)
def test_cleaning_only_ever_removes_whole_blocks_and_reaches_a_fixed_point(
    parts: list[str],
) -> None:
    page = "\n\n".join(parts)
    cleaner = WebBoilerplateCleaner()

    cleaned = cleaner.clean(page, "example.org")

    assert all(block in parts for block in cleaned.rstrip("\n").split("\n\n") if block)
    assert cleaner.clean(cleaned, "example.org") == cleaned


@given(st.text(alphabet=st.characters(categories=("L", "N", "Zs")), min_size=300, max_size=600))
def test_a_block_of_pure_prose_is_never_chrome(prose: str) -> None:
    page = f"{prose}\n"

    assert WebBoilerplateCleaner().clean(page, "example.org") == page
