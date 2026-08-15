"""``POST /v4/Stock`` — how many gift cards are available."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ._base import AsyncResource, Operation, Resource, compact, require_minor_units


@dataclass(frozen=True)
class CheckStockResult:
    #: Number of gift cards available, or ``None`` if the API did not report one.
    stock: int | None


def _operation(product_token: str, value: Optional[int]) -> Operation:
    if value is not None:
        require_minor_units(value)
    return Operation(
        method="POST",
        path="/v4/Stock",
        body=compact({"ProductToken": product_token, "Value": value}),
        retryable=True,
    )


def _map(data: Any) -> CheckStockResult:
    return CheckStockResult(stock=(data or {}).get("Stock"))


class StockResource(Resource):
    def check(self, *, product_token: str, value: Optional[int] = None) -> CheckStockResult:
        """Current stock for a product.

        ``POST /v4/Stock``

        A read, despite being a POST.

        :param product_token: The product to check. Get this from ``catalogue.list()``.
        :param value: The denomination to check, **in minor units**. Omit to use
            the product's default price.
        """
        return _map(self._client._send(_operation(product_token, value)).data)


class AsyncStockResource(AsyncResource):
    async def check(self, *, product_token: str, value: Optional[int] = None) -> CheckStockResult:
        """Current stock for a product.

        ``POST /v4/Stock``
        """
        return _map((await self._client._send(_operation(product_token, value))).data)
