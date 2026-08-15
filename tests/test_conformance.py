"""Spec-fidelity gates.

The SDK's central promise is that it invents nothing: it calls only documented
operations and sends only documented fields. That promise has to be mechanical,
not a matter of discipline, or it quietly decays.

===================  ==========================================================
no-invention         every request the SDK makes exists in the specification
coverage             every operation in the specification has an SDK method
request-conformance  every request body validates against the spec schema,
                     including no unknown properties
===================  ==========================================================
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from huuray import AsyncHuurayClient, HuurayClient, Recipient

from .helpers import CapturedRequest, MockResponse, make_async_client, make_client

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "huuray-v4.json"
SPEC: dict[str, Any] = json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def spec_operations() -> set[str]:
    """``POST /v4/Order`` style keys for every operation the API documents."""
    return {
        f"{verb.upper()} {path}"
        for path, item in SPEC["paths"].items()
        for verb in item
        if verb != "parameters"
    }


def deref(schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.replace("#/components/schemas/", "")
    target = SPEC["components"]["schemas"].get(name)
    if target is None:
        raise AssertionError(f"Unresolvable $ref in spec: {ref}")
    return target


def validate(schema: dict[str, Any], value: Any, at: str = "$") -> list[str]:
    """Return human-readable violations; an empty list means the value conforms.

    FAILS CLOSED: a schema shape this validator does not understand is an error,
    never a silent pass. The spec-drift job re-downloads the live specification
    weekly — if a refresh starts using ``allOf`` wrappers (standard Swashbuckle
    output for nullable ``$ref``s) or drops ``type``, the gates must break loudly
    rather than validate nothing while staying green.
    """
    resolved = deref(schema)
    errors: list[str] = []

    if any(key in resolved for key in ("allOf", "oneOf", "anyOf")):
        return [
            f"{at}: schema uses allOf/oneOf/anyOf, which this validator does not handle — "
            "extend validate() before trusting this run"
        ]

    if value is None:
        if not resolved.get("nullable"):
            errors.append(f"{at}: null but the spec does not mark it nullable")
        return errors

    kind = resolved.get("type")

    if kind == "object":
        if not isinstance(value, dict):
            errors.append(f"{at}: expected object, got {type(value).__name__}")
            return errors
        properties: dict[str, Any] = resolved.get("properties", {})

        # The invention detector: a property the spec does not define.
        for key in value:
            if key not in properties:
                errors.append(
                    f"{at}.{key}: not defined in the spec — "
                    "the SDK must not send undocumented fields"
                )
        for required in resolved.get("required", []):
            if required not in value:
                errors.append(f"{at}.{required}: required by the spec but not sent")
        for key, sub in properties.items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{at}.{key}"))
        return errors

    if kind == "array":
        if not isinstance(value, list):
            errors.append(f"{at}: expected array, got {type(value).__name__}")
            return errors
        items = resolved.get("items")
        if not items:
            # Fail closed, like every other unhandled shape: an array with no
            # `items` would let every element through unvalidated, so the
            # no-invention check would silently not run on them.
            errors.append(
                f"{at}: array schema has no 'items' — this validator cannot check its "
                "elements; extend validate() before trusting this run"
            )
            return errors
        if items:
            for index, item in enumerate(value):
                errors.extend(validate(items, item, f"{at}[{index}]"))
        return errors

    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{at}: expected integer, got {value!r}")
        return errors

    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{at}: expected number, got {type(value).__name__}")
        return errors

    if kind == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{at}: expected boolean, got {type(value).__name__}")
        return errors

    if kind == "string":
        if not isinstance(value, str):
            errors.append(f"{at}: expected string, got {type(value).__name__}")
        return errors

    shape = 'no "type"' if kind is None else f'unknown type "{kind}"'
    return [
        f"{at}: schema has {shape} — this validator cannot check it; "
        "extend validate() before trusting this run"
    ]


# --------------------------------------------------------------- the harness

EXPIRES = datetime(2027, 1, 1, tzinfo=timezone.utc)
DELIVER_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def exercise_everything(client: Any) -> Any:
    """Call every public SDK method once, with every optional parameter populated.

    The gates below only inspect requests this function happens to make, so the
    inventory test further down pins the full public method list against it.

    Written once for both clients: ``client`` is either a ``HuurayClient``, in
    which case the calls return results, or an ``AsyncHuurayClient``, in which
    case they return coroutines that the async caller awaits in order.
    """
    return [
        client.balances.list(),
        client.catalogue.list(all=True),
        client.templates.list(),
        client.stock.check(product_token="tok", value=5000),
        client.exchange_rates.get(from_currency="DKK", to_currency="EUR"),
        client.orders.create(
            product_token="tok",
            value=5000,
            currency="DKK",
            quantity=2,
            expires=EXPIRES,
            ref_id="ref-1",
            template_id=42,
            delivery_datetime=DELIVER_AT,
            personal_message="Thank you",
            recipients=[
                Recipient(name="A", email="a@example.com", ref_id="r-a"),
                Recipient(name="B", phone="+4512345678", ref_id="r-b"),
            ],
        ),
        client.orders.create_sync(
            product_token="tok",
            value=5000,
            currency="DKK",
            quantity=1,
            expires=EXPIRES,
            ref_id="ref-sync",
            template_id=42,
            delivery_datetime=DELIVER_AT,
            personal_message="Thanks",
            recipients=[Recipient(name="C", email="c@example.com", ref_id="r-c")],
        ),
        client.orders.send_reward(
            product_token="tok",
            value=5000,
            currency="DKK",
            recipient=Recipient(name="Jane", email="jane@example.com"),
            template_id=42,
            ref_id="ref-2",
            personal_message="Nice work",
            expires="2027-01-01T00:00:00Z",
            delivery_datetime="2026-09-01T09:00:00Z",
        ),
        client.orders.search(
            order_uid="uid",
            voucher_id=7,
            product_token="tok",
            ref_id="ref-1",
            sms_template_id=1,
            email_template_id=2,
            delivery_datetime=DELIVER_AT,
            recipient_name="Jane",
            recipient_email="jane@example.com",
            recipient_phone="+4512345678",
            recipient_ref_id="r-a",
        ),
        client.orders.resend(order_uid="uid", voucher_id=7),
        client.orders.cancel(order_uid="uid", voucher_id=7),
    ]


@pytest.fixture(scope="module")
def calls() -> list[CapturedRequest]:
    client, captured = make_client(MockResponse(status=200, json={}))
    with client:
        exercise_everything(client)
    return captured


class TestNoInventionGate:
    def test_every_request_the_sdk_makes_is_a_documented_v4_operation(self, calls):
        documented = spec_operations()
        undocumented = {
            f"{call.method.upper()} {call.path}"
            for call in calls
            if f"{call.method.upper()} {call.path}" not in documented
        }
        assert undocumented == set()

    def test_every_query_parameter_the_sdk_sends_is_declared_in_the_spec(self, calls):
        for call in calls:
            if not call.query:
                continue
            operation = SPEC["paths"][call.path][call.method.lower()]
            declared = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "query"
            }
            for key in call.query:
                assert key in declared, (
                    f"{call.method} {call.path} sent undeclared query param {key!r}"
                )

    def test_the_sdk_reaches_no_host_but_the_documented_one(self, calls):
        assert {call.origin for call in calls} == {"https://api.huuray.com"}


class TestCoverageGate:
    def test_every_documented_v4_operation_has_an_sdk_method(self, calls):
        exercised = {f"{call.method.upper()} {call.path}" for call in calls}
        assert sorted(spec_operations() - exercised) == []

    def test_covers_exactly_the_nine_v4_operations_no_more_no_fewer(self):
        assert len(spec_operations()) == 9

    def test_the_spec_is_still_v4_this_client_targets_v4_only(self):
        assert SPEC["info"]["version"] == "v4"


class TestRequestConformanceGate:
    def test_every_request_body_validates_against_its_spec_schema(self, calls):
        failures: list[str] = []

        for call in calls:
            operation = SPEC["paths"][call.path][call.method.lower()]
            schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )

            if not schema:
                # The spec declares no body for this operation, so the SDK must
                # send none.
                if not call.body_omitted:
                    failures.append(
                        f"{call.method} {call.path}: spec declares no requestBody, "
                        "but the SDK sent one"
                    )
                continue

            failures.extend(validate(schema, call.body, f"{call.method} {call.path}"))

        assert failures == []

    def test_sends_no_body_to_post_v4_template_which_declares_none(self, calls):
        template_call = next(call for call in calls if call.path == "/v4/Template")
        assert template_call.body_omitted is True


class TestTheHarnessStaysLinkedToThePublicSurface:
    """The three gates only inspect what ``exercise_everything`` happens to call.

    This inventory pins the full public method list: adding a resource method
    without updating BOTH this list and ``exercise_everything()`` fails here, so
    a new method can never silently bypass the gates.
    """

    EXERCISED = {
        "BalancesResource": ["list"],
        "CatalogueResource": ["list"],
        "TemplatesResource": ["list"],
        "StockResource": ["check"],
        "ExchangeRatesResource": ["get"],
        "OrdersResource": [
            "cancel",
            "create",
            "create_sync",
            "resend",
            "search",
            "send_reward",
        ],
    }

    @staticmethod
    def surface(client: Any) -> dict[str, list[str]]:
        resources = [
            client.balances,
            client.catalogue,
            client.templates,
            client.stock,
            client.exchange_rates,
            client.orders,
        ]
        return {
            type(resource).__name__.removeprefix("Async"): sorted(
                name
                for name, attribute in vars(type(resource)).items()
                if not name.startswith("_") and callable(attribute)
            )
            for resource in resources
        }

    def test_every_public_resource_method_is_on_the_exercised_inventory(self):
        client, _ = make_client(MockResponse(status=200, json={}))
        with client:
            assert self.surface(client) == self.EXERCISED

    def test_the_async_client_exposes_exactly_the_same_surface(self):
        client, _ = make_async_client(MockResponse(status=200, json={}))
        assert self.surface(client) == self.EXERCISED

    def test_both_clients_expose_the_same_top_level_methods(self):
        def public(cls: type) -> set[str]:
            return {
                name
                for name in dir(cls)
                if not name.startswith("_") and callable(getattr(cls, name, None))
            }

        # close/aclose are the only intentional difference: the lifetime of an
        # async connection pool is awaited.
        assert public(HuurayClient) - {"close"} == public(AsyncHuurayClient) - {"aclose"}


class TestTheAsyncClientEmitsTheSameRequests:
    async def test_byte_for_byte_the_same_bodies_paths_and_verbs(self):
        sync_client, sync_calls = make_client(MockResponse(status=200, json={}))
        with sync_client:
            exercise_everything(sync_client)

        async_client, async_calls = make_async_client(MockResponse(status=200, json={}))
        async with async_client:
            for coroutine in exercise_everything(async_client):
                await coroutine

        assert [(c.method, c.path, c.query, c.body) for c in sync_calls] == [
            (c.method, c.path, c.query, c.body) for c in async_calls
        ]


class TestTheGatesThemselvesWork:
    def test_flags_an_undocumented_property(self):
        schema = SPEC["components"]["schemas"]["CancelRequest"]
        errors = validate(schema, {"OrderUID": "x", "Invented": True})
        assert any("Invented" in error and "not defined in the spec" in error for error in errors)

    def test_flags_a_missing_required_property(self):
        schema = SPEC["components"]["schemas"]["CancelRequest"]
        errors = validate(schema, {})
        assert any("OrderUID" in error and "required" in error for error in errors)

    def test_flags_a_wrong_type(self):
        schema = SPEC["components"]["schemas"]["StockRequest"]
        errors = validate(schema, {"ProductToken": "x", "Value": 1.5})
        assert any("Value" in error and "expected integer" in error for error in errors)

    def test_flags_a_bool_where_an_integer_is_expected(self):
        schema = SPEC["components"]["schemas"]["StockRequest"]
        errors = validate(schema, {"ProductToken": "x", "Value": True})
        assert any("expected integer" in error for error in errors)

    def test_fails_closed_on_a_composed_schema_it_does_not_understand(self):
        errors = validate({"allOf": [{"type": "object"}]}, {"anything": 1})
        assert any("does not handle" in error for error in errors)

    def test_fails_closed_on_a_schema_with_no_type(self):
        errors = validate({"description": "mystery"}, "anything")
        assert any('no "type"' in error for error in errors)

    def test_flags_a_null_where_the_spec_does_not_allow_one(self):
        errors = validate({"type": "string"}, None)
        assert any("nullable" in error for error in errors)

    def test_raises_on_an_unresolvable_ref_rather_than_passing_it(self):
        with pytest.raises(AssertionError, match="Unresolvable"):
            validate({"$ref": "#/components/schemas/NoSuchSchema"}, {})
