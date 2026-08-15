"""Ordering, searching, resending and cancelling gift cards.

Everything in this module moves real value, so it is where the safety rules
live: no automatic retries, integers only for money, and an explicit
"outcome unknown" error rather than a silent second order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from ..errors import HuurayError, indeterminate_order_error
from ..redact import BEARER_MARKER
from ._base import (
    AsyncResource,
    DateTimeLike,
    Operation,
    Resource,
    compact,
    require_minor_units,
    to_datetime,
)

#: The maximum ``quantity`` a synchronous order may request, per the API.
SYNC_QUANTITY_LIMIT = 25


@dataclass(frozen=True)
class Recipient:
    """One recipient — used both when ordering and when reading results back."""

    name: Optional[str] = None
    #: Required when delivering by email.
    email: Optional[str] = None
    #: Required when delivering by SMS.
    phone: Optional[str] = None
    #: Your own identifier for this recipient.
    ref_id: Optional[str] = None


@dataclass(frozen=True, repr=False)
class Voucher:
    """One gift card.

    ``code``, ``cvv`` and ``redeem_link`` are **bearer instruments**: whoever
    holds them holds the value. They are masked in ``repr()``, so a voucher that
    reaches a log line, a traceback, or a debugger transcript never carries a
    redeemable code with it. Read the attributes directly to use them.
    """

    #: Voucher identifier, used by ``resend()`` and ``cancel()``.
    id: Optional[int]
    #: The redeemable code.
    #:
    #: **Blank unless ``ReturnCode`` is enabled on your B2B account.** If you
    #: need codes returned to your system rather than delivered by Huuray, ask
    #: your Huuray contact to enable it.
    code: Optional[str]
    cvv: Optional[str]
    redeem_link: Optional[str]
    expires: Optional[str]
    recipient: Optional[Recipient]

    def __repr__(self) -> str:
        def hide(value: Optional[str]) -> str:
            return repr(value) if not value else BEARER_MARKER

        return (
            f"Voucher(id={self.id!r}, code={hide(self.code)}, cvv={hide(self.cvv)}, "
            f"redeem_link={hide(self.redeem_link)}, expires={self.expires!r}, "
            f"recipient={self.recipient!r})"
        )


@dataclass(frozen=True)
class CreateOrderResult:
    """Result of an asynchronous order. No voucher data is returned."""

    order_uid: Optional[str]
    ref_id: Optional[str]


@dataclass(frozen=True)
class CreateSyncOrderResult:
    """Result of a synchronous order. Vouchers are returned inline."""

    order_uid: Optional[str]
    ref_id: Optional[str]
    vouchers: list[Voucher] = field(default_factory=list)


@dataclass(frozen=True)
class SearchOrdersResult:
    order_uid: Optional[str]
    ref_id: Optional[str]
    vouchers: list[Voucher] = field(default_factory=list)


@dataclass(frozen=True)
class ResendResult:
    number_of_resends: Optional[int]
    #: ``True`` when the API answered ``206 Partial Content`` — some resends
    #: succeeded and some did not. Treating this as plain success is a common bug.
    partial: bool


@dataclass(frozen=True)
class CancelledVoucher:
    id: int
    cancelled: bool


@dataclass(frozen=True)
class CancelResult:
    order_uid: Optional[str]
    order_cancelled: bool
    vouchers: list[CancelledVoucher] = field(default_factory=list)
    #: ``True`` when the API answered ``206 Partial Content`` — inspect
    #: ``vouchers`` to see which ones were not cancelled.
    partial: bool = False


# --------------------------------------------------------------- wire mapping


def _map_recipient(payload: Any) -> Optional[Recipient]:
    if not payload:
        return None
    return Recipient(
        name=payload.get("Name"),
        email=payload.get("Email"),
        phone=payload.get("Phone"),
        ref_id=payload.get("RefID"),
    )


def _map_voucher(payload: Any) -> Voucher:
    return Voucher(
        id=payload.get("ID"),
        code=payload.get("Code"),
        cvv=payload.get("CVV"),
        redeem_link=payload.get("RedeemLink"),
        expires=payload.get("Expires"),
        recipient=_map_recipient(payload.get("Recipient")),
    )


def _to_wire_recipient(recipient: Recipient) -> dict[str, Any]:
    return compact(
        {
            "Name": recipient.name,
            "Email": recipient.email,
            "Phone": recipient.phone,
            "RefID": recipient.ref_id,
        }
    )


# ------------------------------------------------------------ request bodies


def _order_operation(
    *,
    product_token: str,
    value: int,
    currency: str,
    quantity: int,
    sync: bool,
    expires: Optional[DateTimeLike],
    ref_id: Optional[str],
    template_id: Optional[int],
    delivery_datetime: Optional[DateTimeLike],
    personal_message: Optional[str],
    recipients: Optional[Sequence[Recipient]],
) -> Operation:
    """Validate an order and describe the single request it becomes."""
    require_minor_units(value)

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError(f"quantity must be a positive int, received {quantity!r}.")

    if sync and quantity > SYNC_QUANTITY_LIMIT:
        raise ValueError(
            f"Synchronous orders are limited to {SYNC_QUANTITY_LIMIT} codes; "
            f"received {quantity}. Use orders.create() for larger orders."
        )

    if template_id is not None:
        count = len(recipients) if recipients is not None else 0
        if count == 0:
            raise ValueError(
                "recipients is required when template_id is set — the template needs "
                "somewhere to deliver to."
            )
        if count != 1 and count != quantity:
            raise ValueError(
                f"recipients must contain either 1 entry or exactly quantity "
                f"({quantity}); received {count}."
            )

    body = compact(
        {
            "Product": compact(
                {
                    "Token": product_token,
                    "Value": value,
                    "Currency": currency,
                    "Quantity": quantity,
                    "Expires": to_datetime(expires),
                }
            ),
            "Sync": sync,
            "RefID": ref_id,
            "DeliveryTemplateId": template_id,
            "DeliveryDatetime": to_datetime(delivery_datetime),
            "PersonalMessage": personal_message,
            "Recipients": (
                [_to_wire_recipient(r) for r in recipients] if recipients is not None else None
            ),
        }
    )
    # retryable is False and must stay False: /v4/Order has no idempotency key.
    return Operation(method="POST", path="/v4/Order", body=body, retryable=False)


def _search_operation(
    *,
    order_uid: Optional[str],
    voucher_id: Optional[int],
    product_token: Optional[str],
    ref_id: Optional[str],
    sms_template_id: Optional[int],
    email_template_id: Optional[int],
    delivery_datetime: Optional[DateTimeLike],
    recipient_name: Optional[str],
    recipient_email: Optional[str],
    recipient_phone: Optional[str],
    recipient_ref_id: Optional[str],
) -> Operation:
    return Operation(
        method="POST",
        path="/v4/Search",
        body=compact(
            {
                "OrderUID": order_uid,
                "VoucherID": voucher_id,
                "ProductToken": product_token,
                "RefID": ref_id,
                "SMSTemplateID": sms_template_id,
                "EmailTemplateID": email_template_id,
                "DeliveryDatetime": to_datetime(delivery_datetime),
                "RecipientName": recipient_name,
                "RecipientEmail": recipient_email,
                "RecipientPhone": recipient_phone,
                "RecipientRefID": recipient_ref_id,
            }
        ),
        retryable=True,
    )


def _resend_operation(order_uid: str, voucher_id: Optional[int]) -> Operation:
    return Operation(
        method="POST",
        path="/v4/Resend",
        body=compact({"OrderUID": order_uid, "VoucherID": voucher_id}),
        retryable=False,
    )


def _cancel_operation(order_uid: str, voucher_id: Optional[int]) -> Operation:
    return Operation(
        method="DELETE",
        path="/v4/Cancel",
        body=compact({"OrderUID": order_uid, "VoucherID": voucher_id}),
        retryable=False,
    )


def _require_ref_id(ref_id: str) -> str:
    if not ref_id:
        raise ValueError(
            "ref_id is required by send_reward(). It is the only way to determine whether "
            "an order landed if the request times out, because /v4/Order has no idempotency "
            'key. Use a stable key from your own system, e.g. "payroll-2026-08-jane".'
        )
    return ref_id


# ------------------------------------------------------------ result mapping


def _map_create(data: Any) -> CreateOrderResult:
    payload = data or {}
    return CreateOrderResult(order_uid=payload.get("OrderUID"), ref_id=payload.get("RefID"))


def _map_create_sync(data: Any) -> CreateSyncOrderResult:
    payload = data or {}
    return CreateSyncOrderResult(
        order_uid=payload.get("OrderUID"),
        ref_id=payload.get("RefID"),
        vouchers=[_map_voucher(v) for v in (payload.get("Vouchers") or [])],
    )


def _map_search(data: Any) -> SearchOrdersResult:
    payload = data or {}
    return SearchOrdersResult(
        order_uid=payload.get("OrderUID"),
        ref_id=payload.get("RefID"),
        vouchers=[_map_voucher(v) for v in (payload.get("Vouchers") or [])],
    )


def _map_resend(data: Any, http_status: int) -> ResendResult:
    return ResendResult(
        number_of_resends=(data or {}).get("NumberOfResends"),
        partial=http_status == 206,
    )


def _map_cancel(data: Any, http_status: int) -> CancelResult:
    payload = data or {}
    return CancelResult(
        order_uid=payload.get("OrderUID"),
        order_cancelled=bool(payload.get("OrderCancelled")),
        vouchers=[
            CancelledVoucher(id=v.get("ID") or 0, cancelled=bool(v.get("Cancelled")))
            for v in (payload.get("Vouchers") or [])
        ],
        partial=http_status == 206,
    )


# ------------------------------------------------------------------ resource


class OrdersResource(Resource):
    """Synchronous ordering, searching, resending and cancelling."""

    def create(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        quantity: int,
        expires: Optional[DateTimeLike] = None,
        ref_id: Optional[str] = None,
        template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
        recipients: Optional[Sequence[Recipient]] = None,
    ) -> CreateOrderResult:
        """Place an order and return immediately.

        ``POST /v4/Order`` with ``Sync: False``

        Huuray delivers the gift cards using the template you name; no voucher
        data comes back. Use ``search()`` with your ``ref_id`` to find the order
        later.

        **Not retried on failure.** The endpoint has no idempotency key, so a
        retry can order twice. A timeout, a dropped connection, a 5xx, or an
        unreadable 2xx body raises
        :class:`~huuray.HuurayIndeterminateOrderError` instead.

        :param value: Denomination **in minor units** — 50.00 is ``5000``.
        :param expires: Optional expiry for the gift cards. Cannot exceed the
            product default.
        :param ref_id: Your own identifier for this order. Strongly recommended:
            it is what makes an order recoverable after a timeout.
        :param template_id: Delivery template id from ``templates.list()``.
            Omit for no delivery.
        :param recipients: Required when ``template_id`` is set. The count must
            be either 1 or exactly ``quantity``.
        """
        op = _order_operation(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=quantity,
            sync=False,
            expires=expires,
            ref_id=ref_id,
            template_id=template_id,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
            recipients=recipients,
        )
        return _map_create(self._post_order(op, ref_id))

    def create_sync(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        quantity: int,
        expires: Optional[DateTimeLike] = None,
        ref_id: Optional[str] = None,
        template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
        recipients: Optional[Sequence[Recipient]] = None,
    ) -> CreateSyncOrderResult:
        """Place an order and wait for the vouchers.

        ``POST /v4/Order`` with ``Sync: True``

        ``quantity`` is limited to :data:`SYNC_QUANTITY_LIMIT` for synchronous
        orders. Voucher codes are blank unless ``ReturnCode`` is enabled on your
        account.

        **Not retried on failure**, the same as :meth:`create`.
        """
        op = _order_operation(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=quantity,
            sync=True,
            expires=expires,
            ref_id=ref_id,
            template_id=template_id,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
            recipients=recipients,
        )
        return _map_create_sync(self._post_order(op, ref_id))

    def send_reward(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        recipient: Recipient,
        template_id: int,
        ref_id: str,
        expires: Optional[DateTimeLike] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
    ) -> CreateOrderResult:
        """Send one gift card to one recipient — the common case, in one call.

        Performs exactly one ``POST /v4/Order`` with ``Sync: False`` and
        ``Quantity: 1``.

        ``ref_id`` is required here even though the API treats it as optional,
        and is never generated for you: a generated key is not in your system,
        so it could not be used to reconcile an order whose outcome is unknown.
        """
        return self.create(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=1,
            ref_id=_require_ref_id(ref_id),
            template_id=template_id,
            recipients=[recipient],
            expires=expires,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
        )

    def search(
        self,
        *,
        order_uid: Optional[str] = None,
        voucher_id: Optional[int] = None,
        product_token: Optional[str] = None,
        ref_id: Optional[str] = None,
        sms_template_id: Optional[int] = None,
        email_template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        recipient_name: Optional[str] = None,
        recipient_email: Optional[str] = None,
        recipient_phone: Optional[str] = None,
        recipient_ref_id: Optional[str] = None,
    ) -> SearchOrdersResult:
        """Search gift cards from previous orders.

        ``POST /v4/Search``

        Also the way to resolve an order whose outcome is unknown: search by the
        ``ref_id`` you sent. A read, despite being a POST.

        Note that "no match" comes back as HTTP 404, raised as
        :class:`~huuray.HuurayNotFoundError` — from a reconciliation flow, read
        it as "the order did not land".

        :param voucher_id: Required for the response to include the voucher code.
        """
        op = _search_operation(
            order_uid=order_uid,
            voucher_id=voucher_id,
            product_token=product_token,
            ref_id=ref_id,
            sms_template_id=sms_template_id,
            email_template_id=email_template_id,
            delivery_datetime=delivery_datetime,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
            recipient_ref_id=recipient_ref_id,
        )
        return _map_search(self._client._send(op).data)

    def resend(self, *, order_uid: str, voucher_id: Optional[int] = None) -> ResendResult:
        """Resend an order, or one voucher from it, to its original recipients.

        ``POST /v4/Resend``

        **Never retried.** A resend delivers a live gift card, so repeating it
        on a timeout would re-send real value.

        Check ``partial``: the API answers ``206`` when only some resends
        succeeded.

        :param voucher_id: A single voucher. Omit to resend the whole order to
            all its recipients.
        """
        response = self._client._send(_resend_operation(order_uid, voucher_id))
        return _map_resend(response.data, response.http_status)

    def cancel(self, *, order_uid: str, voucher_id: Optional[int] = None) -> CancelResult:
        """Cancel an order, or one voucher from it.

        ``DELETE /v4/Cancel`` — a DELETE carrying a JSON body, as the
        specification defines it.

        Check ``partial``: the API answers ``206`` when only some vouchers could
        be cancelled, and the per-voucher outcome is in ``vouchers``.

        :param voucher_id: A single voucher. Omit to attempt cancelling the
            whole order.
        """
        response = self._client._send(_cancel_operation(order_uid, voucher_id))
        return _map_cancel(response.data, response.http_status)

    def _post_order(self, op: Operation, ref_id: Optional[str]) -> Any:
        try:
            return self._client._send(op).data
        except HuurayError as exc:
            # The request may well have been processed. Never retry; make the
            # caller reconcile instead.
            indeterminate = indeterminate_order_error(exc, ref_id)
            if indeterminate is not None:
                raise indeterminate from exc
            raise


class AsyncOrdersResource(AsyncResource):
    """Asynchronous ordering, searching, resending and cancelling."""

    async def create(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        quantity: int,
        expires: Optional[DateTimeLike] = None,
        ref_id: Optional[str] = None,
        template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
        recipients: Optional[Sequence[Recipient]] = None,
    ) -> CreateOrderResult:
        """Place an order and return immediately. See :meth:`OrdersResource.create`."""
        op = _order_operation(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=quantity,
            sync=False,
            expires=expires,
            ref_id=ref_id,
            template_id=template_id,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
            recipients=recipients,
        )
        return _map_create(await self._post_order(op, ref_id))

    async def create_sync(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        quantity: int,
        expires: Optional[DateTimeLike] = None,
        ref_id: Optional[str] = None,
        template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
        recipients: Optional[Sequence[Recipient]] = None,
    ) -> CreateSyncOrderResult:
        """Place an order and wait for the vouchers. See :meth:`OrdersResource.create_sync`."""
        op = _order_operation(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=quantity,
            sync=True,
            expires=expires,
            ref_id=ref_id,
            template_id=template_id,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
            recipients=recipients,
        )
        return _map_create_sync(await self._post_order(op, ref_id))

    async def send_reward(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        recipient: Recipient,
        template_id: int,
        ref_id: str,
        expires: Optional[DateTimeLike] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        personal_message: Optional[str] = None,
    ) -> CreateOrderResult:
        """Send one gift card to one recipient. See :meth:`OrdersResource.send_reward`."""
        return await self.create(
            product_token=product_token,
            value=value,
            currency=currency,
            quantity=1,
            ref_id=_require_ref_id(ref_id),
            template_id=template_id,
            recipients=[recipient],
            expires=expires,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
        )

    async def search(
        self,
        *,
        order_uid: Optional[str] = None,
        voucher_id: Optional[int] = None,
        product_token: Optional[str] = None,
        ref_id: Optional[str] = None,
        sms_template_id: Optional[int] = None,
        email_template_id: Optional[int] = None,
        delivery_datetime: Optional[DateTimeLike] = None,
        recipient_name: Optional[str] = None,
        recipient_email: Optional[str] = None,
        recipient_phone: Optional[str] = None,
        recipient_ref_id: Optional[str] = None,
    ) -> SearchOrdersResult:
        """Search gift cards from previous orders. See :meth:`OrdersResource.search`."""
        op = _search_operation(
            order_uid=order_uid,
            voucher_id=voucher_id,
            product_token=product_token,
            ref_id=ref_id,
            sms_template_id=sms_template_id,
            email_template_id=email_template_id,
            delivery_datetime=delivery_datetime,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
            recipient_ref_id=recipient_ref_id,
        )
        return _map_search((await self._client._send(op)).data)

    async def resend(self, *, order_uid: str, voucher_id: Optional[int] = None) -> ResendResult:
        """Resend an order, or one voucher from it. See :meth:`OrdersResource.resend`."""
        response = await self._client._send(_resend_operation(order_uid, voucher_id))
        return _map_resend(response.data, response.http_status)

    async def cancel(self, *, order_uid: str, voucher_id: Optional[int] = None) -> CancelResult:
        """Cancel an order, or one voucher from it. See :meth:`OrdersResource.cancel`."""
        response = await self._client._send(_cancel_operation(order_uid, voucher_id))
        return _map_cancel(response.data, response.http_status)

    async def _post_order(self, op: Operation, ref_id: Optional[str]) -> Any:
        try:
            return (await self._client._send(op)).data
        except HuurayError as exc:
            # The request may well have been processed. Never retry; make the
            # caller reconcile instead.
            indeterminate = indeterminate_order_error(exc, ref_id)
            if indeterminate is not None:
                raise indeterminate from exc
            raise
