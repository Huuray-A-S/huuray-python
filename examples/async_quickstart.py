"""The same read-only tour, awaited.

    HUURAY_API_TOKEN=... HUURAY_API_SECRET=... python examples/async_quickstart.py

Nothing here orders, delivers, or spends anything. The async client exposes the
same surface as the synchronous one — the only difference is the ``await``.
"""

from __future__ import annotations

import asyncio
import os

from huuray import AsyncHuurayClient, HuurayNotFoundError


async def main() -> int:
    async with AsyncHuurayClient(
        api_token=os.environ["HUURAY_API_TOKEN"],
        api_secret=os.environ["HUURAY_API_SECRET"],
    ) as huuray:
        # Reads are independent, so gather them rather than waiting in turn.
        balances, catalogue = await asyncio.gather(
            huuray.balances.list(),
            huuray.catalogue.list(all=False),
        )

        for balance in balances.balances:
            print(f"{balance.currency}  {balance.balance / 100:.2f}")

        print(f"\n{len(catalogue.products)} products available")

        try:
            templates = (await huuray.templates.list()).templates
        except HuurayNotFoundError:
            # An account with no templates answers 404, not an empty list.
            templates = []
        print(f"{len(templates)} delivery templates")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
