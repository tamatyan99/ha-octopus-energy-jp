"""Constants for the Octopus Energy Japan integration."""
from datetime import timedelta

DOMAIN = "octopus_energy_jp"
API_URL = "https://api.oejp-kraken.energy/v1/graphql/"

CONF_ACCOUNT_NUMBER = "account_number"

# 請求期間オプション (options flow で設定、未設定時は 0.0 扱い)
CONF_BASIC_CHARGE_PER_DAY = "basic_charge_per_day"
CONF_FUEL_ADJUSTMENT_PER_KWH = "fuel_adjustment_per_kwh"
CONF_RENEWABLE_LEVY_PER_KWH = "renewable_levy_per_kwh"

UPDATE_INTERVAL = timedelta(hours=1)
# 契約情報・単価は変動が稀なので1日キャッシュ
STATIC_CACHE_TTL = timedelta(days=1)
# Octopus Japan の30分値は約8時間遅れで届く。統計投入は確定済み枠のみにする
STATS_IMPORT_BUFFER = timedelta(hours=8)

STATISTIC_ID_CONSUMPTION_PREFIX = f"{DOMAIN}:"
STORAGE_VERSION = 1
# 日次履歴ストアの保持上限（無限肥大防止）
MAX_DAILY_DAYS = 400
# メインセンサー属性 `daily` の最大日数
MAX_DAILY_ATTRIBUTE_DAYS = 180
