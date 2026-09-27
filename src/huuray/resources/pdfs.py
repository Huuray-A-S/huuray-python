"""``POST /v4/Pdf`` — the gift card PDFs of a previous order."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from ..errors import HuurayTimeoutError
from ._base import AsyncResource, Operation, Resource, compact

if TYPE_CHECKING:  # pragma: no cover - import cycle only exists for type checkers
    from ..client import RawResponse

#: How long ``get_when_ready()`` keeps asking by default, in seconds: 10 minutes.
DEFAULT_MAX_WAIT = 600.0

#: The wait after a 202 without a usable ``Retry-After``, in seconds.
DEFAULT_RETRY_AFTER = 30

#: The shortest wait between two requests, in seconds: a ``Retry-After: 0``
#: from a server or proxy never turns into back-to-back requests.
MIN_WAIT = 1

#: ``Retry-After`` in whole seconds. Anything else, the HTTP-date form included,
#: reads as absent.
_DELTA_SECONDS = re.compile(r"[0-9]+")

# The clock and the sleeps get_when_ready() waits with. They are looked up on
# every call, so a test replaces them instead of waiting, as it does the backoff.
_monotonic = time.monotonic
_sleep = time.sleep
_async_sleep = asyncio.sleep


@dataclass(frozen=True, repr=False)
class PdfDocument:
    """One gift card PDF.

    ``content`` is a **bearer instrument**: the PDF shows the redeemable code,
    and depending on the template the CVV and QR codes, so whoever holds the
    file holds the value. ``repr()`` and ``redact()`` show its size, never the
    bytes. Never log it, and keep it no longer than you need it.
    """

    #: The vouchers in the document: one, or every selected one when combined.
    voucher_ids: list[int]
    #: The PDF template the document was built from; ``None`` for a combined
    #: document built from several templates.
    pdf_template_uid: Optional[str]
    #: A suggested file name, e.g. ``giftcard-5123401.pdf``.
    file_name: Optional[str]
    #: ``application/pdf``.
    content_type: Optional[str]
    #: The PDF, decoded from the base64 the API sends.
    content: Optional[bytes]

    def __repr__(self) -> str:
        content = "None" if self.content is None else f"[{len(self.content)} bytes]"
        return (
            f"PdfDocument(voucher_ids={self.voucher_ids!r}, "
            f"pdf_template_uid={self.pdf_template_uid!r}, file_name={self.file_name!r}, "
            f"content_type={self.content_type!r}, content={content})"
        )


@dataclass(frozen=True)
class PdfResult:
    """The gift card PDFs of an order, or word that they are not ready yet."""

    #: ``True`` when the API answered 200 with the documents. ``False`` on a
    #: 202: the order is still in Huuray's queue, or a supplier has not
    #: delivered a code yet. Ask again after ``retry_after`` seconds.
    ready: bool
    order_uid: Optional[str]
    #: One document per voucher, or a single one when combined. Empty when not
    #: ready.
    documents: list[PdfDocument] = field(default_factory=list)
    #: The ``Retry-After`` header in whole seconds, or ``None`` when it is absent
    #: or not a whole number of seconds.
    retry_after: Optional[int] = None


@dataclass(frozen=True)
class _Body:
    """A 2xx body, every document's content decoded."""

    order_uid: Optional[str]
    documents: list[PdfDocument]
    #: ``StatusMessage``, or the deprecated ``Message``: why a PDF is not ready.
    status_message: Optional[str]


def _operation(
    order_uid: str,
    voucher_id: Optional[int],
    pdf_template_uid: Optional[str],
    combine: Optional[bool],
) -> Operation:
    return Operation(
        method="POST",
        path="/v4/Pdf",
        # Sent exactly as given. The API, not this client, checks the UIDs and
        # that the order has at most 3 receivers.
        body=compact(
            {
                "OrderUID": order_uid,
                "VoucherID": voucher_id,
                "PDFTemplateUid": pdf_template_uid,
                "Combine": combine,
            }
        ),
        # A read, despite being a POST: it never changes the order.
        retryable=True,
        decode=_decode,
    )


def _voucher_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("a document's VoucherIDs is not a list")
    return value


def _content(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("a document's Content is not a string")
    try:
        # validate=True: a character outside the base64 alphabet is an error,
        # not silently skipped into a corrupt PDF.
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("a document's Content is not valid base64") from None


def _decode(data: Any) -> _Body:
    """Read a 2xx body, decoding each document's base64 ``Content`` to bytes.

    Raises ``ValueError`` when the documents cannot be read, which makes the
    body unreadable. No message quotes the body: the content is a bearer
    instrument.
    """
    payload = {} if data is None else data
    if not isinstance(payload, dict):
        raise ValueError("it is not a JSON object")
    documents = payload.get("Documents") or []
    if not isinstance(documents, list) or not all(isinstance(d, dict) for d in documents):
        raise ValueError("Documents is not a list of objects")
    return _Body(
        order_uid=payload.get("OrderUID"),
        documents=[
            PdfDocument(
                voucher_ids=_voucher_ids(document.get("VoucherIDs")),
                pdf_template_uid=document.get("PDFTemplateUid"),
                file_name=document.get("FileName"),
                content_type=document.get("ContentType"),
                content=_content(document.get("Content")),
            )
            for document in documents
        ],
        status_message=payload.get("StatusMessage") or payload.get("Message"),
    )


def _retry_after(value: Optional[str]) -> Optional[int]:
    """``Retry-After`` as whole seconds, or ``None`` when absent or anything else."""
    text = "" if value is None else value.strip()
    if not _DELTA_SECONDS.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:  # more digits than int() converts
        return None


def _result(response: RawResponse[Any]) -> PdfResult:
    body: _Body = response.data
    return PdfResult(
        ready=response.http_status == 200,
        order_uid=body.order_uid,
        documents=body.documents,
        retry_after=_retry_after(response.headers.get("Retry-After")),
    )


def _deadline(max_wait: float) -> float:
    """When ``get_when_ready()`` stops asking, on the monotonic clock."""
    # A bool is an int in Python, and NaN or infinity would never give up.
    if (
        isinstance(max_wait, bool)
        or not isinstance(max_wait, (int, float))
        or not 0 <= max_wait < math.inf
    ):
        raise ValueError(
            f"max_wait must be a finite number of seconds, 0 or more, received {max_wait!r}."
        )
    return _monotonic() + max_wait


def _wait(
    op: Operation,
    response: RawResponse[Any],
    result: PdfResult,
    deadline: float,
    max_wait: float,
) -> int:
    """Seconds to wait before asking again. Raises if that would pass ``max_wait``."""
    wait = DEFAULT_RETRY_AFTER if result.retry_after is None else max(MIN_WAIT, result.retry_after)
    # Compared with the time left, never as now + wait: a Retry-After too large
    # for a float would raise OverflowError there, where it must give up at once.
    if wait > deadline - _monotonic():
        status_message = response.data.status_message
        last = f" Last status: {status_message}" if status_message else ""
        raise HuurayTimeoutError(
            op.method,
            op.path,
            max_wait,
            f"The gift card PDF was still not ready, and waiting {wait}s more would pass "
            f"max_wait.{last}",
        )
    return wait


class PdfsResource(Resource):
    def get(
        self,
        *,
        order_uid: str,
        voucher_id: Optional[int] = None,
        pdf_template_uid: Optional[str] = None,
        combine: Optional[bool] = None,
    ) -> PdfResult:
        """Fetch the gift card PDFs of an order, or learn that they are not ready.

        ``POST /v4/Pdf`` — one request. Your API token needs the **Search**
        permission.

        Check ``ready``: a 202 is not success. It means the order is still being
        processed, or a supplier has not delivered a code yet, and
        ``retry_after`` says when to ask again. :meth:`get_when_ready` does
        that for you.

        **The PDF is a bearer instrument**: it shows the redeemable code. Never
        log ``content``, and keep it no longer than you need it.

        Retried like any read, on connection failures and 5xx: the call never
        changes the order. The API supports orders with at most 3 receivers and
        answers others with a 422, raised as ``HuurayValidationError``; an
        unknown order or voucher is a 404, raised as ``HuurayNotFoundError``.
        This client checks neither.

        A PDF can take a while to render and run to several MB, so build the
        client with a longer ``timeout`` for these calls, e.g. ``timeout=100.0``.

        :param order_uid: The order's ``order_uid``.
        :param voucher_id: One voucher of the order. Omit for all of them.
        :param pdf_template_uid: A ``uid`` from ``templates.list().pdf_templates``.
            Omit for the PDF template the order's delivery email was sent with.
        :param combine: ``True`` for one PDF holding every selected voucher.
            Omitted, the API returns one PDF per voucher.
        """
        op = _operation(order_uid, voucher_id, pdf_template_uid, combine)
        return _result(self._client._send(op))

    def get_when_ready(
        self,
        *,
        order_uid: str,
        voucher_id: Optional[int] = None,
        pdf_template_uid: Optional[str] = None,
        combine: Optional[bool] = None,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> PdfResult:
        """Like :meth:`get`, but asks again on a 202 until the PDFs are ready.

        ``POST /v4/Pdf``, repeated: after each 202 it waits ``Retry-After``
        seconds, at least 1, or 30 without one, and every request is signed
        with a new nonce. An error is raised at once, not waited out.

        :param max_wait: Seconds to keep asking, 10 minutes by default. When the
            next wait would pass it, :class:`~huuray.HuurayTimeoutError` is
            raised, with the last status the API gave.
        """
        deadline = _deadline(max_wait)
        op = _operation(order_uid, voucher_id, pdf_template_uid, combine)
        while True:
            response = self._client._send(op)
            result = _result(response)
            if result.ready:
                return result
            _sleep(_wait(op, response, result, deadline, max_wait))


class AsyncPdfsResource(AsyncResource):
    async def get(
        self,
        *,
        order_uid: str,
        voucher_id: Optional[int] = None,
        pdf_template_uid: Optional[str] = None,
        combine: Optional[bool] = None,
    ) -> PdfResult:
        """Fetch the gift card PDFs of an order. See :meth:`PdfsResource.get`.

        ``POST /v4/Pdf``
        """
        op = _operation(order_uid, voucher_id, pdf_template_uid, combine)
        return _result(await self._client._send(op))

    async def get_when_ready(
        self,
        *,
        order_uid: str,
        voucher_id: Optional[int] = None,
        pdf_template_uid: Optional[str] = None,
        combine: Optional[bool] = None,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> PdfResult:
        """Ask again on a 202 until the PDFs are ready. See :meth:`PdfsResource.get_when_ready`.

        ``POST /v4/Pdf``, repeated. The waits are awaited, never blocking the
        event loop.
        """
        deadline = _deadline(max_wait)
        op = _operation(order_uid, voucher_id, pdf_template_uid, combine)
        while True:
            response = await self._client._send(op)
            result = _result(response)
            if result.ready:
                return result
            await _async_sleep(_wait(op, response, result, deadline, max_wait))
