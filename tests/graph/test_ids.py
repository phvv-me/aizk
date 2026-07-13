from hypothesis import given
from hypothesis import strategies as st

from aizk.graph.ids import entity_id, fact_id, normalize
from aizk.graph.naming import normalize_name

names = st.text(min_size=1, max_size=30)


@given(value=st.text())
def test_normalize_collapses_whitespace_casefolds_and_is_idempotent(value: str) -> None:
    once = normalize(value)
    assert once == " ".join(value.split()).casefold()  # the exact fold, the spec
    assert normalize(once) == once  # idempotent
    assert "  " not in once and once == once.strip()


@given(value=names)
def test_normalize_name_is_idempotent(value: str) -> None:
    once = normalize_name(value)
    assert normalize_name(once) == once


@given(label=st.text(alphabet=st.characters(categories=["Ll", "Lu"]), min_size=1, max_size=20))
def test_wikilink_and_markdown_unwrap_to_label(label: str) -> None:
    key = normalize_name(label)
    assert normalize_name(f"[[{label}]]") == key
    assert normalize_name(f"[[{label}|shown differently]]") == key
    assert normalize_name(f"[{label}](https://example.com/x)") == key


@given(
    head=st.sampled_from(["/", "./", "../", "~/", "https://", "http://", "file://"]),
    tail=st.text(alphabet="abcdef/._-", min_size=1, max_size=20),
)
def test_path_or_url_folds_to_empty(head: str, tail: str) -> None:
    assert normalize_name(f"{head}{tail}") == ""


def test_kebab_and_spaced_and_accented_converge() -> None:
    key = normalize_name("team memory spine")
    assert normalize_name("team-memory-spine") == key
    assert normalize_name("Team Memory Spine") == key
    assert normalize_name("café") == normalize_name("CAFÉ")


@given(name=names, type_=names)
def test_entity_id_is_deterministic_uuid5(name: str, type_: str) -> None:
    once = entity_id(name, type_)
    assert once == entity_id(name, type_)
    assert once.version == 5


def test_entity_id_collapses_slug_equivalent_names() -> None:
    assert entity_id("team-memory-spine", "project") == entity_id("Team Memory Spine", "project")


@given(
    subject=names,
    predicate=names,
    object_=st.text(max_size=30),
    statement=names,
)
def test_fact_id_is_deterministic_over_its_triple(
    subject: str, predicate: str, object_: str, statement: str
) -> None:
    once = fact_id(subject, predicate, object_, statement)
    assert once == fact_id(subject, predicate, object_, statement)
    assert once.version == 5


def test_distinct_triples_do_not_collide_on_the_delimiter() -> None:
    assert fact_id("a b", "uses", "", "s") != fact_id("a", "b uses", "", "s")
