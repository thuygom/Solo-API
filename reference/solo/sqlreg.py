"""Fixed SQL registry.

A statement is reviewed text with an ID. Values are bound by name; SQL text, tables,
columns, filters and ordering never come from a request or from a model.
"""
import re
from types import MappingProxyType

_PARAM = re.compile(r"(?<![:\w]):([a-z_]+)")


def params(statement):
    """The parameter names a statement binds."""
    return frozenset(_PARAM.findall(statement))


def registry(statements):
    """Freeze {statement_id: sql}. A statement that is not scoped by :account_id is refused at import time."""
    for statement_id, sql in statements.items():
        if "account_id" not in params(sql):
            raise ValueError(f"{statement_id}: statement is not account-scoped")
    return MappingProxyType(dict(statements))
