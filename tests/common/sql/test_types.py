import sqlalchemy as sa
from common.sql.model import Doc, compiled
from hypothesis import given
from hypothesis import strategies as st


@given(key=st.sampled_from(("summary", "source", "an odd key")))
def test_rshift_extracts_one_key_as_text(key: str) -> None:
    """`attributes ->> key` with the key bound as a parameter, typed as text."""
    expression = Doc.attributes >> key
    assert compiled(expression) == f"doc.attributes ->> '{key}'"
    assert isinstance(expression.type, sa.Text)


def test_getitem_without_a_type_stays_plain_jsonb_element_access() -> None:
    """A non-type index defers to the parent comparator's `->` subscript."""
    expression = Doc.attributes["key"]
    assert compiled(expression) == "doc.attributes['key']"


def test_matmul_renders_the_pgvector_cosine_operator() -> None:
    """`embedding <=> other` with a float result type for ordering."""
    expression = Doc.embedding @ sa.column("query_vector")
    assert compiled(expression) == "doc.embedding <=> query_vector"
    assert isinstance(expression.type, sa.Float)
