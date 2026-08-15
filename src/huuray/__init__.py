"""Official Python client for the Huuray API v4.

.. code-block:: python

    import os
    from huuray import HuurayClient

    huuray = HuurayClient(
        api_token=os.environ["HUURAY_API_TOKEN"],
        api_secret=os.environ["HUURAY_API_SECRET"],
    )

    result = huuray.balances.list()

Every method maps onto a single documented v4 operation. Request and response
field names match the Huuray API reference exactly, differing only in casing:
``OrderUID`` becomes ``order_uid``.
"""

from __future__ import annotations

from ._version import VERSION, __version__
from .auth import (
    DEFAULT_HASH_ENCODING,
    NONCE_MAX_LENGTH,
    HashEncoding,
    build_auth_headers,
    generate_nonce,
    sign_request,
)
from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    AsyncHuurayClient,
    HuurayClient,
    RawResponse,
)
from .errors import (
    HuurayAPIError,
    HuurayAuthError,
    HuurayConfigError,
    HuurayConnectionError,
    HuurayError,
    HuurayIndeterminateOrderError,
    HuurayNotFoundError,
    HuurayServerError,
    HuurayTimeoutError,
    HuurayValidationError,
)
from .redact import SECRET_FIELDS, SENSITIVE_FIELDS, redact, safe_json
from .resources.balances import Balance, ListBalancesResult
from .resources.catalogue import CatalogueProduct, ListCatalogueResult
from .resources.exchange_rates import ExchangeRateResult
from .resources.orders import (
    SYNC_QUANTITY_LIMIT,
    CancelledVoucher,
    CancelResult,
    CreateOrderResult,
    CreateSyncOrderResult,
    Recipient,
    ResendResult,
    SearchOrdersResult,
    Voucher,
)
from .resources.stock import CheckStockResult
from .resources.templates import ListTemplatesResult, Template
from .retry import DEFAULT_RETRY, RetryOptions

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_HASH_ENCODING",
    "DEFAULT_RETRY",
    "DEFAULT_TIMEOUT",
    "NONCE_MAX_LENGTH",
    "SECRET_FIELDS",
    "SENSITIVE_FIELDS",
    "SYNC_QUANTITY_LIMIT",
    "VERSION",
    "AsyncHuurayClient",
    "Balance",
    "CancelResult",
    "CancelledVoucher",
    "CatalogueProduct",
    "CheckStockResult",
    "CreateOrderResult",
    "CreateSyncOrderResult",
    "ExchangeRateResult",
    "HashEncoding",
    "HuurayAPIError",
    "HuurayAuthError",
    "HuurayClient",
    "HuurayConfigError",
    "HuurayConnectionError",
    "HuurayError",
    "HuurayIndeterminateOrderError",
    "HuurayNotFoundError",
    "HuurayServerError",
    "HuurayTimeoutError",
    "HuurayValidationError",
    "ListBalancesResult",
    "ListCatalogueResult",
    "ListTemplatesResult",
    "RawResponse",
    "Recipient",
    "ResendResult",
    "RetryOptions",
    "SearchOrdersResult",
    "Template",
    "Voucher",
    "__version__",
    "build_auth_headers",
    "generate_nonce",
    "redact",
    "safe_json",
    "sign_request",
]
