# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Confirmed against the live API (2026-08-15)

Every assumption the specification left open has been verified with real calls:

- **`X-API-HASH` encoding is lowercase hex** — authenticated against
  `GET /v4/Balance`; the other three candidate encodings return 401. The default
  is pinned by a test; `hash_encoding` remains available as an override.
- **Base URL `https://api.huuray.com`** works for every endpoint exercised.
- **`POST /v4/Template` accepts a bodyless request**, as the spec implies.
- **The full order loop works end to end**: Balance → sync Order (quantity 1, no
  delivery) → Search by `RefID` (matched) → Cancel (full) → Balance.
- **An empty result set is signalled as HTTP 404**, not as an empty 200 —
  observed live on `/v4/Template` ("There were no active templates"). This is why
  the reconciliation examples treat `HuurayNotFoundError` from `/v4/Search` as
  "the order did not land".

## [0.1.0] — unreleased

First release. Complete coverage of the Huuray API v4.

### Added

- `HuurayClient` and `AsyncHuurayClient`, sharing one implementation of signing,
  the error taxonomy, the retry decision, and response interpretation.
- All nine v4 operations: balances, catalogue, templates, stock, exchange rates,
  orders (create, create_sync, search, resend, cancel).
- `send_reward()` — one gift card to one recipient in a single call.
- `request()` — an escape hatch that signs any call.
- Read-only CLI: `balance`, `catalogue`, `templates`, `stock`, `rates`, `search`.
- `redact()` and `safe_json()` for keeping voucher codes out of logs, understanding
  both raw response bodies and the dataclasses this SDK returns.
- Typed throughout, with a `py.typed` marker; `httpx` is the only runtime dependency.

### Safety behaviour worth calling out

- **Orders, resends and cancels are never retried automatically.** The API has no
  idempotency key, so a retry can order twice or re-deliver a live gift card.
  Retries are opt-in per operation and never inferred from the HTTP method —
  four read-only v4 endpoints are POSTs.
- A failed order raises `HuurayIndeterminateOrderError`, which points at
  `orders.search(ref_id=...)` for reconciliation and carries the `ref_id`.
- **The response body read happens inside the same error handling as the request**,
  so a connection dropped or timed out mid-body is wrapped rather than escaping as
  a raw `httpx` exception past the order-safety wrapper.
- **A 2xx with an empty or unparseable body raises `HuurayConnectionError`**
  instead of masquerading as an empty result — a garbled `/v4/Search` response
  must never read as "the order did not land".
- **Amounts must be integers in minor units.** Anything else is rejected rather
  than rounded, because rounding here is a 100× error. Python rejects `50.0`,
  which a JavaScript client cannot distinguish from `50`.
- **`206 Partial Content`** on cancel and resend is surfaced as `partial=True`
  rather than being treated as plain success.
- **Voucher codes are never logged** by this library at any level, and a `Voucher`
  masks its code, CVV and redeem link in `repr()`.
- Error objects retain only a **redacted** copy of the response body.
- **The CLI cannot move value.**

### Enforced mechanically

Three gates in [`tests/test_conformance.py`](tests/test_conformance.py) run on
every commit, reading the vendored specification at test time:

- **no-invention** — every request the SDK can emit maps to a spec path and verb,
  and sends no property the spec does not define
- **coverage** — every operation in the spec has an SDK method
- **request-conformance** — every request body validates against the spec schema

The validator **fails closed** on schema shapes it does not understand
(`allOf`/`oneOf`/`anyOf`, or a missing `type`), so a weekly spec refresh cannot
leave a vacuous gate green. A mechanical inventory of the public surface pins the
harness, so a new method cannot bypass the gates, and a parity test asserts the
async client emits byte-identical requests.

[Unreleased]: https://github.com/Huuray-A-S/huuray-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Huuray-A-S/huuray-python/releases/tag/v0.1.0
