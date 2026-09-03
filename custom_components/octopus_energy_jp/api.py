"""Async GraphQL API client for Octopus Energy Japan (Kraken)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from .const import API_URL

_LOGGER = logging.getLogger(__name__)

# エラーメッセージ中の認証切れを示す手がかり（例: "Signature of the JWT has expired."）
AUTH_ERROR_HINTS = ("auth", "unauthorized", "jwt", "expired")

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
query halfHourlyReadings($accountNumber: String!, $fromDatetime: DateTime, $toDatetime: DateTime) {
  account(accountNumber: $accountNumber) {
    properties {
      electricitySupplyPoints {
        halfHourlyReadings(fromDatetime: $fromDatetime, toDatetime: $toDatetime) {
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
        try:
            self._token = payload["data"]["obtainKrakenToken"]["token"]
        except (KeyError, TypeError) as err:
            raise OctopusApiError(f"Unexpected auth response: {payload}") from err

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
                payload = await resp.json()
        except aiohttp.ClientError as err:
            raise OctopusApiError(f"Connection error: {err}") from err
        errors = payload.get("errors")
        if errors:
            message = str(errors)
            if any(hint in message.lower() for hint in AUTH_ERROR_HINTS):
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
        return payload["data"]

    async def async_get_account_number(self) -> str:
        """Return the first account number; also serves as credential validation."""
        data = await self._async_query(ACCOUNT_QUERY)
        accounts = data["viewer"]["accounts"]
        if not accounts:
            raise OctopusApiError("No accounts found for this user")
        return accounts[0]["number"]

    async def async_get_contract(self, account_number: str) -> dict[str, Any]:
        """Return active product and supply point info."""
        data = await self._async_query(CONTRACT_QUERY, {"accountNumber": account_number})
        account = data["account"]
        plan_name = None
        product_code = None
        for edge in account["marketSupplyAgreements"]["edges"]:
            node = edge["node"]
            if node.get("isActive") and node.get("product"):
                plan_name = node["product"].get("displayName")
                product_code = node["product"].get("code")
                break
        supply_point = account["properties"][0]["electricitySupplyPoints"][0]
        return {
            "plan_name": plan_name,
            "product_code": product_code,
            "grid_operator_code": (supply_point.get("spin") or "")[:2],
            "capacity_unit": (supply_point.get("contractedCapacity") or {}).get("unit", ""),
        }

    async def async_get_readings(
        self, account_number: str, from_dt: datetime, to_dt: datetime
    ) -> list[dict[str, Any]]:
        """Return half-hourly readings for the period."""
        data = await self._async_query(
            READINGS_QUERY,
            {
                "accountNumber": account_number,
                "fromDatetime": from_dt.isoformat(),
                "toDatetime": to_dt.isoformat(),
            },
        )
        return data["account"]["properties"][0]["electricitySupplyPoints"][0]["halfHourlyReadings"]

    async def async_get_tariff_rates(
        self, grid_operator_code: str, product_code: str, capacity_unit: str
    ) -> list[dict[str, Any]]:
        """Return tiered consumption rates for the contract."""
        data = await self._async_query(
            TARIFF_QUERY,
            {"gridOperatorCode": grid_operator_code, "productCode": product_code},
        )
        summaries = data["tariffSummary"]
        if not summaries:
            raise OctopusApiError("No tariff summary found")
        tiers = summaries[0]["tiers"]
        wanted = "TIERED_LOW" if "LESS_THAN" in capacity_unit else "TIERED_HIGH"
        tier = next((t for t in tiers if t["contractCapacityPattern"] == wanted), tiers[0])
        return tier["consumptionRates"]
