# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Confirmed against the live API

Every assumption the specification left open has been verified with real calls
on 2026-08-15, unless another date is given:

- **`X-API-HASH` encoding is lowercase hex** — authenticated against
  `GET /v4/Balance`; the other three candidate encodings return 401. The default
  is pinned by a test; `hash_encoding` remains available as an override.
- **Base URL `https://api.huuray.com`** works for every endpoint exercised.
- **`POST /v4/Template` accepts a bodyless request**, as the spec implies.
- **The full order loop works end to end**: Balance → sync Order (quantity 1, no
  delivery) → Search by `RefID` (matched) → Cancel (full) → Balance.
- **`POST /v4/Template` answered HTTP 404** ("There were no active templates") for
  an account with no templates — observed live 2026-08-15. This is why the
  reconciliation examples treat `HuurayNotFoundError` from `/v4/Search` as
  "the order did not land".
- **An account with PDF templates but no email or SMS templates gets `200`** with
  an empty `Templates` list, and its PDF templates in `PDFTemplates` — observed live
  2026-09-16. `templates.list()` can therefore return an empty `templates` list as
  well as raise `HuurayNotFoundError`; handle both.

### Security

- `request()` rejects a method that is not an HTTP token, and a path that does not
  start with `/` or holds anything but visible ASCII, with `ValueError` before
  anything is sent. A path such as `@host/...`, `:8443/...` or `v4/...` was appended
  to the base URL and moved the signed request to another host or port; an invalid
  method failed at send time as a connection error quoting it, or a raw `TypeError`.
- `api_token` and `user_agent` holding a control character (line break, NUL, tab,
  DEL), a non-ASCII character, or a space at either end raise `HuurayConfigError`
  at construction, and a whitespace-only `api_token` counts as missing. A custom
  nonce like that, or an empty one, raises `ValueError` before sending. Before,
  such a value failed at send time as a connection error quoting the token or nonce
  (for an order, `HuurayIndeterminateOrderError`), raised a `UnicodeEncodeError`
  carrying it, or reached the wire; an empty nonce went out as an empty
  `X-API-NONCE`. The new errors never quote the value.
- `base_url` raises `HuurayConfigError` at construction when it holds:
  - a space, control character or non-ASCII character, which failed or was
    percent-encoded at request time;
  - user-info (`user@` or `user:password@`), which httpx sent to the host as Basic
    credentials on every request;
  - a query (`?`) or fragment (`#`): the request path was appended after it, so
    every request went to the wrong path;
  - an empty host, a port that is not a number from 1 to 65535, or a host that is
    not valid IDNA, which were accepted at construction (a non-numeric port or
    invalid IDNA then raised a raw `httpx.InvalidURL` or `idna.IDNAError` at the
    first request); or an unclosed IPv6 bracket, which raised a raw `ValueError` at
    construction.

  No `base_url` error quotes the value (it may hold a password) or chains a parser
  error; the error for a URL that is not absolute http(s) used to quote it.
- An `api_secret` holding an unpaired surrogate, which is what `os.environ` gives
  for an undecodable byte on POSIX, raises `HuurayConfigError` at construction.
  Before, every request raised a raw `UnicodeEncodeError` whose repr and args
  carried the secret and the nonce.
- `timeout` must be greater than 0 and at most 2147483.647 seconds, checked at
  construction. 0 or a negative value made an order that was never sent raise
  `HuurayIndeterminateOrderError`; `None` meant no timeout on both clients, and NaN
  or infinity on the async one; a larger value raised `OverflowError` on Windows
  and can wrap on Linux and macOS.

### Fixed

- The README Feedback section no longer invites pull requests, which this
  repository does not accept.
- The recipient-count guard is documented as applying when `template_id` is set,
  which is the only time it is checked.
- The templates docs no longer promise a 404 for an account without email or SMS
  templates; see the 2026-09-16 observation above.
- CONTRIBUTING, `scripts/fetch_spec.py` and the spec-drift workflow no longer say a
  spec change always opens a pull request: without a `SPEC_DRIFT_TOKEN` secret,
  Actions may not create one and the run fails instead.

## [0.1.0] — unreleased

First release. Complete coverage of the Huuray API v4.

### Added

- `HuurayClient` and `AsyncHuurayClient`, sharing one implementation of signing,
  the error taxonomy, the retry decision, and response interpretation.
- All nine v4 operations: balances, catalogue, templates, stock, exchange rates,
  orders (create, create_sync, search, resend, cancel).
- `send_reward()` — one gift card to one recipient in a single call.
- PDF delivery templates, added to the v4 specification: `templates.list()` returns
  `pdf_templates` (`PdfTemplate`: `uid`, `name`, `type`, `language`, `country`,
  `brand_name`) alongside `templates`, so an account whose templates are all PDF
  templates does not read as empty. `orders.create()`, `orders.create_sync()` and
  `send_reward()` accept an optional `pdf_template_uid`, sent as
  `DeliveryPDFTemplateUid`. It is rejected before any request unless `template_id`
  is also set; the API requires that template to be an email template.
- `request()` — an escape hatch that signs any call.
- Read-only CLI: `balance`, `catalogue`, `templates`, `stock`, `rates`, `search`.
- The CLI's `templates` command lists PDF templates as well as delivery templates,
  in both table and `--json` output.
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
