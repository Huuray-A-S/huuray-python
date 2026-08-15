"""Keeping bearer instruments out of logs.

Voucher codes are bearer instruments: whoever holds the code holds the value.
They must never reach a log file, an error report, a CI fixture, or a bug report
pasted into a public issue.

Redaction is this library's job, not the caller's. Anything this SDK prints or
attaches to an error goes through here first.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import Any

#: Response fields that carry redeemable value and are never logged.
#:
#: Both the wire spelling (``Code``) and the mapped Python spelling (``code``,
#: ``redeem_link``) are listed, so a redacted result object is as safe as a
#: redacted response body.
SECRET_FIELDS: tuple[str, ...] = (
    "Code",
    "CVV",
    "RedeemLink",
    "code",
    "cvv",
    "redeemLink",
    "redeem_link",
)

#: Fields carrying credentials or personal data, masked in any diagnostic output.
SENSITIVE_FIELDS: tuple[str, ...] = (
    "X-API-TOKEN",
    "X-API-HASH",
    "apiToken",
    "apiSecret",
    "api_token",
    "api_secret",
    "Email",
    "email",
    "Phone",
    "phone",
)

_SECRET = frozenset(SECRET_FIELDS)
_SENSITIVE = frozenset(SENSITIVE_FIELDS)

#: Replacement for a value that could be redeemed for money.
BEARER_MARKER = "[redacted: bearer value]"

_MAX_DEPTH = 12


def _mask_partial(value: str) -> str:
    """Keep just enough of a value to recognise it, never enough to use it."""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def redact(value: Any, depth: int = 0) -> Any:
    """Return a deep copy with secret and sensitive values replaced by markers.

    Handles mappings, sequences, and dataclass instances — so both raw response
    bodies and the mapped result objects this SDK returns are covered.

    Use it for anything human-facing. It is deliberately lossy: a redacted
    voucher code cannot be recovered from the output.

    >>> redact({"Vouchers": [{"ID": 1, "Code": "REAL-CODE"}]})
    {'Vouchers': [{'ID': 1, 'Code': '[redacted: bearer value]'}]}
    """
    if depth > _MAX_DEPTH:
        return "[redacted: too deep]"

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if key in _SECRET:
                out[key] = item if item is None or item == "" else BEARER_MARKER
            elif key in _SENSITIVE:
                out[key] = item if item is None or item == "" else _mask_partial(str(item))
            else:
                out[key] = redact(item, depth + 1)
        return out

    if isinstance(value, (str, bytes)):
        return value

    if isinstance(value, Sequence):
        return [redact(item, depth + 1) for item in value]

    return value


def safe_json(value: Any, indent: int | None = None) -> str:
    """``json.dumps`` with redaction applied. Safe to log."""
    return json.dumps(redact(value), indent=indent, default=str)
