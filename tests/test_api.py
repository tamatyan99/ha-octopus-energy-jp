"""Hermetic tests for the Kraken GraphQL API client (no network)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import aiohttp
import pytest

from custom_components.octopus_energy_jp.api import (
    OctopusApiError,
    OctopusAuthError,
    OctopusEnergyJpApiClient,
    _has_auth_error_code,
    _is_auth_error,
)

EMAIL = "user@example.com"
PASSWORD = "secret"


# ---------------------------------------------------------------------------
# Helpers: fake aiohttp session (async context manager protocol)
# ---------------------------------------------------------------------------


def _ok_response(status: int = 200, payload: dict | None = None) -> MagicMock:
    """Build a fake response with status/json()/text()."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=str(payload))
    return resp


def _context_manager_for(resp: MagicMock) -> MagicMock:
    """Wrap a fake response in an async context manager for session.post."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _client_with_posts(
    post_side_effect: list,
) -> tuple[OctopusEnergyJpApiClient, MagicMock]:
    """Build a client whose session.post replays the given CMs/exceptions.

    NOTE: ``post`` is a MagicMock (not AsyncMock) because the client uses
    ``async with session.post(...)`` and an AsyncMock call returns a plain
    coroutine which cannot serve as an async context manager.
    """
    session = MagicMock()
    session.post = MagicMock(side_effect=post_side_effect)
    return OctopusEnergyJpApiClient(session, EMAIL, PASSWORD), session


def _client_with_query(result: dict | None = None, error: Exception | None = None):
    """Build a client with _async_query mocked (parsing-level tests)."""
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    if error is not None:
        client._async_query = AsyncMock(side_effect=error)
    else:
        client._async_query = AsyncMock(return_value=result)
    return client


# ---------------------------------------------------------------------------
# 1. Auth-error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "invalid credentials",
        "INVALID CREDENTIALS",
        "incorrect password",
        "Incorrect Password",
        "incorrect username",
        "token expired",
        "Token Expired",
        "signature has expired",
        "Unauthorized",
        "UNAUTHENTICATED: invalid token",
        "jwt malformed",
        "invalid email or password",
        "authentication failed",
        "access denied",
        "Access Denied",
        "not authorized",
        "Not Authorized",
        "invalid token",
        "expired",
    ],
)
def test_is_auth_error_positive_variants(message: str) -> None:
    assert _is_auth_error(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit exceeded (429), please retry later",
        "Internal server error",
        "Something went wrong",
        "timeout while fetching readings",
        "author biography updated",
        "authorizing user profile",
        "",
    ],
)
def test_is_auth_error_negative_variants(message: str) -> None:
    assert _is_auth_error(message) is False


@pytest.mark.parametrize(
    "code", ["UNAUTHENTICATED", "unauthenticated", "FORBIDDEN", "forbidden"]
)
def test_has_auth_error_code_positive(code: str) -> None:
    assert _has_auth_error_code({"extensions": {"code": code}}) is True
    assert _has_auth_error_code([{"extensions": {"code": code}}]) is True


@pytest.mark.parametrize(
    "errors",
    [
        {"extensions": {"code": "BAD_REQUEST"}},
        {"extensions": {"code": "INTERNAL"}},
        [{"message": "boom"}],
        {"extensions": {}},
        {"extensions": None},
        {"extensions": {"code": 123}},
        {"extensions": {"code": None}},
        {"no_extensions": True},
        "just a string",
        None,
        [],
        [{"extensions": {"code": "BAD_REQUEST"}}, {"message": "nope"}],
        "UNAUTHENTICATED without structure",
    ],
)
def test_has_auth_error_code_negative_and_malformed(errors) -> None:
    assert _has_auth_error_code(errors) is False


def test_has_auth_error_code_mixed_list_detects_auth_entry() -> None:
    errors = [
        {"extensions": {"code": "BAD_REQUEST"}},
        {"extensions": {"code": "forbidden"}},
    ]
    assert _has_auth_error_code(errors) is True


# ---------------------------------------------------------------------------
# 2. Exception inheritance
# ---------------------------------------------------------------------------


def test_auth_error_is_an_api_error() -> None:
    assert issubclass(OctopusAuthError, OctopusApiError)
    assert isinstance(OctopusAuthError("nope"), OctopusApiError)


# ---------------------------------------------------------------------------
# 3. HTTP/GraphQL request path with mocked session
# ---------------------------------------------------------------------------


async def test_post_returns_well_formed_payload() -> None:
    payload = {"data": {"viewer": {"accounts": [{"number": "A-1"}]}}}
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(payload=payload))]
    )
    assert await client._async_post({"query": "q"}) == payload
    assert session.post.call_count == 1


async def test_post_sends_jwt_header_when_token_set() -> None:
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(payload={"data": {}}))]
    )
    client._token = "tok123"
    await client._async_post({"query": "q"})
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Authorization"] == "JWT tok123"


async def test_post_omits_auth_header_for_login() -> None:
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(payload={"data": {}}))]
    )
    client._token = "tok123"
    await client._async_post({"query": "q"}, authenticated=False)
    _, kwargs = session.post.call_args
    assert "Authorization" not in kwargs["headers"]


async def test_graphql_auth_message_raises_auth_error() -> None:
    payload = {"errors": [{"message": "Invalid credentials, please try again"}]}
    client, _ = _client_with_posts(
        [_context_manager_for(_ok_response(payload=payload))]
    )
    with pytest.raises(OctopusAuthError):
        await client._async_post({"query": "q"})


async def test_graphql_auth_code_raises_auth_error() -> None:
    payload = {"errors": [{"message": "denied", "extensions": {"code": "FORBIDDEN"}}]}
    client, _ = _client_with_posts(
        [_context_manager_for(_ok_response(payload=payload))]
    )
    with pytest.raises(OctopusAuthError):
        await client._async_post({"query": "q"})


async def test_graphql_non_auth_error_raises_api_error() -> None:
    payload = {"errors": [{"message": "Rate limit exceeded, slow down"}]}
    client, _ = _client_with_posts(
        [_context_manager_for(_ok_response(payload=payload))]
    )
    with pytest.raises(OctopusApiError) as exc_info:
        await client._async_post({"query": "q"})
    assert not isinstance(exc_info.value, OctopusAuthError)


@pytest.mark.parametrize("status", [401, 403])
async def test_post_http_auth_status_raises_auth_error(status: int) -> None:
    client, _ = _client_with_posts([_context_manager_for(_ok_response(status=status))])
    with pytest.raises(OctopusAuthError):
        await client._async_post({"query": "q"})


async def test_post_http_other_status_raises_api_error() -> None:
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(status=500))]
    )
    with pytest.raises(OctopusApiError) as exc_info:
        await client._async_post({"query": "q"})
    assert not isinstance(exc_info.value, OctopusAuthError)
    assert session.post.call_count == 1


async def test_post_json_decode_failure_raises_api_error() -> None:
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(side_effect=ValueError("No JSON"))
    client, _ = _client_with_posts([_context_manager_for(resp)])
    with pytest.raises(OctopusApiError):
        await client._async_post({"query": "q"})


async def test_post_client_error_on_json_raises_api_error() -> None:
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(side_effect=aiohttp.ClientError("bad body"))
    client, _ = _client_with_posts([_context_manager_for(resp)])
    with pytest.raises(OctopusApiError):
        await client._async_post({"query": "q"})


async def test_post_non_dict_payload_raises_api_error() -> None:
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=["not", "a", "dict"])
    client, _ = _client_with_posts([_context_manager_for(resp)])
    with pytest.raises(OctopusApiError):
        await client._async_post({"query": "q"})


async def test_post_transport_error_becomes_api_error() -> None:
    client, _ = _client_with_posts([aiohttp.ClientError("connection reset")])
    with pytest.raises(OctopusApiError):
        await client._async_post({"query": "q"})


async def test_post_timeout_becomes_api_error() -> None:
    client, _ = _client_with_posts([TimeoutError("timed out")])
    with pytest.raises(OctopusApiError):
        await client._async_post({"query": "q"})


async def test_post_unexpected_error_is_wrapped() -> None:
    client, _ = _client_with_posts([RuntimeError("weird")])
    with pytest.raises(OctopusApiError, match="Unexpected API error"):
        await client._async_post({"query": "q"})


async def test_post_cancelled_error_is_not_swallowed() -> None:
    client, _ = _client_with_posts([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await client._async_post({"query": "q"})


async def test_post_key_error_propagates_unwrapped() -> None:
    resp = MagicMock()
    type(resp).status = PropertyMock(side_effect=KeyError("status"))
    client, _ = _client_with_posts([_context_manager_for(resp)])
    with pytest.raises(KeyError):
        await client._async_post({"query": "q"})


async def test_query_retries_once_after_401_then_succeeds() -> None:
    auth_payload = {"data": {"obtainKrakenToken": {"token": "new-token"}}}
    data_payload = {"data": {"viewer": {"accounts": [{"number": "A-1"}]}}}
    client, session = _client_with_posts(
        [
            _context_manager_for(_ok_response(status=401)),
            _context_manager_for(_ok_response(payload=auth_payload)),
            _context_manager_for(_ok_response(payload=data_payload)),
        ]
    )
    client._token = "stale-token"
    assert await client._async_query("query { viewer }") == data_payload["data"]
    assert client._token == "new-token"
    assert session.post.call_count == 3


async def test_query_retries_once_after_401_then_raises() -> None:
    auth_payload = {"data": {"obtainKrakenToken": {"token": "new-token"}}}
    client, session = _client_with_posts(
        [
            _context_manager_for(_ok_response(status=401)),
            _context_manager_for(_ok_response(payload=auth_payload)),
            _context_manager_for(_ok_response(status=401)),
        ]
    )
    client._token = "stale-token"
    with pytest.raises(OctopusAuthError):
        await client._async_query("query { viewer }")
    assert session.post.call_count == 3


async def test_query_authenticates_lazily_when_no_token() -> None:
    auth_payload = {"data": {"obtainKrakenToken": {"token": "fresh"}}}
    data_payload = {"data": {"viewer": {"accounts": []}}}
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._async_post = AsyncMock(side_effect=[auth_payload, data_payload])
    assert await client._async_query("query { viewer }") == data_payload["data"]
    assert client._async_post.call_count == 2
    first_body = client._async_post.call_args_list[0].args[0]
    assert "obtainKrakenToken" in first_body["query"]


async def test_query_retries_once_via_mocked_post_then_raises() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._token = "old"

    async def _fake_auth() -> None:
        client._token = "renewed"

    client._async_authenticate = AsyncMock(side_effect=_fake_auth)
    client._async_post = AsyncMock(side_effect=OctopusAuthError("token expired"))
    with pytest.raises(OctopusAuthError):
        await client._async_query("query { viewer }")
    assert client._async_post.call_count == 2
    assert client._async_authenticate.call_count == 1


async def test_query_recovers_after_reauth_via_mocked_post() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._token = "old"
    client._async_authenticate = AsyncMock()

    async def _fake_auth() -> None:
        client._token = "new"

    client._async_authenticate.side_effect = _fake_auth
    client._async_post = AsyncMock(
        side_effect=[OctopusAuthError("expired"), {"data": {"viewer": {}}}]
    )
    assert await client._async_query("q") == {"viewer": {}}
    assert client._async_post.call_count == 2


async def test_query_rejects_non_dict_data() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._token = "tok"
    client._async_post = AsyncMock(return_value={"data": None})
    with pytest.raises(OctopusApiError):
        await client._async_query("q")


async def test_authenticate_stores_token() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._async_post = AsyncMock(
        return_value={"data": {"obtainKrakenToken": {"token": "abc"}}}
    )
    await client._async_authenticate()
    assert client._token == "abc"
    _, kwargs = client._async_post.call_args
    assert kwargs.get("authenticated") is False
    variables = client._async_post.call_args.args[0]["variables"]
    assert variables["input"] == {"email": EMAIL, "password": PASSWORD}


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {}},
        {"data": {"obtainKrakenToken": {}}},
        {"data": {"obtainKrakenToken": {"token": ""}}},
        {"data": {"obtainKrakenToken": {"token": None}}},
        {"data": None},
        {"unexpected": True},
    ],
)
async def test_authenticate_rejects_malformed_payload(payload: dict) -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._async_post = AsyncMock(return_value=payload)
    with pytest.raises(OctopusApiError):
        await client._async_authenticate()
    assert client._token is None


# ---------------------------------------------------------------------------
# 4. Data parsing
# ---------------------------------------------------------------------------


async def test_get_account_number_returns_first() -> None:
    client = _client_with_query({"viewer": {"accounts": [{"number": "A-123"}]}})
    assert await client.async_get_account_number() == "A-123"


@pytest.mark.parametrize(
    "data",
    [
        {"viewer": {"accounts": []}},
        {"viewer": {}},
        {"viewer": None},
        {},
        {"viewer": {"accounts": [{"number": ""}]}},
        {"viewer": {"accounts": [{"number": None}]}},
        {"viewer": {"accounts": ["not-a-dict"]}},
    ],
)
async def test_get_account_number_rejects_bad_payloads(data: dict) -> None:
    client = _client_with_query(data)
    with pytest.raises(OctopusApiError):
        await client.async_get_account_number()


def _contract_payload(active: bool = True) -> dict:
    return {
        "account": {
            "marketSupplyAgreements": {
                "edges": [
                    {
                        "node": {
                            "isActive": False,
                            "product": {"code": "OLD", "displayName": "Old"},
                        }
                    },
                    {
                        "node": {
                            "isActive": active,
                            "product": {"code": "P-PLAN", "displayName": "My Plan"},
                        }
                    },
                ]
            },
            "properties": [
                {
                    "electricitySupplyPoints": [
                        {
                            "spin": "1234567890",
                            "contractedCapacity": {
                                "value": 40,
                                "unit": "LESS_THAN_6KVA",
                            },
                        }
                    ]
                }
            ],
        }
    }


async def test_get_contract_parses_active_product() -> None:
    client = _client_with_query(_contract_payload())
    contract = await client.async_get_contract("A-123")
    assert contract["plan_name"] == "My Plan"
    assert contract["product_code"] == "P-PLAN"
    assert contract["grid_operator_code"] == "12"
    assert contract["capacity_unit"] == "LESS_THAN_6KVA"


async def test_get_contract_without_active_product_keeps_supply_info() -> None:
    client = _client_with_query(_contract_payload(active=False))
    contract = await client.async_get_contract("A-123")
    assert contract["plan_name"] is None
    assert contract["product_code"] is None
    assert contract["grid_operator_code"] == "12"


async def test_get_contract_rejects_missing_account() -> None:
    client = _client_with_query({"account": None})
    with pytest.raises(OctopusApiError):
        await client.async_get_contract("A-123")


async def test_get_contract_rejects_missing_supply_point() -> None:
    client = _client_with_query({"account": {"properties": []}})
    with pytest.raises(OctopusApiError):
        await client.async_get_contract("A-123")


def _readings_payload(values: list) -> dict:
    return {
        "account": {
            "properties": [
                {"electricitySupplyPoints": [{"halfHourlyReadings": values}]}
            ]
        }
    }


async def test_get_readings_returns_list() -> None:
    rows = [
        {
            "startAt": "2024-05-01T00:00:00+09:00",
            "endAt": "2024-05-01T00:30:00+09:00",
            "version": "1",
            "value": 0.4,
        }
    ]
    client = _client_with_query(_readings_payload(rows))
    assert (
        await client.async_get_readings(
            "A-1", datetime(2024, 5, 1), datetime(2024, 5, 2)
        )
        == rows
    )


async def test_get_readings_accepts_limit_kwarg() -> None:
    rows = [{"startAt": "2024-05-01T00:00:00+09:00", "value": 0.1}]
    client = _client_with_query(_readings_payload(rows))
    result = await client.async_get_readings(
        "A-1", datetime(2024, 5, 1), datetime(2024, 5, 2), limit=5
    )
    assert result == rows


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"account": None},
        {"account": {"properties": []}},
        {"account": {"properties": [{"electricitySupplyPoints": [{}]}]}},
    ],
)
async def test_get_readings_rejects_bad_payloads(data: dict) -> None:
    client = _client_with_query(data)
    with pytest.raises(OctopusApiError):
        await client.async_get_readings(
            "A-1", datetime(2024, 5, 1), datetime(2024, 5, 2)
        )


def _bills_payload() -> dict:
    return {
        "account": {
            "bills": {
                "edges": [
                    {
                        "node": {
                            "billType": "STATEMENT",
                            "fromDate": "2024-04-01",
                            "toDate": "2024-04-30",
                            "issuedDate": "2024-05-10",
                        }
                    }
                ]
            }
        }
    }


async def test_get_latest_bill_returns_period() -> None:
    client = _client_with_query(_bills_payload())
    bill = await client.async_get_latest_bill("A-1")
    assert bill is not None
    assert bill["from_date"] == "2024-04-01"
    assert bill["to_date"] == "2024-04-30"


async def test_get_latest_bill_falls_back_to_simple_query() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._async_query = AsyncMock(
        side_effect=[OctopusApiError("bad orderBy enum"), _bills_payload()]
    )
    bill = await client.async_get_latest_bill("A-1")
    assert bill is not None
    assert bill["from_date"] == "2024-04-01"
    assert client._async_query.call_count == 2


async def test_get_latest_bill_reraises_auth_error_without_fallback() -> None:
    client = OctopusEnergyJpApiClient(MagicMock(), EMAIL, PASSWORD)
    client._async_query = AsyncMock(side_effect=OctopusAuthError("expired"))
    with pytest.raises(OctopusAuthError):
        await client.async_get_latest_bill("A-1")
    assert client._async_query.call_count == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"account": None},
        {"account": {}},
        {"account": {"bills": {"edges": []}}},
        {"account": {"bills": None}},
    ],
)
async def test_get_latest_bill_returns_none_for_empty_malformed(data: dict) -> None:
    client = _client_with_query(data)
    assert await client.async_get_latest_bill("A-1") is None


def _tariff_payload() -> dict:
    return {
        "tariffSummary": [
            {
                "code": "SUMMARY",
                "tiers": [
                    {
                        "contractCapacityPattern": "TIERED_LOW",
                        "consumptionRates": [
                            {
                                "pricePerUnitIncTax": 20.0,
                                "stepStart": 0,
                                "stepEnd": 120,
                                "band": "low",
                            },
                            {
                                "pricePerUnitIncTax": 25.0,
                                "stepStart": 120,
                                "stepEnd": None,
                                "band": "low",
                            },
                        ],
                    },
                    {
                        "contractCapacityPattern": "TIERED_HIGH",
                        "consumptionRates": [
                            {
                                "pricePerUnitIncTax": 30.0,
                                "stepStart": 0,
                                "stepEnd": None,
                                "band": "high",
                            }
                        ],
                    },
                ],
            }
        ]
    }


async def test_get_tariff_rates_selects_low_tier() -> None:
    client = _client_with_query(_tariff_payload())
    assert await client.async_get_tariff_rates("12", "P", "LESS_THAN_6KVA") == [
        (0.0, 120.0, 20.0),
        (120.0, None, 25.0),
    ]


async def test_get_tariff_rates_selects_high_tier() -> None:
    client = _client_with_query(_tariff_payload())
    assert await client.async_get_tariff_rates("12", "P", "MORE_THAN_6KVA") == [
        (0.0, None, 30.0)
    ]


async def test_get_tariff_rates_empty_unit_uses_first_tier() -> None:
    client = _client_with_query(_tariff_payload())
    assert await client.async_get_tariff_rates("12", "P", "") == [
        (0.0, 120.0, 20.0),
        (120.0, None, 25.0),
    ]


async def test_get_tariff_rates_unknown_unit_falls_back_to_first_tier() -> None:
    payload = {
        "tariffSummary": [
            {
                "tiers": [
                    {
                        "contractCapacityPattern": "OTHER",
                        "consumptionRates": [
                            {"pricePerUnitIncTax": 9.0, "stepStart": 0, "stepEnd": None}
                        ],
                    }
                ]
            }
        ]
    }
    client = _client_with_query(payload)
    assert await client.async_get_tariff_rates("12", "P", "SOMETHING") == [
        (0.0, None, 9.0)
    ]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"tariffSummary": []},
        {"tariffSummary": None},
        {"tariffSummary": [{"tiers": []}]},
        {"tariffSummary": [{"tiers": None}]},
        {"tariffSummary": ["not-a-dict"]},
        {"tariffSummary": [{"tiers": ["not-a-dict"]}]},
        {"tariffSummary": [{"tiers": [{"consumptionRates": None}]}]},
        {"tariffSummary": [{"tiers": [{"consumptionRates": []}]}]},
    ],
)
async def test_get_tariff_rates_rejects_bad_payloads(data: dict) -> None:
    client = _client_with_query(data)
    with pytest.raises(OctopusApiError):
        await client.async_get_tariff_rates("12", "P", "")


# ---------------------------------------------------------------------------
# 5. _first_supply_point
# ---------------------------------------------------------------------------


def test_first_supply_point_returns_first() -> None:
    account = {
        "properties": [
            {"electricitySupplyPoints": [{"spin": "first"}, {"spin": "second"}]},
            {"electricitySupplyPoints": [{"spin": "third"}]},
        ]
    }
    assert OctopusEnergyJpApiClient._first_supply_point(account, "readings") == {
        "spin": "first"
    }


def test_first_supply_point_skips_non_dict_entries() -> None:
    account = {
        "properties": [
            "not-a-dict",
            {"electricitySupplyPoints": ["nope", {"spin": "found"}]},
        ]
    }
    assert OctopusEnergyJpApiClient._first_supply_point(account, "contract") == {
        "spin": "found"
    }


@pytest.mark.parametrize(
    "account",
    [
        {},
        {"properties": []},
        {"properties": None},
        {"properties": "not-a-list"},
        {"properties": [{"electricitySupplyPoints": []}]},
        {"properties": [{"electricitySupplyPoints": None}]},
        {"properties": [{"electricitySupplyPoints": ["nope"]}]},
        {"properties": [{"noPoints": True}]},
    ],
)
def test_first_supply_point_raises_without_points(account: dict) -> None:
    with pytest.raises(OctopusApiError):
        OctopusEnergyJpApiClient._first_supply_point(account, "readings")
