"""Async GraphQL API client for Octopus Energy Japan (Kraken)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import aiohttp

from .const import API_URL
from .utils import RateTier, normalize_rates

_LOGGER = logging.getLogger(__name__)

# 認証切れを示す具体的な語のみ。bare "auth" は "author" 等に誤マッチするため除外。
# GraphQL errors の message 判定は小文字化＋単語境界ベースで行う。
AUTH_ERROR_HINTS = (
    "unauthorized",
    "unauthenticated",
    "jwt",
    "signature has expired",
    "token expired",
    "invalid token",
    "authentication failed",
)


def _is_auth_error(message: str) -> bool:
    """Return True when a GraphQL error message indicates an auth failure."""
    lowered = message.lower()
    return any(
        re.search(r"\b" + re.escape(hint) + r"\b", lowered)
        for hint in AUTH_ERROR_HINTS
    )

AUTH_MUTATION = """
mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) { token }
}
"""

ACCOUNT_QUERY = """
query accountViewer { viewer { accounts { number } } }
"""

CONTRACT_QUERY = """
query contractInfo($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    marketSupplyAgreements(first: 5) {
      edges { node { isActive product { code displayName } } }
    }
    properties {
      electricitySupplyPoints {
        spin
        contractedCapacity { value unit }
      }
    }
  }
}
"""

READINGS_QUERY = """
query halfHourlyReadings($accountNumber: String!, $fromDatetime: DateTime, $toDatetime: DateTime, $first: Int) {
  account(accountNumber: $accountNumber) {
    properties {
      electricitySupplyPoints {
        halfHourlyReadings(first: $first, fromDatetime: $fromDatetime, toDatetime: $toDatetime) {
          startAt
          value
        }
      }
    }
  }
}
"""

TARIFF_QUERY = """
query tariff($gridOperatorCode: String!, $productCode: String) {
  tariffSummary(gridOperatorCode: $gridOperatorCode, productCode: $productCode) {
    code
    displayName
    tiers {
      contractCapacityPattern
      consumptionRates { pricePerUnitIncTax stepStart stepEnd band }
    }
  }
}
"""


class OctopusApiError(Exception):
    """General API error."""


class OctopusAuthError(OctopusApiError):
    """Authentication failed or token rejected."""


class OctopusEnergyJpApiClient:
    """GraphQL client with lazy authentication and one re-auth retry."""

    def __init__(
        self, session: aiohttp.ClientSession, email: str, password: str
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None

    async def _async_authenticate(self) -> None:
        payload = await self._async_post(
            {"query": AUTH_MUTATION, "variables": {"input": {"email": self._email, "password": self._password}}},
            authenticated=False,
        )
        token: Any = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                node = data.get("obtainKrakenToken")
                if isinstance(node, dict):
                    token = node.get("token")
        if not isinstance(token, str) or not token:
            # トークン漏洩防止のためレスポンス全体はログ/例外文に含めない
            if isinstance(payload, dict):
                _LOGGER.debug(
                    "Unexpected auth response structure (top-level keys: %s)",
                    sorted(str(key) for key in payload.keys()),
                )
            raise OctopusApiError("Unexpected auth response structure")
        self._token = token

    async def _async_post(self, body: dict[str, Any], authenticated: bool = True) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if authenticated and self._token:
            headers["Authorization"] = f"JWT {self._token}"
        try:
            async with self._session.post(
                API_URL, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status in (401, 403):
                    raise OctopusAuthError(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise OctopusApiError(f"HTTP {resp.status}")
                try:
                    payload = await resp.json()
                except (aiohttp.ClientError, ValueError) as err:
                    raise OctopusApiError(f"Failed to decode JSON response: {err}") from err
        except OctopusApiError:
            raise
        except asyncio.CancelledError:
            raise
        except KeyError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise OctopusApiError(f"Connection error: {err}") from err
        except Exception as err:
            raise OctopusApiError(f"Unexpected API error: {err}") from err
        if not isinstance(payload, dict):
            raise OctopusApiError("Unexpected API response structure")
        errors = payload.get("errors")
        if errors:
            message = str(errors)
            if _is_auth_error(message):
                raise OctopusAuthError(message)
            raise OctopusApiError(message)
        return payload

    async def _async_query(
        self, query: str, variables: dict[str, Any] | None = None, _retried: bool = False
    ) -> dict[str, Any]:
        if not self._token:
            await self._async_authenticate()
        try:
            payload = await self._async_post({"query": query, "variables": variables or {}})
        except OctopusAuthError:
            if _retried:
                raise
            # トークン期限切れを想定して1回だけ再認証
            self._token = None
            await self._async_authenticate()
            return await self._async_query(query, variables, _retried=True)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OctopusApiError("Unexpected API response structure")
        return data

    @staticmethod
    def _first_supply_point(account: dict[str, Any], context: str) -> dict[str, Any]:
        """Return the first electricity supply point, or raise OctopusApiError."""
        properties = account.get("properties")
        if isinstance(properties, list):
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                points = prop.get("electricitySupplyPoints")
                if isinstance(points, list):
                    for point in points:
                        if isinstance(point, dict):
                            return point
        raise OctopusApiError(f"Unexpected {context} response structure")

    async def async_get_account_number(self) -> str:
        """Return the first account number; also serves as credential validation."""
        data = await self._async_query(ACCOUNT_QUERY)
        viewer = data.get("viewer")
        accounts = viewer.get("accounts") if isinstance(viewer, dict) else None
        if not isinstance(accounts, list) or not accounts:
            raise OctopusApiError("No accounts found for this user")
        first = accounts[0]
        number = first.get("number") if isinstance(first, dict) else None
        if not isinstance(number, str) or not number:
            raise OctopusApiError("Unexpected account response structure")
        return number

    async def async_get_contract(self, account_number: str) -> dict[str, Any]:
        """Return active product and supply point info."""
        data = await self._async_query(CONTRACT_QUERY, {"accountNumber": account_number})
        account = data.get("account")
        if not isinstance(account, dict):
            raise OctopusApiError("Unexpected contract response structure")
        plan_name = None
        product_code = None
        agreements = account.get("marketSupplyAgreements")
        edges = agreements.get("edges") if isinstance(agreements, dict) else None
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                product = node.get("product")
                if node.get("isActive") and isinstance(product, dict):
                    plan_name = product.get("displayName")
                    product_code = product.get("code")
                    break
        supply_point = self._first_supply_point(account, "contract")
        spin = supply_point.get("spin")
        contracted = supply_point.get("contractedCapacity")
        capacity_unit = contracted.get("unit") if isinstance(contracted, dict) else ""
        return {
            "plan_name": plan_name,
            "product_code": product_code,
            "grid_operator_code": spin[:2] if isinstance(spin, str) else "",
            "capacity_unit": capacity_unit if isinstance(capacity_unit, str) else "",
        }

    async def async_get_readings(
        self,
        account_number: str,
        from_dt: datetime,
        to_dt: datetime,
        limit: int | None = 5000,
    ) -> list[dict[str, Any]]:
        """Return half-hourly readings for the period.

        The caller is expected to split long periods into chunks
        (see utils.chunk_date_range); ``limit`` caps readings per call.
        """
        variables: dict[str, Any] = {
            "accountNumber": account_number,
            "fromDatetime": from_dt.isoformat(),
            "toDatetime": to_dt.isoformat(),
        }
        if limit is not None:
            variables["first"] = limit
        data = await self._async_query(READINGS_QUERY, variables)
        account = data.get("account")
        if not isinstance(account, dict):
            raise OctopusApiError("Unexpected readings response structure")
        supply_point = self._first_supply_point(account, "readings")
        readings = supply_point.get("halfHourlyReadings")
        if not isinstance(readings, list):
            raise OctopusApiError("Unexpected readings response structure")
        return readings

    async def async_get_tariff_rates(
        self, grid_operator_code: str, product_code: str, capacity_unit: str
    ) -> list[RateTier]:
        """Return normalized tiered consumption rates for the contract."""
        data = await self._async_query(
            TARIFF_QUERY,
            {"gridOperatorCode": grid_operator_code, "productCode": product_code},
        )
        summaries = data.get("tariffSummary")
        if not isinstance(summaries, list) or not summaries:
            raise OctopusApiError("No tariff summary found")
        first_summary = summaries[0]
        tiers = first_summary.get("tiers") if isinstance(first_summary, dict) else None
        if not isinstance(tiers, list) or not tiers:
            raise OctopusApiError("Unexpected tariff response structure")
        unit = capacity_unit or ""
        tier: dict[str, Any] | None = None
        if unit:
            wanted = "TIERED_LOW" if "LESS_THAN" in unit else "TIERED_HIGH"
            matched = next(
                (
                    item
                    for item in tiers
                    if isinstance(item, dict)
                    and item.get("contractCapacityPattern") == wanted
                ),
                None,
            )
            if matched is None:
                # TIERED_HIGH に無言フォールバックせず tiers[0] を使う
                _LOGGER.debug(
                    "No tariff tier matches capacity_unit %r; using first tier",
                    capacity_unit,
                )
            else:
                tier = matched
        else:
            # capacity_unit が空の場合も TIERED_HIGH 決め打ちにせず tiers[0] を使う
            _LOGGER.debug("Empty capacity_unit; using first tariff tier")
        if tier is None:
            first_tier = tiers[0]
            if not isinstance(first_tier, dict):
                raise OctopusApiError("Unexpected tariff response structure")
            tier = first_tier
        raw_rates = tier.get("consumptionRates")
        if not isinstance(raw_rates, list):
            raise OctopusApiError("Unexpected tariff response structure")
        try:
            return normalize_rates(raw_rates)
        except ValueError as err:
            raise OctopusApiError(f"No usable consumption rates: {err}") from err
