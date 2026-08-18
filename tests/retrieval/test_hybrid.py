from sqlalchemy.dialects import postgresql

from aizk.config import settings
from aizk.retrieval import Plan, QueryContext
from aizk.retrieval.find import build_find_statement


def compiled(owned: bool = False) -> str:
    """The find statement one context compiles to, as PostgreSQL text."""
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=True, owned=owned)
    statement = build_find_statement(context, Plan.focused())
    return str(statement.compile(dialect=postgresql.dialect()))


def ranking_window(sql: str, lane: str) -> str:
    """The parenthesised body of one lane's materialized ranking window."""
    start = sql.index("(", sql.index(f"{lane}_window AS MATERIALIZED"))
    depth = 0
    for end in range(start, len(sql)):
        depth += (sql[end] == "(") - (sql[end] == ")")
        if depth == 0:
            return sql[start:end]
    raise AssertionError(f"{lane}_window is unbalanced")


def test_find_compiles_the_vchord_bm25_lexical_lane() -> None:
    sql = compiled()
    assert "to_bm25query" in sql
    assert "tokenize" in sql
    assert "<&>" in sql
    assert "fusion_depth" in sql
    assert "row_number() OVER" in sql


def test_chunk_rankings_walk_their_index_before_any_document_is_joined() -> None:
    sql = compiled()
    for lane in ("dense", "lexical"):
        window = ranking_window(sql, lane)
        # Joining document inside the ranking costs the planner the chunk index, so the
        # window ranks chunks alone and over-fetches for the join that follows it.
        assert "JOIN document" not in window
        # Each driver spells a bound parameter its own way and only some cast it, so assert the
        # over-fetch arithmetic itself rather than one driver's rendering of it.
        limit = window[window.rindex("LIMIT") :]
        assert "fusion_depth" in limit and "*" in limit and "fusion_overfetch" in limit
        assert f"FROM {lane}_window JOIN document" in sql


def test_an_owned_ranking_keeps_its_selective_scope_predicate_inside_the_cut() -> None:
    sql = compiled(owned=True)
    assert "dense_window" not in sql and "lexical_window" not in sql
    assert sql.count("FROM chunk JOIN document") == 3
    assert sql.count("%(qscopes)s") == 3
