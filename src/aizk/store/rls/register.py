from sqlalchemy import Table, event
from sqlalchemy.orm import Mapper

from ..mixins.base import TableBase

# aizk keeps this mapper-construction hook as its own, rather than calling the standalone `rls`
# library's more general `rls.register(Base)` (https://github.com/phvv-me/rls): every aizk model's
# `__rls_policies__` is always a classmethod (never a plain list), and this hook does one thing the
# generic library's does not, populating `metadata.info["rls"]` alongside `metadata.info
# ["rls_policies"]`, the autogenerate guard set `cli.py`'s `check-rls` command and every DB-backed
# test in `tests/store/test_rls.py` read as the expected table set.


@event.listens_for(Mapper, "after_mapper_constructed")
def register_policies(mapper: Mapper, class_: type) -> None:
    """Read a freshly mapped class's own declared policies into the shared metadata registry.

    Fires once per mapped class, after SQLAlchemy has built its `Table`, so a `Policy`'s SQLAlchemy
    expressions can already reach `cls.__table__.c` for the columns they scope, the reason this
    runs as a mapper-construction hook rather than inside `Scoped.__init_subclass__`, which fires
    before the table exists. A class with no `__rls_policies__` classmethod, every non-scoped
    table, is left untouched, since the declaration is opt-in per model and this hook never edits
    one.

    mapper: the mapper SQLAlchemy just finished constructing.
    class_: the mapped class the mapper belongs to.
    """
    declare = getattr(class_, "__rls_policies__", None)
    if declare is None:
        return
    local_table = mapper.local_table
    assert isinstance(local_table, Table), "a mapped class's local_table is always its own Table"
    TableBase.metadata.info.setdefault("rls_policies", {})[local_table.name] = declare()
    # every RLS-declaring table, `Scoped` claim tables and the content tables carrying only their
    # own custom policies alike, joins the one `rls` registry the autogenerate guard and
    # `require_tenant_context` both read, rather than `Scoped.__init_subclass__` remaining the only
    # path into it; a content table carries no owner_id/scope of its own to mix `Scoped` in for.
    TableBase.metadata.info.setdefault("rls", set()).add(local_table.name)
