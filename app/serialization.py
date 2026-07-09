"""Helpers for turning DynamoDB items into JSON-serializable data.

DynamoDB's resource API returns numbers as `Decimal`, which FastAPI's JSON
encoder rejects. `to_jsonable` walks an item and converts Decimals to int (when
integral) or float, recursively through lists/dicts.
"""
from __future__ import annotations

import decimal
from typing import Any


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        as_int = int(obj)
        return as_int if as_int == obj else float(obj)
    return obj
