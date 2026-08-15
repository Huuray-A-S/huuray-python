# huuray

#### Easily send gift cards and rewards from Python

<!-- badges: start -->
[![CI](https://github.com/Huuray-A-S/huuray-python/actions/workflows/ci.yml/badge.svg)](https://github.com/Huuray-A-S/huuray-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/huuray.svg?color=45652a)](https://pypi.org/project/huuray/)
[![Python](https://img.shields.io/pypi/pyversions/huuray.svg?color=45652a)](https://pypi.org/project/huuray/)
[![License: MIT](https://img.shields.io/badge/license-MIT-45652a.svg)](LICENSE)
[![API v4](https://img.shields.io/badge/Huuray%20API-v4-9dcf73.svg)](https://api.huuray.com/swagger/index.html)
[![Sign up](https://img.shields.io/badge/Huuray-sign%20up-ff5c43.svg)](https://huuray.com/sign-up/)
<!-- badges: end -->

[Huuray](https://huuray.com) is a platform for sending digital gift cards and rewards to recipients in 170+ countries. `huuray` is the official, slightly-opinionated Python client for the **Huuray API v4** — with, dare we say, *hurray*-worthy defaults for the parts of a rewards API that are easy to get wrong.

Use it to send employee recognition, customer incentives, survey payouts, referral bonuses, or research participant compensation — without anyone opening a dashboard.

```python
import os
from huuray import HuurayClient, Recipient

huuray = HuurayClient(
    api_token=os.environ["HUURAY_API_TOKEN"],
    api_secret=os.environ["HUURAY_API_SECRET"],
)

huuray.send_reward(
    product_token="the-product-you-chose",
    value=50_00,  # minor units — 50.00
    currency="DKK",
    recipient=Recipient(name="Jane Doe", email="jane@example.com"),
    template_id=42,
    ref_id="payroll-2026-08-jane",  # your own key
)
```

- **Sync and async**, sharing one implementation — `HuurayClient` and `AsyncHuurayClient`.
- **Fully typed**, checked against the Huuray OpenAPI specification in CI, so it cannot drift from the API.
- **Request signing handled** — the nonce and SHA-512 hash every call needs.
- **Safe by default around money** — orders are never automatically retried, because the API has no idempotency key.
- **One runtime dependency**: `httpx`.

## Requirements

- **Python 3.9 or newer.**
- **A Huuray B2B account.** New to Huuray? [Sign up here](https://huuray.com/sign-up/) — it takes a couple of minutes.
- **API credentials** — an API token and secret for your account. Ask your Huuray contact to enable API access if you do not have them yet.

The full API this client wraps is documented at the [Huuray API v4 reference (Swagger)](https://api.huuray.com/swagger/index.html).

## Install

```bash
pip install huuray
```

## Getting started

Start with calls that only read. None of these order anything, deliver anything, or spend anything:

```python
import os
from huuray import HuurayClient

huuray = HuurayClient(
    api_token=os.environ["HUURAY_API_TOKEN"],
    api_secret=os.environ["HUURAY_API_SECRET"],
)

# What can you spend? Amounts are in minor units: 50000 is 500.00.
balances = huuray.balances.list().balances

# What can you send? Omitting `all` returns just your products, with tokens.
products = huuray.catalogue.list().products

# How will it be delivered? Templates are the emails and texts recipients get.
templates = huuray.templates.list().templates
```

The same calls, awaited:

```python
from huuray import AsyncHuurayClient

async with AsyncHuurayClient(api_token=..., api_secret=...) as huuray:
    balances = (await huuray.balances.list()).balances
```

Or from a terminal, without writing any code:

```bash
export HUURAY_API_TOKEN=... HUURAY_API_SECRET=...
huuray balance
huuray catalogue
```

## Sending a reward

`send_reward()` is one gift card to one recipient — the common case, and exactly one `POST /v4/Order`:

```python
from huuray import Recipient

reward = huuray.send_reward(
    product_token="the-product-you-chose",
    value=50_00,
    currency="DKK",
    recipient=Recipient(name="Jane Doe", email="jane@example.com"),
    template_id=42,
    ref_id="payroll-2026-08-jane",
)

reward.order_uid  # keep this
```

For anything larger, use the orders resource directly:

```python
huuray.orders.create(
    product_token="the-product-you-chose",
    value=25_00,
    currency="DKK",
    quantity=200,
    template_id=42,
    ref_id="q3-customer-thankyou",
    recipients=[...],  # 1 recipient, or exactly 200
)
```

## Seven things worth knowing

These are the parts of the API that are easy to get wrong. The client handles each one, but the behaviour is worth understanding.

### 1. Money is in minor units

`value=50_00` is 50.00, not 5000.00. Passing a major-unit amount into this field orders **1/100th** of what you meant, so the client rejects anything that is not an `int`:

```python
huuray.send_reward(value=50.5,  ...)   # raises ValueError — not an int
huuray.send_reward(value=50.0,  ...)   # raises ValueError — a float is major units by mistake
huuray.send_reward(value=50_00, ...)   # 50.00
```

Python catches a mixup that a JavaScript client cannot: `50.00` is a `float` here, not an integer, so it never reaches the API. The one case no guard can catch is a whole-number amount — `value=50` is a perfectly valid order for 0.50 — so always write amounts as integers in minor units.

Every input guard raises the built-in `ValueError` **before any request is sent**, so a rejected amount never reaches the network.

### 2. Orders are never retried automatically

`POST /v4/Order` has no idempotency key, so retrying a timed-out order can order a second time — real gift cards, real money. This client never does that. Instead it raises `HuurayIndeterminateOrderError`, and you reconcile:

```python
from huuray import HuurayIndeterminateOrderError, HuurayNotFoundError

try:
    huuray.send_reward(ref_id="payroll-2026-08-jane", ...)
except HuurayIndeterminateOrderError:
    # Do NOT retry. Find out what actually happened.
    try:
        found = huuray.orders.search(ref_id="payroll-2026-08-jane")
    except HuurayNotFoundError:
        # The API answers 404 when nothing matches: the order did not land.
        # Safe to send again with the same ref_id.
        pass
    else:
        if found.order_uid:
            pass   # It landed. Nothing more to do.
        else:
            pass   # No match — it did not land. Safe to send again.
```

Anything other than `HuurayNotFoundError` from the lookup means the lookup itself failed and the outcome is *still* unknown — never treat a failed lookup as "not landed".

This is why `send_reward()` requires a `ref_id` even though the API treats it as optional: without one, an order that times out cannot be looked up.

Reads *are* retried — with backoff and jitter, on connection failures and 5xx.

### 3. Synchronous and asynchronous orders are different calls

| | `orders.create()` | `orders.create_sync()` |
|---|---|---|
| Sends | `Sync: false` | `Sync: true` |
| Quantity | no documented limit | max 25 |
| Returns | `order_uid` only | `order_uid` **and vouchers** |
| Delivery | Huuray sends via your template | you handle the codes |

"No documented limit" is precise, not a promise: the API specification states a cap only for synchronous orders. It says nothing about an asynchronous maximum, so this client imposes none — but a very large single order is untested territory. Ask your Huuray contact before relying on one.

They are separate methods because their return types differ. Reading `vouchers` on an asynchronous order is a mistake a type checker should catch, not a runtime surprise. (This is about the API's `Sync` flag, not about `async def` — both are available on `HuurayClient` and `AsyncHuurayClient` alike.)

### 4. `206 Partial Content` is a real outcome

Cancel and resend can partly succeed. Checking only that the request "worked" will miss it:

```python
result = huuray.orders.cancel(order_uid=order_uid)

if result.partial:
    failed = [v for v in result.vouchers if not v.cancelled]
    logger.warning("%d vouchers could not be cancelled", len(failed))
```

### 5. Voucher codes are blank unless your account allows them

`voucher.code`, `voucher.cvv` and `voucher.redeem_link` are returned only if **`ReturnCode` is enabled on your B2B account**. Otherwise they come back empty and Huuray delivers the codes for you. If you need codes returned to your own system, ask your Huuray contact to enable it.

This client never logs a code. A `Voucher` masks all three in its `repr()`, so one reaching a log line or a traceback carries nothing redeemable, and `redact()` is exported so you can strip your own structures too:

```python
from huuray import redact

logger.info("order complete: %s", redact(result))  # codes stripped
```

### 6. An empty result is a 404, not an empty list

The API signals "nothing found" as HTTP 404 with a message like *"There were no active templates"* — so `templates.list()` on an account with no templates, or `orders.search()` with no match, raises `HuurayNotFoundError` rather than returning an empty list. Catch it and read it as "none exist":

```python
from huuray import HuurayNotFoundError

try:
    templates = huuray.templates.list().templates
except HuurayNotFoundError:
    templates = []  # 404 -> none exist
```

### 7. Authentication, and what a 401 usually means

Every request carries three headers, all built for you:

| Header | Value |
|---|---|
| `X-API-TOKEN` | your API token |
| `X-API-NONCE` | a random value, **single-use within 60 days, max 50 characters** |
| `X-API-HASH` | SHA-512 of ( API secret + nonce ) |

Nonces are 24 random bytes as base64url — 32 characters, comfortably under the limit. Avoid rolling your own: 32-byte hex is 64 characters and is silently rejected, and timestamps collide under concurrency.

**If you get a 401 with credentials you know are correct**, the digest encoding is the thing to try. The API specification states the construction but not the encoding, so this client defaults to lowercase hex — confirmed against the live API — and lets you change it:

```python
HuurayClient(api_token=..., api_secret=..., hash_encoding="base64")
# "hex" (default) | "hex-upper" | "base64" | "base64url"
```

## API coverage

All nine v4 operations, and nothing else. Every method maps to one operation in the [Swagger reference](https://api.huuray.com/swagger/index.html):

| Method | Endpoint |
|---|---|
| `balances.list()` | `GET /v4/Balance` |
| `catalogue.list(all=...)` | `POST /v4/Catalogue` |
| `templates.list()` | `POST /v4/Template` |
| `stock.check(product_token=..., value=...)` | `POST /v4/Stock` |
| `exchange_rates.get(from_currency=..., to_currency=...)` | `GET /v4/ExchangeRates` |
| `orders.create(...)` | `POST /v4/Order` (`Sync: false`) |
| `orders.create_sync(...)` | `POST /v4/Order` (`Sync: true`) |
| `orders.send_reward(...)` | `POST /v4/Order`, one recipient |
| `orders.search(...)` | `POST /v4/Search` |
| `orders.resend(...)` | `POST /v4/Resend` |
| `orders.cancel(...)` | `DELETE /v4/Cancel` |

Every one of these exists on both clients, identically — `AsyncHuurayClient` differs only in that you await it.

Need something not covered? `request()` signs any call for you:

```python
huuray.request("POST", "/v4/Search", {"RefID": "payroll-2026-08-jane"})
```

**This client targets API v4 only.** Field names match the Huuray API reference exactly, differing only in casing (`OrderUID` → `order_uid`), so anything you read in the API documentation maps straight across.

## Errors

Every error raised by this library extends `HuurayError`. Input guards — a fractional amount, a quantity over the synchronous limit, a recipient count that is neither 1 nor `quantity` — raise the built-in `ValueError` instead, before anything is sent.

| Class | When |
|---|---|
| `HuurayConfigError` | missing or invalid client options |
| `HuurayConnectionError` | the request never reached the API, or its response was unreadable |
| `HuurayTimeoutError` | the request exceeded `timeout` |
| `HuurayAuthError` | 401 or 403 — see *Authentication* above |
| `HuurayNotFoundError` | 404 — including "no results", see above |
| `HuurayValidationError` | 422 |
| `HuurayServerError` | 5xx |
| `HuurayAPIError` | any other non-2xx; the base for the four above |
| `HuurayIndeterminateOrderError` | an order whose outcome is unknown — **do not retry** |

API errors carry `http_status`, `status`, `status_message`, and the parsed `body`. The client reads `StatusMessage` and falls back to the deprecated `Message`. The retained `body` is redacted, so logging an error object never leaks a voucher code.

## Client options

```python
HuurayClient(
    api_token=...,  # required
    api_secret=...,  # required
    base_url=...,  # default https://api.huuray.com
    hash_encoding=...,  # "hex" | "hex-upper" | "base64" | "base64url"
    timeout=30.0,  # seconds
    retry=RetryOptions(max_retries=2, base_delay=0.25, max_delay=4.0),
    user_agent="my-app/1.0",
    nonce_factory=...,  # supply your own; must be unique and <= 50 characters
    transport=...,  # an httpx transport, e.g. behind a proxy
)
```

`AsyncHuurayClient` takes the same arguments, except that `transport` is an `httpx.AsyncBaseTransport`. Both are context managers — `with` and `async with` respectively — and expose `close()` / `aclose()`.

## CLI

Read-only by design. Ordering, resending and cancelling move real value and belong in reviewed code, not a shell one-liner. Voucher codes are never printed.

```bash
huuray balance
huuray catalogue --all
huuray templates
huuray stock --token <token> --value 5000
huuray rates --from EUR --to DKK
huuray search --ref-id payroll-2026-08-jane
huuray --help
```

## Examples

- [`examples/quickstart.py`](examples/quickstart.py) — read-only tour, safe to run
- [`examples/async_quickstart.py`](examples/async_quickstart.py) — the same tour, awaited
- [`examples/reconcile_after_timeout.py`](examples/reconcile_after_timeout.py) — recovering from an order whose outcome is unknown

## Links

- [Sign up for a Huuray B2B account](https://huuray.com/sign-up/)
- [Huuray API v4 reference (Swagger)](https://api.huuray.com/swagger/index.html)
- [huuray.com](https://huuray.com)
- [Report a bug](https://github.com/Huuray-A-S/huuray-python/issues)

## Further reading

- [Huuray API v4 reference (Swagger)](https://api.huuray.com/swagger/index.html) — the specification this client is checked against
- [Sign up for a Huuray B2B account](https://huuray.com/sign-up/) — if you do not have one yet
- [Contributing](.github/CONTRIBUTING.md) — including the note on **spec fidelity**: this client deliberately exposes nothing the API does not document, and that rule is enforced by tests
- [Changelog](CHANGELOG.md)

## Feedback

Found a bug, or something in this library that could be friendlier? Please [file an issue](https://github.com/Huuray-A-S/huuray-python/issues) or open a pull request.

For the API itself, your account, or a live production problem, contact your Huuray representative — see [SUPPORT.md](.github/SUPPORT.md) for which channel to use. Never open a public issue for a security vulnerability; see [SECURITY.md](.github/SECURITY.md).

## Code of Conduct

Please note that this project is released with a [Contributor Code of Conduct](.github/CODE_OF_CONDUCT.md). By contributing to this project, you agree to abide by its terms.

<p align="center">
  <img src="https://raw.githubusercontent.com/Huuray-A-S/huuray-python/main/.github/assets/huuray-logo.svg" width="96" alt="Huuray"/><br/>
  <sub>Made with 💚 in Denmark by <a href="https://huuray.com">Huuray A/S</a> · <a href="LICENSE">MIT</a></sub>
</p>
