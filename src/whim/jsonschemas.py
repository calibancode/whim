"""JSON Schema validation for untrusted (plugin-supplied) schemas.

Only references within the same schema document (`#...`) are supported. Validators use
a reference registry that cannot retrieve anything, so no schema can make the host read
a file or open a network connection; plugin schemas with external references are
rejected before they are ever used.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")


def _no_retrieval(uri: str):
    raise NoSuchResource(ref=uri)


REGISTRY = Registry(retrieve=_no_retrieval)


def validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=REGISTRY)


def external_references(schema: Any) -> list[str]:
    """References that would leave this document, and any `$id` (which re-bases them)."""
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k in REF_KEYS and (not isinstance(x, str) or not x.startswith("#")):
                    found.append(f"{k}: {x!r}"[:200])
                elif k == "$id":
                    found.append(f"$id: {x!r}"[:200])
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(schema)
    return found
