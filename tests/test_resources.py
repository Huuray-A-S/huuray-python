"""The read-only resources: what they send, and how they map what comes back."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from huuray import HuurayNotFoundError

from .helpers import MockResponse, make_async_client, make_client


class TestBalancesList:
    def test_maps_balance_rows_and_keeps_amounts_in_minor_units(self):
        client, calls = make_client(
            MockResponse(
                json={
                    "Balances": [
                        {"Currency": "DKK", "Balance": 50_000, "Master": True},
                        {"Currency": "EUR", "Balance": 1234, "Master": False},
                    ]
                }
            )
        )
        result = client.balances.list()

        assert calls[0].method == "GET"
        assert calls[0].path == "/v4/Balance"
        assert calls[0].body_omitted is True
        assert [(b.currency, b.balance, b.master) for b in result.balances] == [
            ("DKK", 50_000, True),
            ("EUR", 1234, False),
        ]

    def test_returns_an_empty_list_when_the_api_sends_null(self):
        client, _ = make_client(MockResponse(json={"Balances": None}))
        assert client.balances.list().balances == []


class TestCatalogueList:
    def test_defaults_all_to_false_your_products_with_tokens_and_discount(self):
        client, calls = make_client(MockResponse(json={"Products": []}))
        client.catalogue.list()
        assert calls[0].body == {"All": False}

    def test_passes_all_through_when_requesting_the_whole_catalogue(self):
        client, calls = make_client(MockResponse(json={"Products": []}))
        client.catalogue.list(all=True)
        assert calls[0].body == {"All": True}

    def test_maps_product_fields(self):
        client, _ = make_client(
            MockResponse(
                json={
                    "Products": [
                        {
                            "ProductToken": "tok",
                            "BrandName": "Example",
                            "CountryCode": "DK",
                            "Discount": 4.5,
                            "Currency": "DKK",
                            "Active": True,
                        }
                    ]
                }
            )
        )
        product = client.catalogue.list().products[0]
        assert product.product_token == "tok"
        assert product.brand_name == "Example"
        assert product.country_code == "DK"
        assert product.discount == 4.5
        assert product.currency == "DKK"
        assert product.active is True


class TestTemplatesList:
    def test_sends_no_request_body_because_the_spec_declares_none(self):
        client, calls = make_client(MockResponse(json={"Templates": []}))
        client.templates.list()
        assert calls[0].method == "POST"
        assert calls[0].path == "/v4/Template"
        assert calls[0].body_omitted is True
        assert "content-type" not in calls[0].headers

    def test_maps_template_fields(self):
        client, _ = make_client(
            MockResponse(
                json={
                    "Templates": [{"Id": 42, "Name": "Default", "Type": "Email", "Language": "da"}]
                }
            )
        )
        template = client.templates.list().templates[0]
        assert (template.id, template.name, template.type, template.language) == (
            42,
            "Default",
            "Email",
            "da",
        )

    def test_an_account_with_no_templates_gets_a_404_not_an_empty_list(self):
        # Live-observed: the API answers 404 "There were no active templates".
        client, _ = make_client(
            MockResponse(
                status=404,
                json={"Status": 404, "StatusMessage": "There were no active templates"},
            )
        )
        with pytest.raises(HuurayNotFoundError, match="no active templates"):
            client.templates.list()


class TestStockCheck:
    def test_omits_value_when_not_supplied(self):
        client, calls = make_client(MockResponse(json={"Stock": 10}))
        client.stock.check(product_token="tok")
        assert calls[0].body == {"ProductToken": "tok"}

    def test_sends_value_when_supplied(self):
        client, calls = make_client(MockResponse(json={"Stock": 10}))
        assert client.stock.check(product_token="tok", value=5000).stock == 10
        assert calls[0].body == {"ProductToken": "tok", "Value": 5000}

    def test_rejects_a_non_integer_value_before_sending_anything(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="int in minor units"):
            # Deliberately the wrong type: the runtime guard is the thing under
            # test here, because a caller in an untyped codebase can reach it.
            client.stock.check(product_token="tok", value=50.5)  # type: ignore[arg-type]
        assert calls == []


class TestExchangeRatesGet:
    def test_sends_the_currencies_as_query_parameters(self):
        client, calls = make_client(MockResponse(json={"ExchangeRate": 7.46, "Spread": 2}))
        rate = client.exchange_rates.get(from_currency="EUR", to_currency="DKK")
        assert calls[0].method == "GET"
        assert calls[0].path == "/v4/ExchangeRates"
        assert calls[0].query == {"FromCurrency": "EUR", "ToCurrency": "DKK"}
        assert calls[0].body_omitted is True
        assert (rate.exchange_rate, rate.spread) == (7.46, 2)


class TestDateTimeHandling:
    def test_formats_a_datetime_for_the_specs_date_time_fields(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(
            product_token="tok",
            value=5000,
            currency="DKK",
            quantity=1,
            expires=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        assert calls[0].body["Product"]["Expires"] == "2027-01-01T00:00:00+00:00"

    def test_passes_a_preformatted_string_through_untouched(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "x"}))
        client.orders.create(
            product_token="tok",
            value=5000,
            currency="DKK",
            quantity=1,
            expires="2027-01-01T00:00:00Z",
        )
        assert calls[0].body["Product"]["Expires"] == "2027-01-01T00:00:00Z"


class TestAsyncResourceParity:
    async def test_every_read_resource_has_the_same_methods_on_both_clients(self):
        sync_client, _ = make_client()
        async_client, _ = make_async_client()

        def surface(resource: object) -> list[str]:
            return sorted(
                name
                for name in dir(type(resource))
                if not name.startswith("_") and callable(getattr(type(resource), name))
            )

        pairs = [
            (sync_client.balances, async_client.balances),
            (sync_client.catalogue, async_client.catalogue),
            (sync_client.templates, async_client.templates),
            (sync_client.stock, async_client.stock),
            (sync_client.exchange_rates, async_client.exchange_rates),
            (sync_client.orders, async_client.orders),
        ]
        for sync_resource, async_resource in pairs:
            assert surface(sync_resource) == surface(async_resource)

        await async_client.aclose()
        sync_client.close()

    async def test_the_async_resources_send_identical_requests(self):
        client, calls = make_async_client(MockResponse(json={"Products": []}))
        async with client:
            await client.catalogue.list(all=True)
            await client.stock.check(product_token="tok", value=5000)
            await client.exchange_rates.get(from_currency="EUR", to_currency="DKK")
            await client.templates.list()

        assert [(c.method, c.path) for c in calls] == [
            ("POST", "/v4/Catalogue"),
            ("POST", "/v4/Stock"),
            ("GET", "/v4/ExchangeRates"),
            ("POST", "/v4/Template"),
        ]
        assert calls[0].body == {"All": True}
        assert calls[1].body == {"ProductToken": "tok", "Value": 5000}
        assert calls[2].query == {"FromCurrency": "EUR", "ToCurrency": "DKK"}
        assert calls[3].body_omitted is True
