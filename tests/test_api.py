"""Hermetic tests for the Kraken GraphQL API client (no network)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiohttp
import pytest

from custom_components.octopus_energy_jp.api import (
    SURCHARGES_QUERY,
    OctopusApiError,
    OctopusAuthError,
    OctopusEnergyJpApiClient,
    _has_auth_error_code,
    _is_auth_error,
    _parse_api_datetime,
    _pick_active,
)

EMAIL = "user@example.com"
PASSWORD = "secret"


# ---------------------------------------------------------------------------
# Helpers: fake aiohttp session (async context manager protocol)
# ---------------------------------------------------------------------------


def _ok_response(
    status: int = 200,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a fake response with status/json()/text()."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=str(payload))
    resp.headers = headers or {}
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
        [_context_manager_for(_ok_response(status=404))]
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
    client, session = _client_with_posts([aiohttp.ClientError("connection reset")] * 3)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(OctopusApiError):
            await client._async_post({"query": "q"})
    assert session.post.call_count == 3


async def test_post_timeout_becomes_api_error() -> None:
    client, session = _client_with_posts([TimeoutError("timed out")] * 3)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(OctopusApiError):
            await client._async_post({"query": "q"})
    assert session.post.call_count == 3


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


# ---------------------------------------------------------------------------
# Transient retry / backoff
# ---------------------------------------------------------------------------


async def test_post_retries_once_on_429_then_succeeds() -> None:
    ok = {"data": {"viewer": {}}}
    client, session = _client_with_posts(
        [
            _context_manager_for(_ok_response(status=429)),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2
    assert sleep_mock.call_count == 1


async def test_post_exhausts_retries_on_persistent_503() -> None:
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(status=503))] * 3
    )
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(OctopusApiError, match="HTTP 503"):
            await client._async_post({"query": "q"})
    assert session.post.call_count == 3


async def test_post_honours_retry_after_header() -> None:
    ok = {"data": {}}
    client, session = _client_with_posts(
        [
            _context_manager_for(
                _ok_response(status=503, headers={"Retry-After": "4"})
            ),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2
    sleep_mock.assert_awaited_once()
    delay = sleep_mock.await_args.args[0]
    assert 2.0 <= delay <= 4.0


async def test_post_retries_on_timeout_then_succeeds() -> None:
    ok = {"data": {"ok": True}}
    client, session = _client_with_posts(
        [TimeoutError("timed out"), _context_manager_for(_ok_response(payload=ok))]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock):
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2


async def test_query_401_reauth_not_counted_as_transient_retry_budget() -> None:
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
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_query("query { viewer }") == data_payload["data"]
    assert session.post.call_count == 3
    sleep_mock.assert_not_awaited()


async def test_post_retries_on_kraken_rate_limit_graphql_code() -> None:
    rate_limited = {
        "errors": [
            "ignored",
            {
                "message": "Rate limited",
                "extensions": {"code": "KT-CT-1199"},
            },
        ]
    }
    ok = {"data": {"viewer": {}}}
    client, session = _client_with_posts(
        [
            _context_manager_for(_ok_response(payload=rate_limited)),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2
    assert sleep_mock.call_count == 1


@pytest.mark.parametrize("code", ["KT-CT-1138", "KT-CT-1124"])
async def test_post_kraken_auth_codes_raise_auth_error_without_retry(
    code: str,
) -> None:
    payload = {"errors": [{"message": "nope", "extensions": {"code": code}}]}
    client, session = _client_with_posts(
        [_context_manager_for(_ok_response(payload=payload))]
    )
    with pytest.raises(OctopusAuthError):
        await client._async_post({"query": "q"})
    assert session.post.call_count == 1


async def test_post_retry_after_invalid_header_uses_backoff() -> None:
    ok = {"data": {}}
    client, session = _client_with_posts(
        [
            _context_manager_for(
                _ok_response(status=503, headers={"Retry-After": "not-an-int"})
            ),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2
    sleep_mock.assert_awaited_once()


async def test_post_retry_after_negative_header_uses_backoff() -> None:
    ok = {"data": {}}
    client, session = _client_with_posts(
        [
            _context_manager_for(
                _ok_response(status=503, headers={"Retry-After": "-1"})
            ),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock):
        assert await client._async_post({"query": "q"}) == ok
    assert session.post.call_count == 2


async def test_post_retry_after_caps_large_values() -> None:
    ok = {"data": {}}
    client, session = _client_with_posts(
        [
            _context_manager_for(
                _ok_response(status=503, headers={"Retry-After": "120"})
            ),
            _context_manager_for(_ok_response(payload=ok)),
        ]
    )
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        assert await client._async_post({"query": "q"}) == ok
    delay = sleep_mock.await_args.args[0]
    assert delay <= 8.0


async def test_post_value_error_on_transport_raises_api_error() -> None:
    client, session = _client_with_posts([ValueError("socket glitch")])
    with pytest.raises(OctopusApiError, match="Connection error"):
        await client._async_post({"query": "q"})
    assert session.post.call_count == 1


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


async def test_get_contract_skips_malformed_agreement_edges() -> None:
    payload = _contract_payload()
    payload["account"]["marketSupplyAgreements"]["edges"].insert(
        0, {"node": "not-a-dict"}
    )
    client = _client_with_query(payload)
    contract = await client.async_get_contract("A-123")
    assert contract["plan_name"] == "My Plan"


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


NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)

# The module logger the surcharge parser warns on (caplog filter).
API_LOGGER = "custom_components.octopus_energy_jp.api"


def test_surcharges_query_uses_real_schema_field_names() -> None:
    """The document must match the OEJP schema and stay lean.

    A wrong field name only fails against the real API ("Cannot query field"),
    so the shape is pinned here instead of relying on mocked responses.
    """
    assert "query surcharges($accountNumber: String!)" in SURCHARGES_QUERY
    # There is no root-level ``surcharges`` field: the agreement data lives
    # under the account tree.
    assert "\n  surcharges" not in SURCHARGES_QUERY
    for name in (
        "account(accountNumber: $accountNumber)",
        "properties {",
        "electricitySupplyPoints {",
        "agreements {",
        "validFrom",
        "validTo",
        "product {",
        "__typename",
        "... on ElectricitySingleStepProduct",
        "... on ElectricitySteppedProduct",
        "... on ElectricityFitProduct { displayName }",
        "... on GasTieredProduct { displayName }",
        "standingChargePricePerDay",
        "fuelCostAdjustment { pricePerUnitIncTax validFrom validTo }",
        "renewableEnergyLevy { pricePerUnitIncTax validFrom validTo }",
    ):
        assert name in SURCHARGES_QUERY
    # Both charge-bearing fragments request the same real field names.
    assert SURCHARGES_QUERY.count("fuelCostAdjustment {") == 2
    assert SURCHARGES_QUERY.count("renewableEnergyLevy {") == 2
    for absent in (
        "onlyActive",  # agreement selection stays local (_pick_active)
        "fuelAdjustment",
        "renewableLevy",
        "basicCharge",
        "fuelGrossCost",
        "standingCharges",  # per-ampere rows are never requested or scaled
        "standingChargeUnitType",
        "unitType",
    ):
        assert absent not in SURCHARGES_QUERY


async def test_get_surcharges_field_error_raises_api_error() -> None:
    """A GraphQL validation error must fail loudly, never silently return 0.0."""
    payload = {
        "errors": [
            {
                "message": (
                    "Cannot query field 'fuelAdjustment' on type "
                    "'ElectricitySteppedProduct'."
                ),
                "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
            }
        ]
    }
    client, session = _client_with_posts(
        [
            _context_manager_for(
                _ok_response(payload={"data": {"obtainKrakenToken": {"token": "tok"}}})
            ),
            _context_manager_for(_ok_response(payload=payload)),
        ]
    )
    with pytest.raises(OctopusApiError) as exc_info:
        await client.async_get_surcharges("A-1", now=NOW)
    assert not isinstance(exc_info.value, OctopusAuthError)
    assert "Cannot query field" in str(exc_info.value)
    sent = session.post.call_args.kwargs["json"]["query"]
    assert sent == SURCHARGES_QUERY
    assert "fuelAdjustment" not in sent


def _surcharges_payload(agreements: list) -> dict:
    """Response path as the schema returns it: account → properties →
    electricitySupplyPoints → agreements → product."""
    return {
        "account": {
            "properties": [{"electricitySupplyPoints": [{"agreements": agreements}]}]
        }
    }


def _stepped_product(
    fuel: Any,
    levy: Any,
    standing: Any = "0.00",
    typename: str = "ElectricitySteppedProduct",
) -> dict:
    """A charge-bearing product (ProductInterface) with its real field names."""
    return {
        "__typename": typename,
        "standingChargePricePerDay": standing,
        "fuelCostAdjustment": fuel,
        "renewableEnergyLevy": levy,
    }


@pytest.mark.parametrize(
    "typename",
    ["ElectricitySteppedProduct", "ElectricitySingleStepProduct"],
)
async def test_get_surcharges_picks_active_agreement_and_rates(
    typename: str,
) -> None:
    agreements = [
        {
            "validFrom": "2025-01-01T00:00:00+09:00",
            "validTo": "2026-01-01T00:00:00+09:00",
            "product": _stepped_product(
                {"pricePerUnitIncTax": "9.99"},
                {"pricePerUnitIncTax": "9.99"},
                typename=typename,
            ),
        },
        {
            "validFrom": "2026-01-01T00:00:00+09:00",
            "validTo": None,
            "product": _stepped_product(
                [
                    {
                        "pricePerUnitIncTax": "-1.50",
                        "validFrom": "2026-09-01T00:00:00+09:00",
                        "validTo": "2026-10-01T00:00:00+09:00",
                    },
                    {
                        "pricePerUnitIncTax": "-1.20",
                        "validFrom": "2026-10-01T00:00:00+09:00",
                        "validTo": "2026-11-01T00:00:00+09:00",
                    },
                ],
                {"pricePerUnitIncTax": "4.18", "validFrom": "2026-05-01"},
                standing="295.24",
                typename=typename,
            ),
        },
    ]
    client = _client_with_query(_surcharges_payload(agreements))
    assert await client.async_get_surcharges("A-1", now=NOW) == {
        "standing_charge_per_day": 295.24,
        "fuel_per_kwh": -1.2,
        "levy_per_kwh": 4.18,
    }


@pytest.mark.parametrize(
    ("typename", "display_name"),
    [
        ("ElectricityFitProduct", "FIT sales"),
        ("GasTieredProduct", "Gas Flex"),
    ],
)
async def test_get_surcharges_non_electric_product_parses(
    typename: str, display_name: str, caplog
) -> None:
    """FIT/Gas products parse to absent values (no crash, no zeros).

    The name-only fragments let these union members be identified instead of
    collapsing to an opaque product.
    """
    agreements = [
        {
            "validFrom": None,
            "validTo": None,
            "product": {"__typename": typename, "displayName": display_name},
        }
    ]
    client = _client_with_query(_surcharges_payload(agreements))
    with caplog.at_level("DEBUG", logger=API_LOGGER):
        result = await client.async_get_surcharges("A-1", now=NOW)
    assert result == {
        "standing_charge_per_day": None,
        "fuel_per_kwh": None,
        "levy_per_kwh": None,
    }
    assert any(display_name in record.getMessage() for record in caplog.records)


async def test_get_surcharges_newer_agreement_supersedes_null_product() -> None:
    """An older open agreement without a product must not shadow the current one."""
    agreements = [
        {"validFrom": "2025-01-01", "validTo": None, "product": None},
        {
            "validFrom": "2026-01-01",
            "validTo": None,
            "product": {
                "__typename": "ElectricityFitProduct",
                "displayName": "FIT product",
            },
        },
    ]
    client = _client_with_query(_surcharges_payload(agreements))
    assert await client.async_get_surcharges("A-1", now=NOW) == {
        "standing_charge_per_day": None,
        "fuel_per_kwh": None,
        "levy_per_kwh": None,
    }


@pytest.mark.parametrize(
    ("standing", "expected"),
    [
        ("295.24", 295.24),
        ("  41.6  ", 41.6),
        (295, 295.0),
        (295.24, 295.24),
        ("-12.5", -12.5),
        ("0", 0.0),
        (None, None),
    ],
)
async def test_get_surcharges_accepts_decimal_able_values(
    standing: Any, expected: float | None
) -> None:
    agreements = [
        {
            "product": _stepped_product(
                {"pricePerUnitIncTax": "-1.2"},
                {"pricePerUnitIncTax": "4.18"},
                standing=standing,
            )
        }
    ]
    client = _client_with_query(_surcharges_payload(agreements))
    result = await client.async_get_surcharges("A-1", now=NOW)
    assert result["standing_charge_per_day"] == expected
    # A negative fuel adjustment is legitimate and must survive parsing.
    assert result["fuel_per_kwh"] == -1.2
    assert result["levy_per_kwh"] == 4.18


@pytest.mark.parametrize(
    ("fuel", "levy", "standing"),
    [
        ({"pricePerUnitIncTax": "NaN"}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": "nan"}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": "-inf"}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": "abc"}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": True}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": ["1.0"]}, {"pricePerUnitIncTax": "4.18"}, "295.24"),
        ({"pricePerUnitIncTax": "1.0"}, {"pricePerUnitIncTax": "abc"}, "295.24"),
        ({"pricePerUnitIncTax": "1.0"}, {"pricePerUnitIncTax": False}, "295.24"),
        ({"pricePerUnitIncTax": "1.0"}, {"pricePerUnitIncTax": "4.18"}, "inf"),
        ({"pricePerUnitIncTax": "1.0"}, {"pricePerUnitIncTax": "4.18"}, True),
        (
            {"pricePerUnitIncTax": "1.0"},
            {"pricePerUnitIncTax": "4.18"},
            {"pricePerUnitIncTax": "9.99"},
        ),
        ("junk", {"pricePerUnitIncTax": "4.18"}, "295.24"),
        (["junk"], {"pricePerUnitIncTax": "4.18"}, "295.24"),
    ],
)
async def test_get_surcharges_rejects_field_level_garbage(
    fuel: Any, levy: Any, standing: Any
) -> None:
    """Garbage charge fields raise so the coordinator keeps previous values.

    Silently zeroing a surcharge hides a broken field name from operators.
    """
    agreements = [{"product": _stepped_product(fuel, levy, standing=standing)}]
    client = _client_with_query(_surcharges_payload(agreements))
    with pytest.raises(OctopusApiError) as exc_info:
        await client.async_get_surcharges("A-1", now=NOW)
    assert "Unexpected surcharges response structure" in str(exc_info.value)


async def test_get_surcharges_absent_charge_fields_stay_none() -> None:
    """A product that resolved but carries no charge keys is not garbage."""
    agreements = [{"product": {"__typename": "ElectricitySteppedProduct"}}]
    client = _client_with_query(_surcharges_payload(agreements))
    assert await client.async_get_surcharges("A-1", now=NOW) == {
        "standing_charge_per_day": None,
        "fuel_per_kwh": None,
        "levy_per_kwh": None,
    }


async def test_get_surcharges_uses_per_day_standing_charge_verbatim() -> None:
    """standingChargePricePerDay (JPY/day) is used as-is; no amperage math."""
    product = _stepped_product(
        {"pricePerUnitIncTax": "1.0"},
        {"pricePerUnitIncTax": "4.18"},
        standing="295.24",
    )
    # Raw per-ampere rows never reach the parser via the query, but if a
    # response carries them they must not be converted into a daily charge.
    product["standingChargeUnitType"] = "DAYS_ON_SUPPLY"
    product["standingCharges"] = [
        {"pricePerUnitIncTax": "9.85", "validFrom": "2026-01-01", "validTo": None},
        {"pricePerUnitIncTax": "9.85", "validFrom": "2026-08-01", "validTo": None},
    ]
    client = _client_with_query(_surcharges_payload([{"product": product}]))
    result = await client.async_get_surcharges("A-1", now=NOW)
    assert result["standing_charge_per_day"] == 295.24
    assert result["standing_charge_per_day"] != 9.85


@pytest.mark.parametrize("unit_type", ["AMPERE_MONTHS", "DAYS_ON_SUPPLY", None])
async def test_get_surcharges_per_ampere_rows_alone_do_not_substitute(
    unit_type: str | None,
) -> None:
    """Without standingChargePricePerDay nothing is derived from the rows.

    The raw ``standingCharges`` list is per ampere per day; deriving a daily
    basic charge from it would need the amperage, so it stays None.
    """
    product = _stepped_product(
        {"pricePerUnitIncTax": "1.0"},
        {"pricePerUnitIncTax": "4.18"},
        standing=None,
    )
    product["standingChargeUnitType"] = unit_type
    product["standingCharges"] = [
        {"pricePerUnitIncTax": "9.85", "validFrom": "2026-01-01"},
        {"pricePerUnitIncTax": "9.85", "validFrom": "2026-08-01"},
    ]
    client = _client_with_query(_surcharges_payload([{"product": product}]))
    result = await client.async_get_surcharges("A-1", now=NOW)
    assert result["standing_charge_per_day"] is None
    assert result["fuel_per_kwh"] == 1.0


@pytest.mark.parametrize(
    "data",
    [
        {"account": None},
        {"account": {"properties": []}},
        _surcharges_payload([]),
        _surcharges_payload([{"validFrom": None, "product": None}]),
    ],
)
async def test_get_surcharges_rejects_bad_payloads(data: dict) -> None:
    client = _client_with_query(data)
    with pytest.raises(OctopusApiError):
        await client.async_get_surcharges("A-1", now=NOW)


def test_pick_active_prefers_entry_valid_now() -> None:
    expired = {"validFrom": "2025-01-01", "validTo": "2025-06-01", "id": "expired"}
    active = {"validFrom": "2026-01-01", "validTo": "2027-01-01", "id": "active"}
    future = {"validFrom": "2030-01-01", "validTo": None, "id": "future"}
    assert _pick_active([expired, future, active], NOW) is active
    assert _pick_active([future, active, expired], NOW) is active
    # A bare object (single-entry field) is handled the same way.
    assert _pick_active(active, NOW) is active


def test_pick_active_multiple_active_picks_most_recent() -> None:
    """A newer agreement supersedes an older open-ended one."""
    older_open = {"validFrom": "2020-01-01", "validTo": None, "id": "older-open"}
    newer = {
        "validFrom": "2026-09-10T09:00:00+09:00",
        "validTo": "2027-10-10T09:00:00+09:00",
        "id": "newer",
    }
    assert _pick_active([older_open, newer], NOW) is newer
    assert _pick_active([newer, older_open], NOW) is newer


def test_pick_active_open_bounds_count_as_active() -> None:
    open_both = {"validFrom": None, "validTo": None}
    assert _pick_active([open_both], NOW) is open_both
    open_end = {"validFrom": "2026-01-01", "validTo": None}
    assert _pick_active([open_end], NOW) is open_end


def test_pick_active_only_expired_picks_most_recent(caplog) -> None:
    older = {"validFrom": "2024-01-01", "validTo": "2024-06-01", "id": "older"}
    newer = {"validFrom": "2025-06-01", "validTo": "2025-07-01", "id": "newer"}
    with caplog.at_level("WARNING", logger=API_LOGGER):
        assert _pick_active([newer, older], NOW) is newer
    assert any(
        "most recent expired" in record.getMessage() for record in caplog.records
    )


def test_pick_active_only_future_picks_earliest_and_warns(caplog) -> None:
    later = {"validFrom": "2031-01-01", "validTo": None, "id": "later"}
    sooner = {"validFrom": "2030-01-01", "validTo": None, "id": "sooner"}
    with caplog.at_level("WARNING", logger=API_LOGGER):
        assert _pick_active([later, sooner], NOW) is sooner
    assert any(
        "has not started yet" in record.getMessage() for record in caplog.records
    )


def test_pick_active_mixed_expired_and_future_prefers_expired(caplog) -> None:
    """An expired rate beats a not-yet-started one (previous bug: future won)."""
    expired = {"validFrom": "2025-01-01", "validTo": "2025-02-01", "id": "expired"}
    future = {"validFrom": "2030-01-01", "validTo": None, "id": "future"}
    with caplog.at_level("WARNING", logger=API_LOGGER):
        assert _pick_active([future, expired], NOW) is expired
    assert not any(
        "has not started yet" in record.getMessage() for record in caplog.records
    )


def test_pick_active_expired_ignores_undated_entry() -> None:
    """An undated (unknown start) entry never outranks a dated expired one."""
    dated = {"validFrom": "2025-01-01", "validTo": "2025-02-01", "id": "dated"}
    undated = {"validFrom": None, "validTo": "2025-03-01", "id": "undated"}
    assert _pick_active([undated, dated], NOW) is dated
    assert _pick_active([undated], NOW) is undated


def test_pick_active_tie_break_prefers_latest_valid_to() -> None:
    closed = {"validFrom": "2025-01-01", "validTo": "2025-02-01", "id": "closed"}
    open_ended = {"validFrom": "2025-01-01", "validTo": None, "id": "open"}
    assert _pick_active([closed, open_ended], NOW) is open_ended
    assert _pick_active([open_ended, closed], NOW) is open_ended


def test_pick_active_full_tie_is_input_order_stable() -> None:
    first = {"validFrom": "2030-01-01", "validTo": None, "id": "first"}
    second = {"validFrom": "2030-01-01", "validTo": None, "id": "second"}
    assert _pick_active([first, second], NOW) is first
    assert _pick_active([second, first], NOW) is second


def test_pick_active_ignores_junk_and_handles_empty() -> None:
    assert _pick_active(["junk"], NOW) is None
    assert _pick_active([], NOW) is None
    assert _pick_active(None, NOW) is None
    assert _pick_active("nope", NOW) is None
    entry = {"validFrom": "2025-01-01", "validTo": "2025-02-01", "id": "ok"}
    assert _pick_active(["junk", entry], NOW) is entry


def test_parse_api_datetime_variants() -> None:
    assert _parse_api_datetime("2026-10-01") == datetime(2026, 10, 1, tzinfo=UTC)
    assert _parse_api_datetime("2026-10-01T00:00:00Z") == datetime(
        2026, 10, 1, tzinfo=UTC
    )
    assert _parse_api_datetime("not-a-date") is None
    assert _parse_api_datetime(None) is None


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


async def test_get_latest_bill_returns_none_when_parsing_raises() -> None:
    client = _client_with_query({"account": {"bills": "not-a-bills-object"}})
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
