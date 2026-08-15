"""The pattern that matters most: recovering from an order whose outcome is unknown.

``POST /v4/Order`` has no idempotency key. If the request times out or the server
returns a 5xx, the order may or may not have been created — and retrying can
order a second time, for real money.

So this SDK never retries an order. It raises ``HuurayIndeterminateOrderError``
and expects you to reconcile, which is only possible if you sent a ``ref_id`` you
can look up. That is why ``send_reward()`` requires one.
"""

from __future__ import annotations

import os
from typing import Optional

from huuray import (
    CreateOrderResult,
    HuurayClient,
    HuurayIndeterminateOrderError,
    HuurayNotFoundError,
    Recipient,
)

#: A key from your own system — stable, unique, and meaningful to you.
REF_ID = "payroll-2026-08-jane"


def send_once(huuray: HuurayClient) -> Optional[CreateOrderResult]:
    """Send one reward, and resolve the outcome if the request does not complete."""
    try:
        reward = huuray.send_reward(
            product_token="REPLACE_WITH_A_REAL_TOKEN",
            value=50_00,  # minor units — 50.00
            currency="DKK",
            recipient=Recipient(name="Jane Doe", email="jane@example.com"),
            template_id=1,
            ref_id=REF_ID,
        )
    except HuurayIndeterminateOrderError:
        # Do NOT retry the order here. Find out what actually happened.
        print("Order outcome unknown. Reconciling by ref_id instead of retrying.")

        try:
            found = huuray.orders.search(ref_id=REF_ID)
        except HuurayNotFoundError:
            # The API signals "no match" as a 404 — that IS the answer: the
            # order did not land.
            print("No order exists for this ref_id (404). Safe to send again.")
            return None
        # Anything else means the lookup itself failed and the outcome is STILL
        # unknown. Let it propagate — never treat a failed lookup as "not landed".

        if found.order_uid:
            print(f"It landed after all: order_uid={found.order_uid}. Nothing more to do.")
            return CreateOrderResult(order_uid=found.order_uid, ref_id=found.ref_id)

        print("No order exists for this ref_id. Safe to send again with the same ref_id.")
        return None
    else:
        print(f"Ordered. order_uid={reward.order_uid}")
        return reward


def main() -> int:
    with HuurayClient(
        api_token=os.environ["HUURAY_API_TOKEN"],
        api_secret=os.environ["HUURAY_API_SECRET"],
    ) as huuray:
        send_once(huuray)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
