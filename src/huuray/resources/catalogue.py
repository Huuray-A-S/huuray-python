"""``POST /v4/Catalogue`` — the products you can order."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._base import AsyncResource, Operation, Resource


@dataclass(frozen=True)
class CatalogueProduct:
    """A product in the Huuray catalogue."""

    #: Unique product identifier, used when ordering.
    #:
    #: Only present when ``all`` is ``False``. Requesting the entire catalogue
    #: omits tokens, because they describe your account's access rather than the
    #: public list.
    product_token: str | None
    brand_name: str | None
    country: str | None
    #: ISO alpha-2 country code.
    country_code: str | None
    #: Your discount on this product, in percent. Only present when ``all`` is ``False``.
    discount: float | None
    #: Available denominations, comma-separated, as returned by the API.
    denominations: str | None
    #: ISO alpha-3 currency code.
    currency: str | None
    #: Either real-time generated or drawn from stock.
    real_time_stock: str | None
    #: Categories, comma-separated, as returned by the API.
    categories: str | None
    #: ISO alpha-2 language code.
    language_code: str | None
    active: bool
    brand_description: str | None
    redemption_instructions: str | None
    logo_file: str | None


@dataclass(frozen=True)
class ListCatalogueResult:
    products: list[CatalogueProduct] = field(default_factory=list)


def _operation(all: bool) -> Operation:  # noqa: A002 - mirrors the spec field name
    return Operation(
        method="POST",
        path="/v4/Catalogue",
        body={"All": all},
        retryable=True,
    )


def _map(data: Any) -> ListCatalogueResult:
    rows = (data or {}).get("Products") or []
    return ListCatalogueResult(
        products=[
            CatalogueProduct(
                product_token=row.get("ProductToken"),
                brand_name=row.get("BrandName"),
                country=row.get("Country"),
                country_code=row.get("CountryCode"),
                discount=row.get("Discount"),
                denominations=row.get("Denominations"),
                currency=row.get("Currency"),
                real_time_stock=row.get("RealTimeStock"),
                categories=row.get("Categories"),
                language_code=row.get("LanguageCode"),
                active=bool(row.get("Active")),
                brand_description=row.get("BrandDescription"),
                redemption_instructions=row.get("RedemptionInstructions"),
                logo_file=row.get("LogoFile"),
            )
            for row in rows
        ]
    )


class CatalogueResource(Resource):
    def list(self, *, all: bool = False) -> ListCatalogueResult:  # noqa: A002
        """List available products.

        ``POST /v4/Catalogue``

        A read, despite being a POST — it takes a request body but changes
        nothing, which is why it is retried and ordering is not.

        :param all: ``False`` (default) returns only products your account can
            order, including your discount and each ``product_token``. ``True``
            returns the entire Huuray catalogue, without tokens or discounts.
        """
        return _map(self._client._send(_operation(all)).data)


class AsyncCatalogueResource(AsyncResource):
    async def list(self, *, all: bool = False) -> ListCatalogueResult:  # noqa: A002
        """List available products.

        ``POST /v4/Catalogue``
        """
        return _map((await self._client._send(_operation(all))).data)
