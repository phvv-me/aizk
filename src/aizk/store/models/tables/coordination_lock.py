import rls
from patos import sql

from ...mixins import TableBase


class CoordinationLock(TableBase, table=True):
    """One durable key whose row lock serializes a portable critical section."""

    __rls__ = rls.Open()

    key = sql.Field(str, primary_key=True)
