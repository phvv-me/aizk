from sqlalchemy.dialects import postgresql

from aizk.config import settings
from aizk.retrieval import Plan, QueryContext
from aizk.retrieval.recall import build_recall_statement


def test_recall_compiles_the_vchord_bm25_lexical_lane() -> None:
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=True)
    statement = build_recall_statement(context, Plan.focused())
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "to_bm25query" in sql
    assert "tokenize" in sql
    assert "<&>" in sql
    assert "fusion_depth" in sql
    assert "row_number() OVER" in sql
