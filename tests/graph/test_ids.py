import uuid

from hypothesis import given
from hypothesis import strategies as st
from strategies import entity_types, predicates, short_text

from aizk.graph.ids import entity_id, fact_id, normalize
from aizk.graph.naming import normalize_name


def messy(value: str) -> st.SearchStrategy[str]:
    """Surface variants of a name that the canonical fold must collapse onto one key.

    Wraps the value in extra outer whitespace and flips its case, the noise a second extraction of
    the same thing carries, so a content-addressed id derived from the fold ignores all of it.

    value: the base surface form to perturb.
    """
    pad = st.text(alphabet=" \t\n", max_size=3)
    return st.builds(lambda lead, trail: f"{lead}{value.swapcase()}{trail}", lead=pad, trail=pad)


@given(name=short_text, type=entity_types, data=st.data())
def test_entity_id_folds_case_and_whitespace(name: str, type: str, data: st.DataObject) -> None:
    """entity_id is content-addressed, stable across calls and blind to case and outer spacing."""
    noisy_name = data.draw(messy(name))
    noisy_type = data.draw(messy(type))

    base = entity_id(name, type)
    assert isinstance(base, uuid.UUID)
    assert base == entity_id(name, type)
    assert base == entity_id(noisy_name, noisy_type)


@given(
    subject=short_text,
    predicate=predicates,
    object_=short_text,
    statement=short_text,
    data=st.data(),
)
def test_fact_id_folds_case_and_whitespace(
    subject: str, predicate: str, object_: str, statement: str, data: st.DataObject
) -> None:
    """fact_id is content-addressed, stable across calls and blind to case and outer spacing."""
    base = fact_id(subject, predicate, object_, statement)
    noisy = fact_id(
        data.draw(messy(subject)),
        data.draw(messy(predicate)),
        data.draw(messy(object_)),
        data.draw(messy(statement)),
    )
    assert isinstance(base, uuid.UUID)
    assert base == fact_id(subject, predicate, object_, statement) == noisy


@given(statement=short_text, other=short_text)
def test_fact_id_separates_distinct_statements(statement: str, other: str) -> None:
    """Two facts that differ only in a statement of a different normal form mint different ids."""
    if normalize(statement) == normalize(other):
        return
    left = fact_id("Ada", "wrote", "Notes", statement)
    right = fact_id("Ada", "wrote", "Notes", other)
    assert left != right


@given(stem=st.text(alphabet="abcdefghij", min_size=1, max_size=8))
def test_slug_spellings_share_one_entity_id(stem: str) -> None:
    """A kebab slug, its snake form, and its spaced wording fold onto one entity node."""
    parts = [stem, stem, stem]
    spaced = entity_id(" ".join(parts), "Concept")
    kebab = entity_id("-".join(parts), "Concept")
    snake = entity_id("_".join(parts), "Concept")
    wikilink = entity_id(f"[[{' '.join(parts)}]]", "Concept")
    assert spaced == kebab == snake == wikilink


@given(name=short_text)
def test_normalize_name_is_idempotent(name: str) -> None:
    """Folding an already-folded name leaves it unchanged, the canonical key is a fixed point."""
    once = normalize_name(name)
    assert normalize_name(once) == once


@given(
    head=st.sampled_from(["", ".", "..", "~"]),
    tail=st.text(alphabet="abc/", min_size=1, max_size=12),
)
def test_path_like_names_fold_to_empty(head: str, tail: str) -> None:
    """A path or url the extractor mistook for a thing folds to empty so the caller drops it."""
    assert normalize_name(f"{head}/{tail}") == ""
    assert normalize_name(f"https://{tail}") == ""
