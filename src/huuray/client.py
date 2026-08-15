"""The synchronous and asynchronous clients.

Both share one implementation of everything that can be wrong — signing, the
error taxonomy, the retry decision, the treatment of an unreadable body — and
differ only in how they wait.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar

import httpx

from ._version import VERSION
from .auth import DEFAULT_HASH_ENCODING, HashEncoding, build_auth_headers, generate_nonce
from .errors import (
    HuurayAPIError,
    HuurayConfigError,
    HuurayConnectionError,
    HuurayTimeoutError,
)
from .resources._base import Operation
from .resources.balances import AsyncBalancesResource, BalancesResource
from .resources.catalogue import AsyncCatalogueResource, CatalogueResource
from .resources.exchange_rates import AsyncExchangeRatesResource, ExchangeRatesResource
from .resources.orders import (
    AsyncOrdersResource,
    CreateOrderResult,
    OrdersResource,
    Recipient,
)
from .resources.stock import AsyncStockResource, StockResource
from .resources.templates import AsyncTemplatesResource, TemplatesResource
from .retry import DEFAULT_RETRY, RetryOptions, backoff_delay, is_retryable_status

#: The production API. The specification declares no ``servers`` block, so the
#: host is set here. Live-confirmed against every endpoint this client calls.
DEFAULT_BASE_URL = "https://api.huuray.com"

#: Per-request timeout in seconds, applied when the client is built without one.
DEFAULT_TIMEOUT = 30.0

T = TypeVar("T")

_UNREADABLE = object()


@dataclass(frozen=True)
class RawResponse(Generic[T]):
    """A parsed body plus the HTTP status, which some endpoints use semantically."""

    data: T
    http_status: int


class _BaseClient:
    """Configuration, signing, and response interpretation.

    Deliberately transport-free: it builds :class:`httpx.Request` objects and
    interprets finished responses, but never performs I/O. The sync and async
    subclasses supply the two lines that differ.
    """

    def __init__(
        self,
        *,
        api_token: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        hash_encoding: HashEncoding = DEFAULT_HASH_ENCODING,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Optional[RetryOptions] = None,
        user_agent: Optional[str] = None,
        nonce_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if not api_token:
            raise HuurayConfigError(
                "api_token is required. Pass it explicitly, e.g. from "
                "os.environ['HUURAY_API_TOKEN']."
            )
        if not api_secret:
            raise HuurayConfigError(
                "api_secret is required. Pass it explicitly, e.g. from "
                "os.environ['HUURAY_API_SECRET']."
            )

        self._api_token = api_token
        self._api_secret = api_secret
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._hash_encoding: HashEncoding = hash_encoding
        self._timeout = timeout
        self._retry = retry if retry is not None else DEFAULT_RETRY
        self._nonce_factory = nonce_factory or generate_nonce
        self._user_agent = " ".join(
            part for part in (f"huuray-python/{VERSION}", user_agent) if part
        )

    # -------------------------------------------------------------- requests

    def _build_request(self, op: Operation) -> httpx.Request:
        """Build one signed request. A fresh nonce every time it is called.

        The API rejects a repeated nonce for 60 days, so every retry attempt
        must be signed again rather than replayed.
        """
        headers = build_auth_headers(
            api_token=self._api_token,
            api_secret=self._api_secret,
            nonce=self._nonce_factory(),
            hash_encoding=self._hash_encoding,
        )
        headers["Accept"] = "application/json"
        headers["User-Agent"] = self._user_agent

        content: bytes | None = None
        if op.body is not None:
            content = json.dumps(op.body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        return httpx.Request(
            op.method,
            self._base_url + op.path,
            params=op.query or None,
            headers=headers,
            content=content,
            # The timeout is attached to the request itself, not left to the
            # client to copy across. This SDK hands a pre-built Request to
            # Client.send(), and httpx only copies the client-level timeout onto
            # such a request from 0.27.1 onward — before that the request
            # reaches httpcore with no timeout at all and can block forever.
            # An order that hangs forever never raises, so
            # HuurayIndeterminateOrderError never fires and the caller gets no
            # reconcile signal. Setting it here makes the behaviour independent
            # of the installed httpx version.
            extensions={"timeout": httpx.Timeout(self._timeout).as_dict()},
        )

    # ------------------------------------------------------------- responses

    def _transport_error(self, exc: httpx.HTTPError, op: Operation) -> HuurayConnectionError:
        """Map an httpx transport failure into this library's taxonomy.

        Nothing raw may escape the request path: a bare ``httpx`` exception
        would bypass every downstream check, including the one that wraps order
        failures in ``HuurayIndeterminateOrderError``.
        """
        if isinstance(exc, httpx.TimeoutException):
            return HuurayTimeoutError(op.method, op.path, self._timeout)
        return HuurayConnectionError(
            f"{op.method} {op.path} failed to reach the Huuray API: {exc}",
            op.method,
            op.path,
        )

    def _interpret(self, status: int, text: str, op: Operation) -> RawResponse[Any]:
        """Turn a finished response into a result, or raise.

        Raises :class:`HuurayConnectionError` for a 2xx whose body is empty or
        unparseable, and :class:`HuurayAPIError` for anything non-2xx.
        """
        parsed: Any = _UNREADABLE
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = _UNREADABLE

        if 200 <= status < 300:
            # Every documented 2xx carries a JSON body. An empty or unparseable
            # body on a success status is a transport-level fault (proxy
            # interference, truncation) — NOT an empty result. Coercing it to an
            # empty result would make orders.search() report "order absent"
            # after a garbled response, and the documented reconciliation flow
            # would re-order. The body is never quoted in the error: it could
            # hold voucher codes.
            if parsed is _UNREADABLE:
                shape = "not valid JSON" if text else "empty"
                raise HuurayConnectionError(
                    f"{op.method} {op.path} returned HTTP {status} but the body was "
                    f"{shape} ({len(text)} bytes). Treat the outcome as unknown rather "
                    "than empty.",
                    op.method,
                    op.path,
                )
            return RawResponse(data=parsed, http_status=status)

        raise HuurayAPIError.from_response(
            status,
            None if parsed is _UNREADABLE else parsed,
            op.method,
            op.path,
        )

    def _attempts(self, op: Operation) -> int:
        """Retries permitted for this operation. Never inferred from the verb."""
        return self._retry.max_retries if op.retryable else 0


class HuurayClient(_BaseClient):
    """Synchronous client for the Huuray API v4.

    .. code-block:: python

        from huuray import HuurayClient

        huuray = HuurayClient(
            api_token=os.environ["HUURAY_API_TOKEN"],
            api_secret=os.environ["HUURAY_API_SECRET"],
        )

        for balance in huuray.balances.list().balances:
            print(balance.currency, balance.balance)

    :param api_token: Your API token. Sent as ``X-API-TOKEN``.
    :param api_secret: Your API secret. Used to sign each request; never sent
        and never logged.
    :param base_url: Override the API host. Defaults to ``https://api.huuray.com``.
    :param hash_encoding: Encoding of the ``X-API-HASH`` digest. Defaults to
        lowercase hex. If you see a 401 with credentials you know are good, try
        another value.
    :param timeout: Per-request timeout in seconds.
    :param retry: Retry behaviour for read operations. Writes are never retried.
    :param user_agent: Appended to the ``User-Agent``, e.g. your app and version.
    :param nonce_factory: Supply your own nonce. Must be unique per request,
        unused for 60 days, and at most 50 characters. The default (24 random
        bytes, base64url) is right for almost everyone.
    :param transport: Inject an ``httpx`` transport — used by the test suite,
        and for proxies or custom TLS.
    """

    def __init__(
        self,
        *,
        api_token: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        hash_encoding: HashEncoding = DEFAULT_HASH_ENCODING,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Optional[RetryOptions] = None,
        user_agent: Optional[str] = None,
        nonce_factory: Optional[Callable[[], str]] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(
            api_token=api_token,
            api_secret=api_secret,
            base_url=base_url,
            hash_encoding=hash_encoding,
            timeout=timeout,
            retry=retry,
            user_agent=user_agent,
            nonce_factory=nonce_factory,
        )
        self._http = httpx.Client(transport=transport, timeout=timeout)

        self.balances = BalancesResource(self)
        self.catalogue = CatalogueResource(self)
        self.templates = TemplatesResource(self)
        self.stock = StockResource(self)
        self.exchange_rates = ExchangeRatesResource(self)
        self.orders = OrdersResource(self)

    # ---------------------------------------------------------- convenience

    def send_reward(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        recipient: Recipient,
        template_id: int,
        ref_id: str,
        expires: Any = None,
        delivery_datetime: Any = None,
        personal_message: Optional[str] = None,
    ) -> CreateOrderResult:
        """Send one gift card to one recipient — the common case, in one call.

        Performs exactly one ``POST /v4/Order`` with ``Sync: False`` and
        ``Quantity: 1``. Delivery is handled by Huuray using the template you
        name, so no voucher codes come back; use ``orders.search()`` to look the
        order up later.

        ``ref_id`` is required by this SDK even though the API treats it as
        optional: without it there is no way to find out whether an order landed
        after a timeout. See :class:`~huuray.HuurayIndeterminateOrderError`.
        """
        return self.orders.send_reward(
            product_token=product_token,
            value=value,
            currency=currency,
            recipient=recipient,
            template_id=template_id,
            ref_id=ref_id,
            expires=expires,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
        )

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: Optional[dict[str, str]] = None,
        retryable: bool = False,
    ) -> Any:
        """Call any v4 endpoint with signing handled.

        The escape hatch for anything the typed resources do not cover. Request
        and response shapes are exactly as documented in the Huuray API
        reference; this method does no renaming.

        ``retryable`` defaults to ``False`` and must be opted into per call.
        Never set it on ``/v4/Order``, ``/v4/Resend``, or ``/v4/Cancel``.

        .. code-block:: python

            huuray.request("POST", "/v4/Search", {"RefID": "payroll-2026-08-jane"})
        """
        op = Operation(
            method=method,
            path=path,
            body=body,
            query=query or {},
            retryable=retryable,
        )
        return self._send(op).data

    # ------------------------------------------------------------- internals

    def _send(self, op: Operation) -> RawResponse[Any]:
        """Sign and send one request, retrying only if the operation allows it."""
        attempts = self._attempts(op)

        for attempt in range(attempts + 1):
            if attempt:
                time.sleep(backoff_delay(attempt - 1, self._retry))

            request = self._build_request(op)
            try:
                # httpx reads the response body inside send(). That is
                # deliberate: a connection dropped or timed out *while the body
                # streams* must map through this same handler, or it escapes as
                # a raw httpx exception and bypasses the order-safety wrapper.
                response = self._http.send(request)
                text = response.text
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    continue
                raise self._transport_error(exc, op) from exc

            try:
                return self._interpret(response.status_code, text, op)
            except HuurayConnectionError:
                if attempt < attempts:
                    continue
                raise
            except HuurayAPIError as exc:
                if attempt < attempts and is_retryable_status(exc.http_status):
                    continue
                raise

        raise AssertionError("unreachable: the attempt loop always returns or raises")

    # -------------------------------------------------------------- lifetime

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def __enter__(self) -> HuurayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class AsyncHuurayClient(_BaseClient):
    """Asynchronous client for the Huuray API v4.

    The same surface as :class:`HuurayClient`, with every request awaited.

    .. code-block:: python

        async with AsyncHuurayClient(api_token=..., api_secret=...) as huuray:
            result = await huuray.balances.list()

    Accepts the same arguments as :class:`HuurayClient`, except that
    ``transport`` is an :class:`httpx.AsyncBaseTransport`.
    """

    def __init__(
        self,
        *,
        api_token: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        hash_encoding: HashEncoding = DEFAULT_HASH_ENCODING,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Optional[RetryOptions] = None,
        user_agent: Optional[str] = None,
        nonce_factory: Optional[Callable[[], str]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        super().__init__(
            api_token=api_token,
            api_secret=api_secret,
            base_url=base_url,
            hash_encoding=hash_encoding,
            timeout=timeout,
            retry=retry,
            user_agent=user_agent,
            nonce_factory=nonce_factory,
        )
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)

        self.balances = AsyncBalancesResource(self)
        self.catalogue = AsyncCatalogueResource(self)
        self.templates = AsyncTemplatesResource(self)
        self.stock = AsyncStockResource(self)
        self.exchange_rates = AsyncExchangeRatesResource(self)
        self.orders = AsyncOrdersResource(self)

    # ---------------------------------------------------------- convenience

    async def send_reward(
        self,
        *,
        product_token: str,
        value: int,
        currency: str,
        recipient: Recipient,
        template_id: int,
        ref_id: str,
        expires: Any = None,
        delivery_datetime: Any = None,
        personal_message: Optional[str] = None,
    ) -> CreateOrderResult:
        """Send one gift card to one recipient. See :meth:`HuurayClient.send_reward`."""
        return await self.orders.send_reward(
            product_token=product_token,
            value=value,
            currency=currency,
            recipient=recipient,
            template_id=template_id,
            ref_id=ref_id,
            expires=expires,
            delivery_datetime=delivery_datetime,
            personal_message=personal_message,
        )

    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: Optional[dict[str, str]] = None,
        retryable: bool = False,
    ) -> Any:
        """Call any v4 endpoint with signing handled. See :meth:`HuurayClient.request`."""
        op = Operation(
            method=method,
            path=path,
            body=body,
            query=query or {},
            retryable=retryable,
        )
        return (await self._send(op)).data

    # ------------------------------------------------------------- internals

    async def _send(self, op: Operation) -> RawResponse[Any]:
        """Sign and send one request, retrying only if the operation allows it."""
        attempts = self._attempts(op)

        for attempt in range(attempts + 1):
            if attempt:
                await asyncio.sleep(backoff_delay(attempt - 1, self._retry))

            request = self._build_request(op)
            try:
                # As in the sync client: the body read happens inside send(), so
                # a mid-body failure maps through the same taxonomy.
                response = await self._http.send(request)
                text = response.text
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    continue
                raise self._transport_error(exc, op) from exc

            try:
                return self._interpret(response.status_code, text, op)
            except HuurayConnectionError:
                if attempt < attempts:
                    continue
                raise
            except HuurayAPIError as exc:
                if attempt < attempts and is_retryable_status(exc.http_status):
                    continue
                raise

        raise AssertionError("unreachable: the attempt loop always returns or raises")

    # -------------------------------------------------------------- lifetime

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._http.aclose()

    async def __aenter__(self) -> AsyncHuurayClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
