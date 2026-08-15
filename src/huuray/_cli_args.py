"""Argument parsing and output formatting for the read-only CLI.

Kept apart from ``cli.py`` so the parser and the table renderer can be tested
without a client, and without anything that could reach the network.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import Any

USAGE_EPILOG = """
Credentials, from the environment:
  HUURAY_API_TOKEN
  HUURAY_API_SECRET
  HUURAY_BASE_URL     optional; defaults to https://api.huuray.com

Ordering, resending and cancelling are not available here. They move real
value, so they belong in code you have reviewed. See the README.

Voucher codes are never printed by this CLI.
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Every command is read-only. There is deliberately no ``order``, ``resend``
    or ``cancel`` here: sending real gift cards from a shell one-liner is too
    easy to do by accident, and a mistyped quantity is money.
    """
    parser = argparse.ArgumentParser(
        prog="huuray",
        description="Read-only CLI for the Huuray API v4.",
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    subcommands.add_parser("balance", parents=[common], help="available balances, per currency")

    catalogue = subcommands.add_parser("catalogue", parents=[common], help="products you can order")
    catalogue.add_argument(
        "--all",
        action="store_true",
        help="the entire Huuray catalogue, without tokens or discounts",
    )

    subcommands.add_parser("templates", parents=[common], help="delivery templates on your account")

    stock = subcommands.add_parser("stock", parents=[common], help="stock for a product")
    stock.add_argument("--token", required=True, metavar="TOKEN", help="the product token")
    stock.add_argument(
        "--value", type=int, metavar="N", help="denomination in minor units (5.00 is 500)"
    )

    rates = subcommands.add_parser("rates", parents=[common], help="exchange rate and spread")
    rates.add_argument("--from", dest="from_currency", required=True, metavar="CUR")
    rates.add_argument("--to", dest="to_currency", required=True, metavar="CUR")

    search = subcommands.add_parser(
        "search", parents=[common], help="look up vouchers from previous orders"
    )
    search.add_argument("--ref-id", dest="ref_id", metavar="REF")
    search.add_argument("--order-uid", dest="order_uid", metavar="UID")
    search.add_argument("--voucher-id", dest="voucher_id", type=int, metavar="N")

    return parser


def table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Minimal fixed-width table. Kept local so the package ships no CLI dependencies."""
    if not rows:
        return "(no results)"

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    width = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            cell.ljust(width[columns[index]]) for index, cell in enumerate(cells)
        ).rstrip()

    return "\n".join(
        [
            line(columns),
            # ASCII, not U+2500: this is printed to whatever encoding stdout
            # happens to have, and on Windows a redirected or piped stdout
            # falls back to the ANSI code page, where a box-drawing character
            # raises UnicodeEncodeError. `huuray balance > out.txt` must not
            # fail after the API call already succeeded.
            line(["-" * width[column] for column in columns]),
            *(line([str(row.get(column, "")) for column in columns]) for row in rows),
        ]
    )
