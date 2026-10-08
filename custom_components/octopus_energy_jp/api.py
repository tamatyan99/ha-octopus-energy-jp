"""Async GraphQL API client for Octopus Energy Japan (Kraken)."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import API_URL
from .utils import RateTier, normalize_rates, select_latest_bill

_LOGGER = logging.getLogger(__name__)

# Kraken GraphQL extension codes (transport / auth semantics).
KT_CT_RATE_LIMITED = "KT-CT-1199"
KT_CT_TOKEN_EXPIRED = "KT-CT-1124"
KT_CT_INVALID_CREDENTIALS = "KT-CT-1138"

RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_BACKOFF_FACTOR = 2.0
RETRY_MAX_DELAY = 8.0
RETRY_AFTER_MAX = 8.0

_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class _RetryableRequestError(Exception):
    """Internal signal for transient failures eligible for bounded retry."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


# 認証切れを示す具体的な語のみ。bare "auth" は "author" 等に誤マッチするため除外。
# GraphQL errors の message 判定は小文字化＋単語境界ベースで行う。
AUTH_ERROR_HINTS = (
    "unauthorized",
    "unauthenticated",
    "jwt",
    "signature has expired",
    "token expired",
    "expired",
    "invalid token",
    "invalid credentials",
    "invalid email or password",
    "incorrect password",
    "incorrect username",
    "authentication failed",
    "access denied",
    "not authorized",
)


def _is_auth_error(message: str) -> bool:
    """Return True when a GraphQL error message indicates an auth failure."""
    lowered = message.lower()
    return any(
        re.search(r"\b" + re.escape(hint) + r"\b", lowered) for hint in AUTH_ERROR_HINTS
    )


# GraphQL extensions.code values that indicate an auth failure.
_AUTH_ERROR_CODES = frozenset({"UNAUTHENTICATED", "FORBIDDEN"})


def _has_auth_error_code(errors: Any) -> bool:
    """Return True when a GraphQL errors payload carries an auth failure code."""
    items = errors if isinstance(errors, (list, tuple)) else [errors]
    for item in items:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        if not isinstance(extensions, dict):
            continue
        code = extensions.get("code")
        if isinstance(code, str) and code.upper() in _AUTH_ERROR_CODES:
            return True
    return False


def _kraken_extension_codes(errors: Any) -> list[str]:
    """Return Kraken ``extensions.code`` values from a GraphQL errors payload."""
    codes: list[str] = []
    items = errors if isinstance(errors, (list, tuple)) else [errors]
    for item in items:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        if not isinstance(extensions, dict):
            continue
        code = extensions.get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


def _parse_retry_after_seconds(resp: aiohttp.ClientResponse) -> float | None:
    """Parse integer ``Retry-After`` header seconds, capped."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return float(min(seconds, RETRY_AFTER_MAX))


def _compute_retry_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter; honour ``Retry-After`` when provided."""
    if retry_after is not None:
        base = min(retry_after, RETRY_MAX_DELAY)
    else:
        base = min(
            RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1)),
            RETRY_MAX_DELAY,
        )
    return base * (0.5 + random.random() * 0.5)


def _raise_for_graphql_errors(errors: Any) -> None:
    """Map GraphQL errors to auth, retryable, or generic API failures."""
    codes = _kraken_extension_codes(errors)
    if KT_CT_INVALID_CREDENTIALS in codes or KT_CT_TOKEN_EXPIRED in codes:
        raise OctopusAuthError(str(errors))
    if KT_CT_RATE_LIMITED in codes:
        raise _RetryableRequestError(str(errors))
    message = str(errors)
    if _is_auth_error(message) or _has_auth_error_code(errors):
        raise OctopusAuthError(message)
    raise OctopusApiError(message)


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
query halfHourlyReadings(
  $accountNumber: String!, $fromDatetime: DateTime, $toDatetime: DateTime
) {
  account(accountNumber: $accountNumber) {
    properties {
      electricitySupplyPoints {
        halfHourlyReadings(fromDatetime: $fromDatetime, toDatetime: $toDatetime) {
          startAt
          endAt
          version
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

# 契約(agreement)の商品から基本料金・燃料費調整額・再エネ賦課金を取得する。
# product は union 型 (段階制 / 単一単価 / FIT)。FIT には料金項目がない。
SURCHARGE_FIELDS = """
  standingChargePricePerDay
  fuelCostAdjustment { pricePerUnitIncTax validFrom validTo }
  renewableEnergyLevy { pricePerUnitIncTax validFrom validTo }
"""

SURCHARGES_QUERY = (
    """
query surcharges($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    properties {
      electricitySupplyPoints {
        agreements {
          validFrom
          validTo
          product {
            __typename
            ... on ElectricitySteppedProduct {"""
    + SURCHARGE_FIELDS
    + """}
            ... on ElectricitySingleStepProduct {"""
    + SURCHARGE_FIELDS
    + """}
          }
        }
      }
    }
  }
}
"""
)

BILLS_QUERY = """
query bills($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    bills(first: 6, orderBy: ISSUED_DATE_DESC) {
      edges { node { billType fromDate toDate issuedDate } }
    }
  }
}
"""

# orderBy enum 名がスキーマと異なる場合のフォールバック (ソートはローカルで実施)
BILLS_QUERY_SIMPLE = """
query bills($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    bills(first: 6) {
      edges { node { billType fromDate toDate issuedDate } }
    }
  }
}
"""


def _parse_api_datetime(value: Any) -> datetime | None:
    """Parse a Kraken DateTime/Date string; naive values are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_active(item: dict[str, Any], now: datetime) -> bool:
    """Return True when validFrom <= now < validTo (missing bounds are open)."""
    valid_from = _parse_api_datetime(item.get("validFrom"))
    valid_to = _parse_api_datetime(item.get("validTo"))
    if valid_from is not None and valid_from > now:
        return False
    return valid_to is None or valid_to > now


def _pick_active(items: Any, now: datetime) -> dict[str, Any] | None:
    """Pick the currently valid entry from a dict or list of dicts."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None
    candidates = [item for item in items if isinstance(item, dict)]
    for item in candidates:
        if _is_active(item, now):
            return item
    # 期間外しかない場合は validTo 無しの最新を優先し、無ければ末尾
    for item in reversed(candidates):
        if item.get("validTo") is None:
            return item
    return candidates[-1] if candidates else None


def _price(item: dict[str, Any] | None) -> float | None:
    """Return pricePerUnitIncTax as float, or None when missing/invalid."""
    if not isinstance(item, dict):
        return None
    try:
        num = float(item.get("pricePerUnitIncTax"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


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
            {
                "query": AUTH_MUTATION,
                "variables": {
                    "input": {
                        "email": self._email,
                        "password": self._password,
                    }
                },
            },
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
                    sorted(str(key) for key in payload),
                )
            raise OctopusApiError("Unexpected auth response structure")
        self._token = token

    async def _async_post(
        self, body: dict[str, Any], authenticated: bool = True
    ) -> dict[str, Any]:
        last_retryable: _RetryableRequestError | None = None
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                return await self._async_post_once(body, authenticated=authenticated)
            except _RetryableRequestError as err:
                last_retryable = err
                if attempt >= RETRY_MAX_ATTEMPTS:
                    break
                _LOGGER.warning(
                    "Retrying API request (attempt %s/%s): %s",
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    err.reason,
                )
                await asyncio.sleep(_compute_retry_delay(attempt, err.retry_after))
        if last_retryable is not None:
            raise OctopusApiError(last_retryable.reason) from last_retryable
        raise OctopusApiError("Request failed after retries")

    async def _async_post_once(
        self, body: dict[str, Any], authenticated: bool = True
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if authenticated and self._token:
            headers["Authorization"] = f"JWT {self._token}"
        try:
            async with self._session.post(
                API_URL,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (401, 403):
                    raise OctopusAuthError(f"HTTP {resp.status}")
                if resp.status in _TRANSIENT_HTTP_STATUSES:
                    raise _RetryableRequestError(
                        f"HTTP {resp.status}",
                        retry_after=_parse_retry_after_seconds(resp),
                    )
                if resp.status != 200:
                    raise OctopusApiError(f"HTTP {resp.status}")
                try:
                    payload = await resp.json()
                except (aiohttp.ClientError, ValueError) as err:
                    raise OctopusApiError(
                        f"Failed to decode JSON response: {err}"
                    ) from err
        except OctopusApiError:
            raise
        except OctopusAuthError:
            raise
        except _RetryableRequestError:
            raise
        except asyncio.CancelledError:
            raise
        except KeyError:
            raise
        except TimeoutError as err:
            raise _RetryableRequestError(f"Connection error: {err}") from err
        except aiohttp.ClientError as err:
            raise _RetryableRequestError(f"Connection error: {err}") from err
        except ValueError as err:
            raise OctopusApiError(f"Connection error: {err}") from err
        except Exception as err:
            raise OctopusApiError(f"Unexpected API error: {err}") from err
        if not isinstance(payload, dict):
            raise OctopusApiError("Unexpected API response structure")
        errors = payload.get("errors")
        if errors:
            _raise_for_graphql_errors(errors)
        return payload

    async def _async_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        if not self._token:
            await self._async_authenticate()
        try:
            payload = await self._async_post(
                {"query": query, "variables": variables or {}}
            )
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
        data = await self._async_query(
            CONTRACT_QUERY, {"accountNumber": account_number}
        )
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
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return half-hourly readings for the period.

        Each dict contains ``startAt``/``endAt``/``version``/``value``.
        The caller is expected to split long periods into chunks
        (see utils.chunk_date_range). ``limit`` is kept for backward
        compatibility but currently unused: the Kraken endpoint does
        not accept a ``first`` argument on this field.
        """
        variables: dict[str, Any] = {
            "accountNumber": account_number,
            "fromDatetime": from_dt.isoformat(),
            "toDatetime": to_dt.isoformat(),
        }
        data = await self._async_query(READINGS_QUERY, variables)
        account = data.get("account")
        if not isinstance(account, dict):
            raise OctopusApiError("Unexpected readings response structure")
        supply_point = self._first_supply_point(account, "readings")
        readings = supply_point.get("halfHourlyReadings")
        if not isinstance(readings, list):
            raise OctopusApiError("Unexpected readings response structure")
        return readings

    async def async_get_latest_bill(self, account_number: str) -> dict[str, Any] | None:
        """Return the most recent bill period, or None when unavailable.

        Prefers STATEMENT/INVOICE bills and picks the latest one by
        issuedDate (the query asks for ISSUED_DATE_DESC, but the sort is
        redone locally so an unknown orderBy enum never breaks parsing).
        Returns None for empty/malformed responses without raising;
        transport/auth errors still raise OctopusApiError/OctopusAuthError.
        """
        try:
            data = await self._async_query(
                BILLS_QUERY, {"accountNumber": account_number}
            )
        except OctopusAuthError:
            raise
        except OctopusApiError as err:
            # orderBy enum 名の不一致等で失敗した場合は引数なしで再試行
            # (認証エラーは再認証フローに渡すため再試行しない)
            _LOGGER.debug("Bills query with orderBy failed, retrying simple: %s", err)
            data = await self._async_query(
                BILLS_QUERY_SIMPLE, {"accountNumber": account_number}
            )
        try:
            account = data.get("account")
            if not isinstance(account, dict):
                return None
            return select_latest_bill(account.get("bills"))
        except (TypeError, AttributeError):
            return None

    async def async_get_surcharges(
        self, account_number: str, now: datetime | None = None
    ) -> dict[str, float | None]:
        """Return the active agreement's standing charge and per-kWh surcharges.

        Keys: ``standing_charge_per_day`` (JPY/day), ``fuel_per_kwh`` and
        ``levy_per_kwh`` (JPY/kWh, tax included). A value is None when the
        API does not provide it (e.g. FIT products).
        """
        data = await self._async_query(
            SURCHARGES_QUERY, {"accountNumber": account_number}
        )
        account = data.get("account")
        if not isinstance(account, dict):
            raise OctopusApiError("Unexpected surcharges response structure")
        supply_point = self._first_supply_point(account, "surcharges")
        now = now or datetime.now(timezone.utc)
        agreement = _pick_active(supply_point.get("agreements"), now)
        product = agreement.get("product") if isinstance(agreement, dict) else None
        if not isinstance(product, dict):
            raise OctopusApiError("No active agreement product found")
        try:
            standing = float(product.get("standingChargePricePerDay"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            standing = None
        return {
            "standing_charge_per_day": standing
            if standing is not None and math.isfinite(standing)
            else None,
            "fuel_per_kwh": _price(_pick_active(product.get("fuelCostAdjustment"), now)),
            "levy_per_kwh": _price(_pick_active(product.get("renewableEnergyLevy"), now)),
        }

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
