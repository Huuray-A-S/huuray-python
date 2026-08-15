"""Read-only command line interface.

Deliberately limited to operations that cannot move value: there is no ordering,
resending, or cancelling here. Sending real gift cards from a shell one-liner is
too easy to do by accident, and a mistyped quantity is money.

Voucher codes are never printed, whatever the account settings allow.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, Callable, Optional

from ._cli_args import build_parser, table
from .client import DEFAULT_BASE_URL, HuurayClient
from .errors import HuurayAPIError, HuurayError
from .redact import redact

Rows = Callable[[], list[dict[str, Any]]]


def _emit(data: Any, rows: Rows, *, as_json: bool) -> None:
    """Print a result. Redaction runs on both paths — codes never reach stdout.

    ``redact()`` understands dataclasses, so the result objects this SDK returns
    are stripped just as thoroughly as a raw response body would be.
    """
    if as_json:
        print(json.dumps(redact(data), indent=2, default=str))
    else:
        print(table([redact(row) for row in rows()]))


def _run(client: HuurayClient, args: Any) -> int:
    as_json = bool(args.json)

    if args.command == "balance":
        result = client.balances.list()
        _emit(
            result.balances,
            lambda: [
                {
                    "currency": row.currency or "",
                    "balance (minor units)": row.balance,
                    "master": "yes" if row.master else "",
                }
                for row in result.balances
            ],
            as_json=as_json,
        )
        return 0

    if args.command == "catalogue":
        catalogue = client.catalogue.list(all=args.all)
        _emit(
            catalogue.products,
            lambda: [
                {
                    "token": row.product_token or "(not returned with --all)",
                    "brand": row.brand_name or "",
                    "country": row.country_code or "",
                    "currency": row.currency or "",
                    "discount": row.discount if row.discount is not None else "",
                    "active": "yes" if row.active else "no",
                }
                for row in catalogue.products
            ],
            as_json=as_json,
        )
        return 0

    if args.command == "templates":
        templates = client.templates.list()
        _emit(
            templates.templates,
            lambda: [
                {
                    "id": row.id,
                    "name": row.name or "",
                    "type": row.type or "",
                    "language": row.language or "",
                    "sender": row.sender or "",
                }
                for row in templates.templates
            ],
            as_json=as_json,
        )
        return 0

    if args.command == "stock":
        stock = client.stock.check(product_token=args.token, value=args.value)
        _emit(
            stock,
            lambda: [{"stock": stock.stock if stock.stock is not None else "unknown"}],
            as_json=as_json,
        )
        return 0

    if args.command == "rates":
        rate = client.exchange_rates.get(
            from_currency=args.from_currency, to_currency=args.to_currency
        )
        _emit(
            rate,
            lambda: [
                {
                    "rate": rate.exchange_rate if rate.exchange_rate is not None else "",
                    "spread (%)": rate.spread if rate.spread is not None else "",
                }
            ],
            as_json=as_json,
        )
        return 0

    if args.command == "search":
        found = client.orders.search(
            ref_id=args.ref_id,
            order_uid=args.order_uid,
            voucher_id=args.voucher_id,
        )
        # No code column at all: codes are never printed by this CLI, and a
        # column of redaction markers would wrongly imply codes were present.
        _emit(
            found,
            lambda: [
                {
                    "voucher id": voucher.id if voucher.id is not None else "",
                    "expires": voucher.expires or "",
                    "recipient": (
                        (voucher.recipient.name or voucher.recipient.ref_id or "")
                        if voucher.recipient
                        else ""
                    ),
                }
                for voucher in found.vouchers
            ],
            as_json=as_json,
        )
        if not as_json:
            print(f"\norder: {found.order_uid or '(none)'}  ref: {found.ref_id or ''}")
            print("(voucher codes are never printed by this CLI)")
        return 0

    print(f'Unknown command "{args.command}". Run "huuray --help".', file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``huuray`` command. Returns the process exit code."""
    # Recipient names and brand names are free text and routinely non-ASCII.
    # A redirected stdout on Windows defaults to the ANSI code page, which
    # cannot encode them, so print() would raise after the request already
    # succeeded. Degrade the odd character instead of failing the command.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no cover - depends on stream type
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Help must work before anything else, including the credential check.
    if not args.command:
        parser.print_help()
        return 1

    api_token = os.environ.get("HUURAY_API_TOKEN")
    api_secret = os.environ.get("HUURAY_API_SECRET")
    if not api_token or not api_secret:
        print(
            "Set HUURAY_API_TOKEN and HUURAY_API_SECRET in the environment.",
            file=sys.stderr,
        )
        print('Run "huuray --help" for usage.', file=sys.stderr)
        return 1

    base_url = os.environ.get("HUURAY_BASE_URL")
    client = HuurayClient(
        api_token=api_token,
        api_secret=api_secret,
        base_url=base_url or DEFAULT_BASE_URL,
        user_agent="huuray-cli",
    )

    try:
        with client:
            return _run(client, args)
    except HuurayAPIError as err:
        print(f"Error: {err}", file=sys.stderr)
        if err.http_status in (401, 403):
            print(
                "\nIf the credentials are correct, the X-API-HASH encoding may differ from\n"
                'this client\'s default. See the README section "Authentication".',
                file=sys.stderr,
            )
        return 1
    except HuurayError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
