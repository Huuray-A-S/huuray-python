"""``GET /v4/Balance`` — available balance per currency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._base import AsyncResource, Operation, Resource


@dataclass(frozen=True)
class Balance:
    """One currency balance on your B2B account."""

    #: ISO alpha-3 currency code.
    currency: str | None
    #: Available balance **in minor units** — ``50000`` is 500.00.
    balance: int
    #: Whether this is a master currency on the account.
    master: bool


@dataclass(frozen=True)
class ListBalancesResult:
    balances: list[Balance] = field(default_factory=list)


def _operation() -> Operation:
    return Operation(method="GET", path="/v4/Balance", retryable=True)


def _map(data: Any) -> ListBalancesResult:
    rows = (data or {}).get("Balances") or []
    return ListBalancesResult(
        balances=[
            Balance(
                currency=row.get("Currency"),
                balance=row.get("Balance") or 0,
                master=bool(row.get("Master")),
            )
            for row in rows
        ]
    )


class BalancesResource(Resource):
    def list(self) -> ListBalancesResult:
        """Available balances on your B2B account, per currency.

        ``GET /v4/Balance``

        Amounts are in **minor units**: ``50000`` means 500.00, not 50000.00.
        """
        return _map(self._client._send(_operation()).data)


class AsyncBalancesResource(AsyncResource):
    async def list(self) -> ListBalancesResult:
        """Available balances on your B2B account, per currency.

        ``GET /v4/Balance``
        """
        return _map((await self._client._send(_operation())).data)
