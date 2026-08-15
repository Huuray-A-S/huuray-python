"""Error types.

The API returns ``Status`` and ``StatusMessage`` in the body alongside the HTTP
status. ``Message`` carries the same text but is marked deprecated in the
specification, so this client reads ``StatusMessage`` first and falls back to
``Message``.

Input guards — a fractional amount, a quantity over the synchronous limit, a
recipient count that is neither 1 nor ``quantity`` — raise the built-in
``ValueError`` before any request is sent. They are programming mistakes, not
API responses, and nothing in this hierarchy is raised for them.
"""

from __future__ import annotations

from typing import Any

from .redact import redact


class HuurayError(Exception):
    """Base class for everything this library raises. Catch it to catch them all."""


class HuurayConfigError(HuurayError):
    """Client is misconfigured — missing credentials, bad base URL. Not a server response."""


class HuurayConnectionError(HuurayError):
    """The request never completed: network failure, DNS, TLS, or timeout."""

    def __init__(self, message: str, method: str, path: str) -> None:
        super().__init__(message)
        self.method = method
        self.path = path


class HuurayTimeoutError(HuurayConnectionError):
    """The request exceeded the configured timeout."""

    def __init__(self, method: str, path: str, timeout: float) -> None:
        super().__init__(f"{method} {path} timed out after {timeout}s.", method, path)
        self.timeout = timeout


class HuurayAPIError(HuurayError):
    """The API returned a non-2xx response."""

    def __init__(
        self,
        message: str,
        http_status: int,
        status: int | None,
        status_message: str | None,
        body: Any,
        method: str,
        path: str,
    ) -> None:
        super().__init__(message)
        #: HTTP status of the response.
        self.http_status = http_status
        #: The ``Status`` field from the response body, when present.
        self.status = status
        #: The ``StatusMessage`` field, or the deprecated ``Message`` as fallback.
        self.status_message = status_message
        #: The parsed response body, if it was JSON — **redacted**: any field
        #: that could carry a voucher code or contact detail is masked, so
        #: logging an error object never leaks a bearer instrument.
        self.body = body
        self.method = method
        self.path = path

    @classmethod
    def from_response(
        cls,
        http_status: int,
        body: Any,
        method: str,
        path: str,
    ) -> HuurayAPIError:
        """Build the most specific error class for an HTTP status."""
        fields = body if isinstance(body, dict) else {}
        status_message = fields.get("StatusMessage") or fields.get("Message")
        status = fields.get("Status")
        detail = f" — {status_message}" if status_message else ""
        message = f"{method} {path} failed with HTTP {http_status}{detail}"

        # The raw body is dropped here: only the redacted copy is retained, so
        # an undocumented error payload carrying voucher or recipient fields
        # cannot ride into a consumer's logs via logging.exception().
        args = (
            message,
            http_status,
            status if isinstance(status, int) else None,
            status_message,
            redact(body),
            method,
            path,
        )

        if http_status in (401, 403):
            return HuurayAuthError(*args)
        if http_status == 404:
            return HuurayNotFoundError(*args)
        if http_status == 422:
            return HuurayValidationError(*args)
        if http_status >= 500:
            return HuurayServerError(*args)
        return HuurayAPIError(*args)


class HuurayAuthError(HuurayAPIError):
    """401 or 403.

    With credentials you believe are correct, the usual causes are, in order: a
    wrong ``X-API-HASH`` encoding (see ``hash_encoding``), a reused nonce (the
    API remembers them for 60 days), or a nonce over 50 characters.
    """


class HuurayNotFoundError(HuurayAPIError):
    """404 — the order, voucher, or product was not found.

    Also how the API signals an **empty result set**: ``POST /v4/Template`` on
    an account with no templates answers 404 ("There were no active templates")
    rather than an empty list, and ``POST /v4/Search`` with no match does the
    same. From ``orders.search()`` this means "the order did not land".
    """


class HuurayValidationError(HuurayAPIError):
    """422 — the request was well-formed but rejected. Read ``status_message``."""


class HuurayServerError(HuurayAPIError):
    """5xx — a server-side failure. Safe to retry only for reads."""


class HuurayIndeterminateOrderError(HuurayError):
    """An order request whose outcome is unknown.

    Raised when ``POST /v4/Order`` fails with a timeout, a dropped connection, a
    5xx, or a 2xx whose body could not be read — every case in which the request
    reached the API but the answer did not reach you.

    **Do not retry.** ``POST /v4/Order`` has no idempotency key, so a retry can
    order a second set of gift cards. The order may or may not have been created.

    Resolve it by looking the order up instead. Note that the API signals "no
    match" on ``/v4/Search`` as a ``404``, which this client raises as
    :class:`HuurayNotFoundError` — catch it and read it as "the order did not
    land"::

        try:
            huuray.send_reward(ref_id="payroll-2026-08-jane", ...)
        except HuurayIndeterminateOrderError as err:
            try:
                found = huuray.orders.search(ref_id=err.ref_id)
                if found.order_uid:
                    pass   # The order landed. Nothing more to do.
                else:
                    pass   # No match -> it did not land. Safe to send again.
            except HuurayNotFoundError:
                pass       # 404 -> no order exists for this ref_id. Safe to send again.
    """

    def __init__(self, ref_id: str | None) -> None:
        if ref_id:
            tail = f"Call orders.search(ref_id={ref_id!r}) to check whether it landed."
        else:
            tail = (
                "No RefID was sent, so the order cannot be looked up by reference. "
                "Always set ref_id on orders so this case is recoverable."
            )
        super().__init__(
            "The order request did not complete, so it is unknown whether the order was "
            "created. Do NOT retry: /v4/Order has no idempotency key and a retry may order "
            "a second time. " + tail
        )
        #: The ``RefID`` sent with the order, if any — the key to look it up with.
        self.ref_id = ref_id


def indeterminate_order_error(
    cause: BaseException,
    ref_id: str | None,
) -> HuurayIndeterminateOrderError | None:
    """Return the error to raise when an order call failed, or ``None`` to re-raise.

    A transport fault or a 5xx leaves the outcome unknown. A 4xx does not: the
    API definitively rejected that order, and masking it would send the caller
    into a reconciliation flow for an order that was never created.
    """
    if isinstance(cause, (HuurayConnectionError, HuurayServerError)):
        return HuurayIndeterminateOrderError(ref_id)
    return None
