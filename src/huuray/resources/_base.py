"""Shared machinery for the typed resources.

Every resource method maps 1:1 onto a single documented v4 operation. A method
with no corresponding path and verb in ``openapi/huuray-v4.json`` fails the
no-invention gate in ``tests/test_conformance.py``.

Each resource is written twice — a synchronous class and an ``Async`` twin — but
the parts that can be wrong are written once: an :class:`Operation` describes
exactly what goes on the wire, and a module-level mapping function turns the
response into result objects. The sync and async classes only differ in whether
they ``await``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:  # pragma: no cover - import cycle only exists for type checkers
    from ..client import AsyncHuurayClient, HuurayClient

#: Anything accepted where the specification declares a ``date-time`` string.
DateTimeLike = Union[datetime, date, str]


@dataclass(frozen=True)
class Operation:
    """One HTTP call, fully described before anything is sent.

    ``body`` of ``None`` means *no request body at all* — distinct from an empty
    object. ``POST /v4/Template`` declares no ``requestBody`` in the
    specification, so the SDK sends none.
    """

    method: str
    path: str
    body: Optional[dict[str, Any]] = None
    query: dict[str, str] = field(default_factory=dict)
    #: Whether repeating this call is safe. **Opt-in per operation** — never
    #: inferred from the HTTP method, because four read-only v4 endpoints are
    #: POSTs and two value-moving ones are too.
    retryable: bool = False


class Resource:
    """Base for the synchronous resources."""

    def __init__(self, client: HuurayClient) -> None:
        self._client = client


class AsyncResource:
    """Base for the asynchronous resources."""

    def __init__(self, client: AsyncHuurayClient) -> None:
        self._client = client


def compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` entries so they are never serialised into a request body."""
    return {key: value for key, value in values.items() if value is not None}


def to_datetime(value: DateTimeLike | None) -> str | None:
    """Format a value for the specification's ``date-time`` fields.

    ``datetime`` and ``date`` are rendered ISO-8601; strings pass through
    untouched, so a caller with a preformatted timestamp is never second-guessed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def require_minor_units(value: int, label: str = "value") -> int:
    """Reject an amount that is not an integer number of minor units.

    Money on this API is **minor units**: 50.00 is ``5000``. A float here almost
    always means major units were passed by mistake, which orders 1/100th of the
    intended amount, so it is rejected rather than rounded.

    ``True`` is an ``int`` in Python, so booleans are excluded explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{label} must be an int in minor units (50.00 is 5000), received {value!r}. "
            "A non-integer amount always means major units were passed by mistake — which "
            "would order 1/100th of the intended amount. Note that no guard can catch every "
            "mixup: a whole-number amount like 50 is a valid order for 0.50, so always write "
            "amounts as integers in minor units."
        )
    return value
