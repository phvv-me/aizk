import uuid

from hypothesis import given
from hypothesis import strategies as st

from aizk.graph.ids import DELIMITER, NAMESPACE, entity_id, fact_id, normalize
from aizk.graph.naming import normalize_name

names = st.text(min_size=1, max_size=30)


@given(value=st.text())
def test_normalize_collapses_whitespace_casefolds_and_is_idempotent(value: str) -> None:
    """`normalize` is `casefold` over whitespace-collapsed text, and reapplying it is stable."""
    once = normalize(value)
    assert once == " ".join(value.split()).casefold()  # the exact fold, the spec
    assert normalize(once) == once  # idempotent
    assert "  " not in once and once == once.strip()


def test_normalize_is_case_insensitive_for_ascii() -> None:
    """An ASCII name folds to one key regardless of the case it arrives in."""
    assert normalize("Team Memory") == normalize("team memory") == normalize("TEAM MEMORY")


@given(value=names)
def test_normalize_name_is_idempotent(value: str) -> None:
    """Folding an already-folded name to a slug key is stable, the interning invariant."""
    once = normalize_name(value)
    assert normalize_name(once) == once


@given(label=st.text(alphabet=st.characters(categories=["Ll", "Lu"]), min_size=1, max_size=20))
def test_wikilink_and_markdown_unwrap_to_label(label: str) -> None:
    """A `[[label]]`, `[[label|alias]]`, and `[label](url)` all fold to the bare label's key."""
    key = normalize_name(label)
    assert normalize_name(f"[[{label}]]") == key
    assert normalize_name(f"[[{label}|shown differently]]") == key
    assert normalize_name(f"[{label}](https://example.com/x)") == key


@given(
    head=st.sampled_from(["/", "./", "../", "~/", "https://", "http://", "file://"]),
    tail=st.text(alphabet="abcdef/._-", min_size=1, max_size=20),
)
def test_path_or_url_folds_to_empty(head: str, tail: str) -> None:
    """A name that is really a path or url folds to empty so the caller drops it, not an entity."""
    assert normalize_name(f"{head}{tail}") == ""


def test_kebab_and_spaced_and_accented_converge() -> None:
    """A slug token, its spaced wording, and an accented spelling reduce to one stable key."""
    key = normalize_name("team memory spine")
    assert normalize_name("team-memory-spine") == key
    assert normalize_name("Team Memory Spine") == key
    assert normalize_name("café") == normalize_name("CAFÉ")


@given(name=names, type_=names)
def test_entity_id_is_deterministic_uuid5(name: str, type_: str) -> None:
    """`entity_id` is a stable uuid5 over the normalized (type, name), the content address."""
    once = entity_id(name, type_)
    assert once == entity_id(name, type_)
    assert once == uuid.uuid5(NAMESPACE, DELIMITER.join((normalize(type_), normalize_name(name))))
    assert once.version == 5


def test_entity_id_collapses_slug_equivalent_names() -> None:
    """Two surface forms of one name mint one entity id, making ingestion idempotent."""
    assert entity_id("team-memory-spine", "Project") == entity_id("Team Memory Spine", "Project")


@given(
    subject=names,
    predicate=names,
    object_=st.text(max_size=30),
    statement=names,
)
def test_fact_id_is_deterministic_over_its_triple(
    subject: str, predicate: str, object_: str, statement: str
) -> None:
    """`fact_id` is a stable uuid5 over the normalized triple and statement."""
    once = fact_id(subject, predicate, object_, statement)
    assert once == fact_id(subject, predicate, object_, statement)
    assert once.version == 5


def test_distinct_triples_do_not_collide_on_the_delimiter() -> None:
    """The unit-separator delimiter keeps distinct field tuples from hashing to one id."""
    assert fact_id("a b", "uses", "", "s") != fact_id("a", "b uses", "", "s")
