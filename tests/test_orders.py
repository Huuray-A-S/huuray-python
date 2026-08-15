"""The parts that move money: minor units, sync vs async, indeterminate orders, 206."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from huuray import (
    SYNC_QUANTITY_LIMIT,
    HuurayIndeterminateOrderError,
    HuurayServerError,
    HuurayValidationError,
    Recipient,
)

from .helpers import MockResponse, make_async_client, make_client

#: A valid minimum order, splatted as **BASE and overridden per test.
#:
#: Typed as Any-valued on purpose: several tests below deliberately override an
#: entry with the WRONG type (a float value, an over-limit quantity) to prove the
#: guard rejects it before anything is sent. Inferred as dict[str, object] this
#: would be 200-odd type errors at every call site instead.
BASE: dict[str, Any] = {
    "product_token": "tok",
    "value": 5000,
    "currency": "DKK",
    "quantity": 1,
}


class TestMinorUnits:
    def test_rejects_a_fractional_value_before_sending_anything(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="int in minor units"):
            client.orders.create(**{**BASE, "value": 50.0001})
        assert calls == []

    def test_rejects_a_whole_number_float_too_python_catches_what_javascript_cannot(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="int in minor units"):
            client.orders.create(**{**BASE, "value": 50.00})
        assert calls == []

    def test_rejects_a_bool_because_true_is_an_int_in_python(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="int in minor units"):
            client.orders.create(**{**BASE, "value": True})
        assert calls == []

    def test_explains_the_real_failure_major_units_order_one_hundredth(self):
        client, _ = make_client()
        with pytest.raises(ValueError, match=r"1/100th of the intended amount"):
            client.orders.create(**{**BASE, "value": 50.5})

    def test_admits_the_mixup_no_guard_can_catch(self):
        client, _ = make_client()
        with pytest.raises(ValueError, match="no guard can catch every mixup"):
            client.orders.create(**{**BASE, "value": 50.5})

    def test_sends_the_integer_through_untouched(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(**{**BASE, "value": 5000})
        assert calls[0].body["Product"]["Value"] == 5000

    def test_rejects_a_non_positive_quantity(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="positive int"):
            client.orders.create(**{**BASE, "quantity": 0})
        assert calls == []


class TestSyncVersusAsyncOrdering:
    def test_create_sends_sync_false(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(**BASE)
        assert calls[0].body["Sync"] is False

    def test_create_sync_sends_sync_true(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x", "Vouchers": []}))
        client.orders.create_sync(**BASE)
        assert calls[0].body["Sync"] is True

    def test_create_sync_enforces_the_documented_25_code_limit(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="limited to 25"):
            client.orders.create_sync(**{**BASE, "quantity": SYNC_QUANTITY_LIMIT + 1})
        assert calls == []

    def test_create_has_no_such_limit(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(**{**BASE, "quantity": 500})
        assert calls[0].body["Product"]["Quantity"] == 500

    def test_create_sync_returns_vouchers_and_create_does_not_expose_them(self):
        client, _ = make_client(
            MockResponse(
                json={
                    "OrderUID": "x",
                    "Vouchers": [
                        {
                            "ID": 1,
                            "Code": "ABC",
                            "RedeemLink": "https://r/1",
                            "Expires": "2027-01-01",
                        }
                    ],
                }
            )
        )
        result = client.orders.create_sync(**BASE)
        assert result.vouchers[0].id == 1
        assert result.vouchers[0].code == "ABC"
        assert result.vouchers[0].redeem_link == "https://r/1"
        # The asynchronous result type has no vouchers at all — reading them off
        # an async order is a mistake the type system should catch, not a
        # runtime surprise.
        assert not hasattr(client.orders.create(**BASE), "vouchers")

    def test_surfaces_blanked_codes_as_none_rather_than_pretending(self):
        # Codes come back empty unless ReturnCode is enabled on the account.
        client, _ = make_client(
            MockResponse(
                json={
                    "OrderUID": "x",
                    "Vouchers": [{"ID": 1, "Code": None, "CVV": None, "RedeemLink": None}],
                }
            )
        )
        voucher = client.orders.create_sync(**BASE).vouchers[0]
        assert voucher.code is None
        assert voucher.cvv is None
        assert voucher.redeem_link is None

    def test_a_voucher_never_prints_its_code(self):
        # repr() is what reaches log lines, tracebacks and debugger transcripts.
        client, _ = make_client(
            MockResponse(
                json={
                    "OrderUID": "x",
                    "Vouchers": [
                        {"ID": 1, "Code": "REAL-CODE", "CVV": "999", "RedeemLink": "https://r/1"}
                    ],
                }
            )
        )
        printed = repr(client.orders.create_sync(**BASE).vouchers[0])
        assert "REAL-CODE" not in printed
        assert "999" not in printed
        assert "https://r/1" not in printed
        assert "id=1" in printed


class TestRecipientValidationAsTheSpecStatesIt:
    def test_requires_recipients_when_a_delivery_template_is_set(self):
        client, _ = make_client()
        with pytest.raises(ValueError, match="recipients is required"):
            client.orders.create(**BASE, template_id=42)

    def test_accepts_exactly_one_recipient_for_a_multi_code_order(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(
            **{**BASE, "quantity": 5},
            template_id=42,
            recipients=[Recipient(email="a@example.com")],
        )
        assert len(calls[0].body["Recipients"]) == 1

    def test_accepts_a_recipient_count_matching_quantity(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(
            **{**BASE, "quantity": 2},
            template_id=42,
            recipients=[Recipient(email="a@example.com"), Recipient(email="b@example.com")],
        )
        assert len(calls[0].body["Recipients"]) == 2

    def test_rejects_a_count_that_is_neither_one_nor_quantity(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="either 1 entry or exactly quantity"):
            client.orders.create(
                **{**BASE, "quantity": 5},
                template_id=42,
                recipients=[Recipient(email="a@example.com"), Recipient(email="b@example.com")],
            )
        assert calls == []

    def test_allows_no_recipients_when_there_is_no_delivery_template(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(**BASE)
        assert "Recipients" not in calls[0].body

    def test_omits_recipient_fields_that_were_not_supplied(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(**BASE, template_id=42, recipients=[Recipient(email="a@b.test")])
        assert calls[0].body["Recipients"] == [{"Email": "a@b.test"}]


class TestSendReward:
    def test_makes_exactly_one_post_order_with_quantity_1_and_sync_false(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x", "RefID": "r"}))
        client.orders.send_reward(
            product_token="tok",
            value=5000,
            currency="DKK",
            recipient=Recipient(name="Jane", email="jane@example.com"),
            template_id=42,
            ref_id="payroll-2026-08-jane",
        )

        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert calls[0].path == "/v4/Order"
        body = calls[0].body
        assert body["Product"]["Quantity"] == 1
        assert body["Sync"] is False
        assert body["RefID"] == "payroll-2026-08-jane"
        assert len(body["Recipients"]) == 1

    def test_refuses_without_a_ref_id_and_never_generates_one(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="ref_id is required"):
            client.orders.send_reward(
                product_token="tok",
                value=5000,
                currency="DKK",
                recipient=Recipient(email="jane@example.com"),
                template_id=42,
                ref_id="",
            )
        assert calls == []

    def test_is_also_reachable_from_the_client_for_the_one_call_case(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.send_reward(
            product_token="tok",
            value=5000,
            currency="DKK",
            recipient=Recipient(email="jane@example.com"),
            template_id=42,
            ref_id="r-1",
        )
        assert len(calls) == 1


class TestIndeterminateOrders:
    def test_raises_when_the_connection_drops(self):
        client, _ = make_client(MockResponse(raises=httpx.ConnectError("socket hang up")))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(**BASE, ref_id="ref-9")

    def test_raises_when_the_connection_drops_mid_body_after_the_request_was_sent(self):
        # The regression that mattered most: a body-read failure escaping as a
        # raw transport exception would bypass this wrapper entirely — and a
        # consumer's generic retry handler would then re-order.
        client, _ = make_client(MockResponse(body_raises=httpx.ReadError("terminated")))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(**BASE, ref_id="ref-9")

    def test_raises_on_a_timeout_that_fires_while_the_response_body_streams(self):
        client, _ = make_client(MockResponse(body_raises=httpx.ReadTimeout("timed out")))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(**BASE, ref_id="ref-9")

    def test_raises_on_a_garbled_2xx_body_the_order_may_well_have_landed(self):
        client, _ = make_client(MockResponse(status=200, text="not json at all"))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(**BASE, ref_id="ref-9")

    def test_raises_on_a_5xx_too_the_server_may_still_have_processed_the_order(self):
        client, _ = make_client(MockResponse(status=500))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(**BASE, ref_id="ref-9")

    def test_carries_the_ref_id_so_the_caller_can_reconcile(self):
        client, _ = make_client(MockResponse(status=502))
        with pytest.raises(HuurayIndeterminateOrderError) as caught:
            client.orders.create(**BASE, ref_id="ref-9")
        assert caught.value.ref_id == "ref-9"
        assert "Do NOT retry" in str(caught.value)
        assert "ref-9" in str(caught.value)
        assert "orders.search" in str(caught.value)

    def test_keeps_the_underlying_failure_as_the_cause(self):
        client, _ = make_client(MockResponse(status=502))
        with pytest.raises(HuurayIndeterminateOrderError) as caught:
            client.orders.create(**BASE, ref_id="ref-9")
        assert caught.value.__cause__ is not None

    def test_says_so_plainly_when_no_ref_id_was_sent(self):
        client, _ = make_client(MockResponse(status=500))
        with pytest.raises(HuurayIndeterminateOrderError, match="No RefID was sent"):
            client.orders.create(**BASE)

    def test_does_not_mask_a_422_that_order_was_definitively_rejected(self):
        client, _ = make_client(
            MockResponse(status=422, json={"Status": 422, "StatusMessage": "bad"})
        )
        with pytest.raises(HuurayValidationError):
            client.orders.create(**BASE)

    def test_also_wraps_create_sync(self):
        client, _ = make_client(MockResponse(status=500))
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create_sync(**BASE, ref_id="ref-9")

    def test_also_wraps_send_reward(self):
        client, _ = make_client(MockResponse(status=500))
        with pytest.raises(HuurayIndeterminateOrderError) as caught:
            client.send_reward(
                product_token="tok",
                value=5000,
                currency="DKK",
                recipient=Recipient(email="jane@example.com"),
                template_id=42,
                ref_id="ref-send",
            )
        assert caught.value.ref_id == "ref-send"

    def test_wraps_only_ordering_not_resend_or_cancel(self):
        # A failed resend or cancel is its own problem: nothing new was ordered,
        # so the caller gets the server error rather than a reconciliation
        # instruction that would not apply.
        for call in (
            lambda c: c.orders.resend(order_uid="x"),
            lambda c: c.orders.cancel(order_uid="x"),
        ):
            client, _ = make_client(MockResponse(status=500))
            with pytest.raises(HuurayServerError):
                call(client)


class TestPartialSuccessOn206:
    def test_flags_a_partial_cancel_and_exposes_the_per_voucher_outcome(self):
        client, _ = make_client(
            MockResponse(
                status=206,
                json={
                    "OrderUID": "uid",
                    "OrderCancelled": False,
                    "Vouchers": [
                        {"ID": 1, "Cancelled": True},
                        {"ID": 2, "Cancelled": False},
                    ],
                },
            )
        )
        result = client.orders.cancel(order_uid="uid")
        assert result.partial is True
        assert result.order_cancelled is False
        assert [(v.id, v.cancelled) for v in result.vouchers] == [(1, True), (2, False)]

    def test_does_not_flag_a_clean_200_cancel_as_partial(self):
        client, _ = make_client(
            MockResponse(json={"OrderUID": "uid", "OrderCancelled": True, "Vouchers": []})
        )
        result = client.orders.cancel(order_uid="uid")
        assert result.partial is False
        assert result.order_cancelled is True

    def test_flags_a_partial_resend(self):
        client, _ = make_client(MockResponse(status=206, json={"NumberOfResends": 3}))
        result = client.orders.resend(order_uid="uid")
        assert result.number_of_resends == 3
        assert result.partial is True

    def test_cancel_is_a_delete_carrying_a_json_body(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "uid"}))
        client.orders.cancel(order_uid="uid", voucher_id=7)
        assert calls[0].method == "DELETE"
        assert calls[0].path == "/v4/Cancel"
        assert calls[0].body == {"OrderUID": "uid", "VoucherID": 7}


class TestSearch:
    def test_omits_every_parameter_that_was_not_supplied(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x", "Vouchers": []}))
        client.orders.search(ref_id="ref-1")
        assert calls[0].body == {"RefID": "ref-1"}

    def test_is_the_documented_way_to_reconcile_after_an_indeterminate_order(self):
        client, calls = make_client(
            MockResponse(json={"OrderUID": "uid-7", "RefID": "ref-9", "Vouchers": []})
        )
        found = client.orders.search(ref_id="ref-9")
        assert calls[0].method == "POST"
        assert calls[0].path == "/v4/Search"
        assert found.order_uid == "uid-7"

    def test_maps_the_recipient_on_a_matched_voucher(self):
        client, _ = make_client(
            MockResponse(
                json={
                    "OrderUID": "uid",
                    "Vouchers": [
                        {
                            "ID": 3,
                            "Recipient": {"Name": "Jane", "Email": "j@e.test", "RefID": "r-a"},
                        }
                    ],
                }
            )
        )
        recipient = client.orders.search(order_uid="uid").vouchers[0].recipient
        assert recipient == Recipient(name="Jane", email="j@e.test", ref_id="r-a")


class TestAsyncOrders:
    async def test_create_sends_the_same_body_as_the_sync_client(self):
        client, calls = make_async_client(MockResponse(json={"OrderUID": "x"}))
        async with client:
            await client.orders.create(**BASE, ref_id="ref-1")
        assert calls[0].path == "/v4/Order"
        assert calls[0].body["Sync"] is False
        assert calls[0].body["RefID"] == "ref-1"

    async def test_rejects_a_fractional_value_before_sending_anything(self):
        client, calls = make_async_client()
        async with client:
            with pytest.raises(ValueError, match="int in minor units"):
                await client.orders.create(**{**BASE, "value": 50.5})
        assert calls == []

    async def test_enforces_the_sync_quantity_cap(self):
        client, calls = make_async_client()
        async with client:
            with pytest.raises(ValueError, match="limited to 25"):
                await client.orders.create_sync(**{**BASE, "quantity": 26})
        assert calls == []

    async def test_raises_indeterminate_on_a_5xx(self):
        client, _ = make_async_client(MockResponse(status=500))
        async with client:
            with pytest.raises(HuurayIndeterminateOrderError) as caught:
                await client.orders.create(**BASE, ref_id="ref-9")
        assert caught.value.ref_id == "ref-9"

    async def test_raises_indeterminate_on_a_mid_body_drop(self):
        client, _ = make_async_client(MockResponse(body_raises=httpx.ReadError("terminated")))
        async with client:
            with pytest.raises(HuurayIndeterminateOrderError):
                await client.orders.create(**BASE, ref_id="ref-9")

    async def test_surfaces_206_as_partial(self):
        client, _ = make_async_client(
            MockResponse(status=206, json={"OrderUID": "uid", "Vouchers": [{"ID": 1}]})
        )
        async with client:
            result = await client.orders.cancel(order_uid="uid")
        assert result.partial is True

    async def test_send_reward_requires_a_ref_id(self):
        client, calls = make_async_client()
        async with client:
            with pytest.raises(ValueError, match="ref_id is required"):
                await client.send_reward(
                    product_token="tok",
                    value=5000,
                    currency="DKK",
                    recipient=Recipient(email="jane@example.com"),
                    template_id=42,
                    ref_id="",
                )
        assert calls == []
