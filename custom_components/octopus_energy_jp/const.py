"""Constants for the Octopus Energy Japan integration."""
from datetime import timedelta

DOMAIN = "octopus_energy_jp"
API_URL = "https://api.oejp-kraken.energy/v1/graphql/"

CONF_ACCOUNT_NUMBER = "account_number"

UPDATE_INTERVAL = timedelta(hours=1)
# 契約情報・単価は変動が稀なので1日キャッシュ
STATIC_CACHE_TTL = timedelta(days=1)
# Octopus Japan の30分値は約8時間遅れで届く。統計投入は確定済み枠のみにする
STATS_IMPORT_BUFFER = timedelta(hours=8)

STATISTIC_ID_CONSUMPTION = f"{DOMAIN}:consumption"
STORAGE_VERSION = 1
