"""Final-result validation against a client-supplied JSON Schema.

A merge order may carry an optional ``result_schema`` (a JSON Schema
following [Draft 2020-12](https://json-schema.org/draft/2020-12)). It acts
as a gate on the immutable merge result:

  * The schema itself is validated against the Draft 2020-12 meta-schema
    when the merge order is created; an invalid schema yields 422 and no
    record is created.
  * A merge with no open conflicts is resolved immediately, so its merged
    document must pass the schema at creation time (or the request fails
    with 422 and nothing is stored).
  * Resolution batches (explicit resolutions and rule resolutions) are
    only checked when the batch resolves the *last* open conflict: the
    candidate final document must pass. A partial batch leaves conflict
    placeholders in the document and is never checked. A failing final
    batch lists every violation and writes nothing.

Everything here is pure and in-memory; the caller decides whether an
outcome is persisted.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft202012Validator
from referencing.exceptions import Unresolvable


class InvalidResultSchema(Exception):
    """The supplied result schema is not a valid Draft 2020-12 schema."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"invalid result schema: {len(errors)} error(s)")


class ResultValidationFailed(Exception):
    """The candidate final document violates the merge's result schema."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"result schema validation failed: {len(errors)} error(s)")


def _to_pointer(parts) -> str:
    """Serialize a deque/tuple of path parts as an RFC 6901 JSON Pointer."""
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def _error_entry(error, instance_path: Any | None = None) -> dict:
    return {
        "instance_path": _to_pointer(
            error.absolute_path if instance_path is None else instance_path
        ),
        "schema_path": _to_pointer(error.absolute_schema_path),
        "message": error.message,
    }


def _sort_errors(errors: list[dict]) -> list[dict]:
    # Stable, total order: the instance location first, then the violated
    # schema keyword location, then the human-readable message.
    return sorted(
        errors,
        key=lambda e: (e["instance_path"], e["schema_path"], e["message"]),
    )


def check_result_schema(schema: Any) -> None:
    """Validate ``schema`` against the Draft 2020-12 meta-schema.

    Raises InvalidResultSchema listing every meta-schema violation.
    """
    errors: list[dict] = []
    metavalidator = Draft202012Validator(
        Draft202012Validator.META_SCHEMA,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    try:
        meta_errors = list(metavalidator.iter_errors(schema))
    except (SchemaError, re.error) as exc:
        # A malformed regex or similar construct can surface while the
        # meta-validator itself evaluates the candidate schema.
        raise InvalidResultSchema(
            _sort_errors(
                [
                    {
                        "instance_path": "",
                        "schema_path": "",
                        "message": str(exc),
                    }
                ]
            )
        ) from exc
    errors.extend(_error_entry(error) for error in meta_errors)
    if errors:
        raise InvalidResultSchema(_sort_errors(errors))


def validate_result(schema: Any, document: Any) -> None:
    """Validate a candidate final result document against ``schema``.

    Raises ResultValidationFailed listing every violation, sorted by
    (instance_path, schema_path, message). A schema that only fails when
    applied (e.g. an unresolvable ``$ref``) is reported here as a failure
    of the candidate result rather than crashing the request.
    """
    validator = Draft202012Validator(schema)
    errors: list[dict] = []
    try:
        violations = list(validator.iter_errors(document))
    except Unresolvable as exc:
        # An unresolvable $ref anywhere in the schema makes the candidate
        # document unvalidatable: report it as a root-level violation.
        raise ResultValidationFailed(
            [
                {
                    "instance_path": "",
                    "schema_path": "",
                    "message": f"cannot resolve $ref while validating result: {exc}",
                }
            ]
        ) from exc
    except (SchemaError, re.error) as exc:
        raise ResultValidationFailed(
            [
                {
                    "instance_path": "",
                    "schema_path": "",
                    "message": f"invalid schema encountered while validating result: {exc}",
                }
            ]
        ) from exc
    errors.extend(_error_entry(error) for error in violations)
    if errors:
        raise ResultValidationFailed(_sort_errors(errors))
