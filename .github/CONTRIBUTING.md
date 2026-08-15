# Contributing

Thanks for taking the time. Bug reports, documentation fixes and pull requests are all welcome.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting set up

```bash
git clone https://github.com/Huuray-A-S/huuray-python.git
cd huuray-python
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

No test touches the network.

| Command | What it does |
|---|---|
| `pytest` | run every test, including the three spec-fidelity gates |
| `mypy` | type-check `src`, `tests`, `examples` and `scripts` in strict mode |
| `ruff check .` | lint |
| `ruff format .` | format |
| `python scripts/fetch_spec.py` | re-download the live spec over `openapi/huuray-v4.json` |

## Spec fidelity — read this before adding anything

**This client exposes nothing the API does not document.** It is the rule the whole library is built on, and it is enforced by tests rather than by review.

In practice:

1. **Never send a field the spec does not define.** Not "just in case", not because another endpoint accepts it.
2. **Never call a path or verb the spec does not define.**
3. **Never depend on undocumented behaviour** — an undocumented status code, an undocumented header, an undocumented error shape. If the specification is silent, we confirm with Huuray before implementing. An unanswered question blocks the feature; it does not get a best guess.
4. **Field names mirror the spec, differing only in casing.** `OrderUID` becomes `order_uid`. It does not become `order_id`, `uid`, or anything more tasteful. Someone reading the Huuray API reference must be able to map it across without a translation table.
5. **Convenience methods are allowed, but only as a documented composition of real operations.** `send_reward()` is fine: it is exactly one `POST /v4/Order`, and its documentation says so.

Three tests in [`tests/test_conformance.py`](../tests/test_conformance.py) enforce this:

- **no-invention** — every request the SDK can emit maps to a path and verb in the spec, and sends no undefined property
- **coverage** — every operation in the spec has a method
- **request-conformance** — every request body validates against the spec schema

They work by calling every public method with every optional parameter populated, then checking what came out. **If you add a method, add it to `exercise_everything()` and to the `EXERCISED` inventory** — the inventory test fails otherwise, which is the point: a new method must never bypass the gates.

The validator deliberately **fails closed**. A schema shape it does not understand is an error, not a silent pass — extend `validate()` rather than loosening it.

## Sync and async

Every resource exists twice: `BalancesResource` and `AsyncBalancesResource`, and so on. Only the awaiting differs. Everything that can be *wrong* — the request body, the validation, the response mapping — lives in module-level functions the two share, so a fix cannot land in one and miss the other.

When you add or change a method, change both classes. A parity test asserts the two surfaces are identical and that they emit byte-identical requests.

## The vendored specification

`openapi/huuray-v4.json` is committed on purpose. A scheduled workflow re-downloads it weekly and opens a pull request if it changed, which is how we find out about API changes. Review every one of those PRs — do not merge on green alone.

## Tests

- **No live API calls, ever.** Ordering gift cards from a test runner spends real money. Inject a fake via the client's `transport` option; `tests/helpers.py` has one ready.
- **Fixtures contain invented data only.** Never record a real response.
- New behaviour needs a test that fails without your change.

## Money and value — extra care

Some of this library moves real money. Changes in these areas get closer review, and pull requests that weaken a guard will be asked to justify it:

- **Never add automatic retries to `/v4/Order`, `/v4/Resend` or `/v4/Cancel`.** There is no idempotency key. A retried order orders twice; a retried resend re-delivers a live gift card. Retries are opt-in per operation and must never be inferred from the HTTP method — four read-only v4 endpoints are POSTs.
- **Never let a transport failure escape the error taxonomy.** The response body read must stay inside the same handling as the request, or a mid-body drop bypasses `HuurayIndeterminateOrderError` entirely.
- **Never coerce an unreadable 2xx into an empty result.** A garbled `/v4/Search` response reading as "no order found" would make the documented reconciliation flow re-order.
- **Never widen the CLI to move value.** It is read-only on purpose.
- **Never log a voucher code**, at any level, in any code path. New fields carrying value or personal data go into `SECRET_FIELDS` or `SENSITIVE_FIELDS` in `src/huuray/redact.py`, with a test.
- **Keep amounts as integers in minor units.** No floats, no silent rounding.

## Pull requests

1. Branch from `main`.
2. Keep the change focused — one concern per pull request.
3. Make sure `pytest`, `mypy` and `ruff check .` all pass.
4. Describe what changed and why. If it touches ordering, say what you did about the points above.

## Reporting a bug

[Open an issue.](https://github.com/Huuray-A-S/huuray-python/issues) Include the SDK version, your Python version, what you called, what you expected, and what happened.

**Never paste an API token, an API secret, or a voucher code into an issue.** For a vulnerability, see [SECURITY.md](SECURITY.md) instead.

## What belongs somewhere else

Questions about the API itself, your account, pricing, or a live production problem go to your Huuray representative rather than here — see [SUPPORT.md](SUPPORT.md). We cannot resolve those from a GitHub issue.
