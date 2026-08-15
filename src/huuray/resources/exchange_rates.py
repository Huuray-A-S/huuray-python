"""``GET /v4/ExchangeRates`` — rate and spread between two currencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._base import AsyncResource, Operation, Resource


@dataclass(frozen=True)
class ExchangeRateResult:
    exchange_rate: float | None
    #: Spread in percent.
    spread: int | None


def _operation(from_currency: str, to_currency: str) -> Operation:
    return Operation(
        method="GET",
        path="/v4/ExchangeRates",
        query={"FromCurrency": from_currency, "ToCurrency": to_currency},
        retryable=True,
    )


def _map(data: Any) -> ExchangeRateResult:
    payload = data or {}
    return ExchangeRateResult(
        exchange_rate=payload.get("ExchangeRate"),
        spread=payload.get("Spread"),
    )


class ExchangeRatesResource(Resource):
    def get(self, *, from_currency: str, to_currency: str) -> ExchangeRateResult:
        """Current exchange rate and spread between two currencies.

        ``GET /v4/ExchangeRates``

        :param from_currency: Source currency, ISO alpha-3.
        :param to_currency: Target currency, ISO alpha-3.
        """
        return _map(self._client._send(_operation(from_currency, to_currency)).data)


class AsyncExchangeRatesResource(AsyncResource):
    async def get(self, *, from_currency: str, to_currency: str) -> ExchangeRateResult:
        """Current exchange rate and spread between two currencies.

        ``GET /v4/ExchangeRates``
        """
        return _map((await self._client._send(_operation(from_currency, to_currency))).data)
