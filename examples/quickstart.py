"""Quickstart — read-only.

Every call here is safe to run against a live account: nothing is ordered,
nothing is delivered, nothing is spent.

    HUURAY_API_TOKEN=... HUURAY_API_SECRET=... python examples/quickstart.py
"""

from __future__ import annotations

import os

from huuray import HuurayClient, HuurayNotFoundError


def main() -> int:
    huuray = HuurayClient(
        api_token=os.environ["HUURAY_API_TOKEN"],
        api_secret=os.environ["HUURAY_API_SECRET"],
    )

    with huuray:
        # 1. What can we spend? Amounts are in minor units: 50000 is 500.00.
        for balance in huuray.balances.list().balances:
            master = "  (master)" if balance.master else ""
            print(f"{balance.currency}  {balance.balance / 100:.2f}{master}")

        # 2. What can we send? Leaving `all` false returns only products this
        #    account can order, and includes the product_token you need.
        products = huuray.catalogue.list(all=False).products
        print(f"\n{len(products)} products available")

        first = next((p for p in products if p.active and p.product_token), None)
        if first is None or first.product_token is None:
            print("No orderable products on this account.")
            return 0
        print(f"Example: {first.brand_name} ({first.currency}) — token {first.product_token}")

        # 3. Is it in stock?
        stock = huuray.stock.check(product_token=first.product_token).stock
        print(f"Stock: {stock if stock is not None else 'unknown'}")

        # 4. How would it be delivered? Templates are the emails and texts
        #    recipients get. An account with none gets a 404, not an empty list.
        try:
            templates = huuray.templates.list().templates
        except HuurayNotFoundError:
            templates = []
        print(f"\n{len(templates)} delivery templates")
        for template in templates[:5]:
            print(f"  {template.id}  {template.name} ({template.type}, {template.language})")

    # Sending an actual reward is one more call. It is commented out because
    # running it spends real money:
    #
    # reward = huuray.send_reward(
    #     product_token=first.product_token,
    #     value=50_00,                 # minor units — 50.00
    #     currency=first.currency,
    #     recipient=Recipient(name="Jane Doe", email="jane@example.com"),
    #     template_id=templates[0].id,
    #     ref_id="quickstart-demo-1",  # your own key, required
    # )
    # print(reward.order_uid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
