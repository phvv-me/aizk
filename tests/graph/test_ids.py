from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.graph.ids import entity_id, fact_id, normalize
from aizk.graph.naming import normalize_name

names = st.text(min_size=1, max_size=30)


@pytest.mark.parametrize("normalizer", [normalize, normalize_name], ids=["identity", "name"])
@given(value=st.text())
def test_normalizers_are_idempotent(normalizer: Callable[[str], str], value: str) -> None:
    once = normalizer(value)
    assert normalizer(once) == once
    assert "  " not in once and once == once.strip()
    if normalizer is normalize:
        assert once == " ".join(value.split()).casefold()


@given(label=st.text(alphabet=st.characters(categories=["Ll", "Lu"]), min_size=1, max_size=20))
def test_name_normalization_unwraps_labels_and_rejects_locations(label: str) -> None:
    key = normalize_name(label)
    assert normalize_name(f"[[{label}]]") == key
    assert normalize_name(f"[[{label}|shown differently]]") == key
    assert normalize_name(f"[{label}](https://example.com/x)") == key
    key = normalize_name("team memory spine")
    assert normalize_name("team-memory-spine") == key
    assert normalize_name("Team Memory Spine") == key
    assert normalize_name("café") == normalize_name("CAFÉ")
    for head in ("/", "./", "../", "~/", "https://", "http://", "file://"):
        assert normalize_name(f"{head}a/path") == ""


@given(
    name=names,
    type_=names,
    predicate=names,
    object_=st.text(max_size=30),
    statement=names,
)
def test_graph_ids_are_deterministic_uuid5_values_with_unambiguous_parts(
    name: str,
    type_: str,
    predicate: str,
    object_: str,
    statement: str,
) -> None:
    subject_id = entity_id(name, type_)
    assert subject_id == entity_id(name, type_)
    assert subject_id.version == 5
    assert entity_id("team-memory-spine", "project") == entity_id("Team Memory Spine", "project")
    object_id = entity_id(object_, "concept") if object_ else None
    once = fact_id(subject_id, predicate, object_id, statement)
    assert once == fact_id(subject_id, predicate, object_id, statement)
    assert once.version == 5
    assert fact_id(entity_id("a b", "concept"), "uses", None, "s") != fact_id(
        entity_id("a", "concept"), "b uses", None, "s"
    )
