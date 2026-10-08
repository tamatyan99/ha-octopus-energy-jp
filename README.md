# Octopus Energy Japan — Home Assistant Custom Integration

[![hacs][hacs-badge]][hacs]
[![test][test-badge]][test]
[![HACS Action][hacs-action-badge]][hacs-action]
[![Hassfest][hassfest-badge]][hassfest]

オクトパスエナジー ジャパンの電力使用量・料金を Home Assistant に取り込むカスタム連携です。
Kraken GraphQL API から30分値・契約情報・料金表を取得し、日次/月次の使用量・料金センサーと
Energy Dashboard 用の外部統計を提供します。

> [!NOTE]
> This is an unofficial community integration. It is not affiliated with or endorsed by Octopus Energy.
> 非公式のコミュニティ連携です。Octopus Energy 公式とは関係ありません。

## 機能

- 前日/当日/当月の使用量 (kWh) と料金 (JPY) センサー
- 前日比較・前月比較センサー
- 現在の単価センサー（v0.4.0〜、段階制の限界単価＋燃料費調整額＋再エネ賦課金）
- 請求期間の使用量・料金センサー（v0.2.3〜、基本料金・燃料費調整額・再エネ賦課金の加算に対応）
- Energy Dashboard 連携（時間別の消費量と料金の外部統計を自動投入。料金統計は v0.4.0〜）
- 資格情報の再設定（v0.4.0〜、削除せずにメールアドレス・パスワードを変更）
- 段階制料金（グリーンオクトパス等）の料金計算
- 日次履歴の永続化（API保持期間より古い分も保持）
- 日英バイリンガル対応

## 前提と制限 / Requirements & Limitations

- Home Assistant 2025.4 以降が必要です（Requires HA 2025.4+。開発・動作確認は 2026.9 系）。
- `recorder` が必須です。無効化していると履歴・統計が記録されません。
- データ更新間隔は1時間です（Data refreshes hourly）。30分値は約8時間遅れで届きます。
- 段階制料金プラン（グリーンオクトパス等）に対応。従量単価は Kraken API の料金表から取得します。
- 複数契約がある場合、最初の供給地点のみ使用します（Only the first supply point is used）。
- Energy Dashboard に料金を表示するには、HA の通貨設定（設定 → 一般 → 通貨 / Currency）が `JPY` である必要があります。
- 不具合・要望は [Issues](https://github.com/tamatyan99/ha-octopus-energy-jp/issues) までお願いします。

## インストール

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tamatyan99&repository=ha-octopus-energy-jp&category=integration)

### HACS（カスタムリポジトリ）

1. HACS → 右上メニュー → **カスタムリポジトリ**
2. リポジトリに `https://github.com/tamatyan99/ha-octopus-energy-jp`、カテゴリに **連携** を選んで追加
3. HACS の連携一覧から **Octopus Energy Japan** をダウンロード
4. Home Assistant を再起動

### 手動インストール

1. `custom_components/octopus_energy_jp/` を Home Assistant の `config/custom_components/` 配下にコピー
2. Home Assistant を再起動

### アンインストール / Removal

1. **設定 → デバイスとサービス → Octopus Energy Japan → ⋮ → 削除** で統合エントリを削除します
   （統合が保存していた日次履歴・統計の永続データも同時に削除されます）。
2. HACS から入れた場合は HACS → 連携 → **Octopus Energy Japan** → ⋮ → **削除** でファイルを削除します。
3. Energy Dashboard の消費量に登録していた場合は、**設定 → ダッシュボード → エネルギー** から該当ソースを削除します。
4. 手動で入れた場合は `config/custom_components/octopus_energy_jp/` を削除して Home Assistant を再起動します。

## 設定

1. **設定 → デバイスとサービス → 統合を追加** → **Octopus Energy Japan** を検索
2. オクトパスエナジーのアカウントのメールアドレスとパスワードを入力
3. アカウント番号が自動取得され、センサーが作成されます

### オプション（請求期間の料金加算、v0.2.3〜）

デバイスページ → **設定**（統合エントリの歯車）から変更できます
（Configure via the device page → Settings）。

| 項目 | 単位 | 意味 | 既定値 |
|---|---|---|---|
| 基本料金 | 円/日 | 契約の基本料金（日割りで加算） | 未設定（API値） |
| 燃料費調整額 | 円/kWh | 燃料費調整単価（使用量に乗じて加算） | 未設定（API値） |
| 再エネ賦課金 | 円/kWh | 再生可能エネルギー発電促進賦課金単価（使用量に乗じて加算） | 未設定（API値） |

未設定の項目は、Kraken API の契約情報から取得した現在有効な値を使います（1日1回更新）。
API から取得できない項目（FIT 契約など）は 0 扱いです。
オプションを設定した項目は API 値より優先されます（0 を設定すると加算を無効化できます）。
これらの値は請求期間センサー（`billing_kwh` / `billing_cost`）・現在の単価・Energy Dashboard の料金統計に反映されます
（基本料金は請求期間センサーのみ）。

## センサー一覧

| センサー | 単位 | 説明 |
|---|---|---|
| 昨日の使用量 | kWh | 確定済みの前日使用量 |
| 当日の使用量 | kWh | 当日0時からの暫定使用量 |
| 当月の使用量 | kWh | 当月1日からの累計 |
| 前日比較 | kWh | 昨日 − 一昨日 |
| 前月の使用量 | kWh | 前月1か月分 |
| 前月比較 | kWh | 当月（昨日まで）− 前月の同日数分 |
| 昨日の料金 / 当日の料金 / 当月の料金 / 前月の料金 | JPY | 段階制料金による概算 |
| 請求期間の使用量 | kWh | 検針期間（billing）の累計使用量（センサーID: `billing_kwh`） |
| 請求期間の料金 | JPY | 検針期間の概算料金。オプション設定時は基本料金・燃料費調整額・再エネ賦課金を加算（センサーID: `billing_cost`） |
| 現在の単価 | JPY/kWh | 段階制の限界単価＋燃料費調整額＋再エネ賦課金（キー: `current_rate`）。属性に `tier_rate` / `next_tier_rate`（いずれも税込の段階単価で、サーチャージは含まず）/ `plan_name` / `month_kwh` |

（表中のカッコ内は連携内部のキーです。実際のエンティティIDは表示名から生成され、例: `sensor.octopus_energy_a_b00c43a0_yesterday_usage`、単価は `sensor.octopus_energy_<account>_current_unit_rate`）

`現在の単価` は当月の使用量から決まる**現在適用中の従量単価**（= `tier_rate` ＋ 燃料費調整額 ＋ 再エネ賦課金）で、
「単価が安い時間帯に家電を回す」といった自動化に使えます。料金表の改定や当月累計が
次の段階に入ると値が変わります。

`昨日の使用量`（`yesterday_kwh`）センサーの属性には `avg_rate`（平均単価）、`daily`（日別使用量・料金）、
`yesterday_series` / `today_series`（30分値系列）、`plan_name`、`last_update` も含まれます。

> [!NOTE]
> 30分値は約8時間遅れで届くため、当日値は暫定表示です。統計投入は確定済み時間枠のみ行います。
> 過去月の料金は現在の単価表による近似です（単価改定は考慮しません）。

> [!IMPORTANT]
> **v0.3.0 の破壊的変更**: v0.1 由来の重複センサー（`usage`）を削除しました。
> `昨日の使用量`（`yesterday_kwh`）と値が完全に同一のエンティティで、機能的な差はありません。
> 集約属性（`avg_rate` / `daily` / 30分値系列など）は `昨日の使用量` に引き継がれています。
> ダッシュボードや自動化が `usage` を参照していた場合は `昨日の使用量` に変更してください。

## Energy Dashboard

1. **設定 → ダッシュボード → エネルギー** を開きます
2. 電力網の **消費量を追加** → ソースの一覧から `Octopus Energy Japan consumption` を選択します
3. 複数の**口座番号**で連携している場合は、一覧に出る statistic_id（口座番号ごと。例: `octopus_energy_jp:a_b00c43a0_consumption` — 口座番号を小文字化し、英数字以外を `_` にした slug）から該当口座を選びます
   （One statistic per account number; pick the matching `octopus_energy_jp:<account_slug>_consumption` when you have several accounts）
   同一口座に複数の供給地点がある場合も statistic_id は1つです（連携は最初の供給地点のみ使用 — 前提と制限のとおり）。
4. 料金も表示する場合は、同じ電力網の **コスト** で「**総コストを追跡するエンティティを使用**」（Use an entity tracking the total costs）を選び、`Octopus Energy Japan cost` を選択します（v0.4.0〜）
5. 保存すると時間別グラフに反映されます

> [!NOTE]
> 料金統計（`Octopus Energy Japan cost`）は、確定済みの30分枠ごとの**従量料金**（段階制の限界単価）に
> 燃料費調整額と再エネ賦課金を加えた額です。**基本料金（日払い分）は含みません**。
> 単価は現在の料金表を用いるため、過去分は概算です（単価改定は考慮しません）。
> 表示される通貨は HA の通貨設定に従います。

> [!NOTE]
> 確定済みの30分枠のみ統計に投入します（暫定の当日値は除外）。
> そのため当日分は翌日以降に反映されます。

> [!IMPORTANT]
> v0.2.0 で外部統計の statistic_id が `octopus_energy_jp:consumption` から口座番号別の
> `octopus_energy_jp:<account_slug>_consumption`（例: `octopus_energy_jp:a_b00c43a0_consumption`）に変更されました。v0.1 から更新した場合は
> Energy Dashboard の消費量ソースを選び直してください（旧統計は履歴として残ります）。

> [!WARNING]
> 消費量には**外部統計（`Octopus Energy Japan consumption`）だけ**を選んでください。
> `today_kwh` / `month_kwh` / `cost_today` / `cost_month` も統計を持ちますが、
> 外部統計と併せて選ぶと**二重計上**になります。
> `yesterday_kwh` / `prev_month_*` / `billing_*` / `cost_yesterday` / `prev_month_cost` は
> 確定済み期間のスナップショットのため、長期統計を生成しません（履歴は recorder の通常履歴に残ります）。

> [!NOTE]
> コストの欄には料金統計（`Octopus Energy Japan cost`）を選んでください。消費量ソースが外部統計の場合、
> HA は料金を単価エンティティや固定価格では計算できません（`cost_today` / `cost_month` などのセンサーは選択不可）。
> 単価センサー（`current_rate`）は表示・自動化用で、Energy Dashboard の料金計算には使われません。

## トラブルシューティング / Troubleshooting

### ログイン失敗

| エラー | 意味・対処 |
|---|---|
| `invalid_auth` | メールアドレスまたはパスワードが違います。オクトパスエナジーのマイページでログインできるか確認してください |
| `cannot_connect` | Kraken API への接続に失敗しました。ネットワークとHAの時刻設定を確認し、時間をおいて再試行してください |
| `unknown` | 予期しない応答です。HAを再起動して再試行し、直らなければログを添えて Issues に報告してください |

### データが出ない

- 30分値は約8時間遅れで届きます。当日分が空でも翌日には確定値が入ります。
- 初回セットアップ直後は過去分の取得に時間がかかります。

### Energy Dashboard の統計に出ない

- `recorder` が必須です。無効化していると統計が記録されません。
- 追加・再起動後は統計の反映に最大2時間程度かかります。
- 確定済み時間枠のみ投入するため、当日分は翌日以降の反映です。

### 再認証

パスワード変更などで `invalid_auth` が出た場合は、統合エントリの再認証から再入力してください
（設定 → デバイスとサービス → Octopus Energy Japan → ⋮ → 再認証 / Re-authenticate）。

メールアドレスやパスワードを変更したい場合は、同じ ⋮ メニューの **再設定**（Reconfigure）を使うと
エントリを削除せずに更新できます（v0.4.0〜。別アカウントの認証情報を入力した場合は拒否されます）。

### v0.2.5 以降の「state class が削除されました」修復警告（v0.2.4 以前からの更新）

v0.2.5 で、確定済み期間のスナップショット6センサーから `state_class` を意図的に外しました（`state_class: total` に有効な `last_reset` がなく、長期統計がマイナス等の不正値になる問題のため）。対象は次のとおりです（`sensor.octopus_energy_<account>_…`）。

| センサー | entity_id 末尾 |
|---|---|
| 前日使用量 (`yesterday_kwh`) | `yesterday_usage` |
| 前月使用量 (`prev_month_kwh`) | `previous_month_usage` |
| 前日料金 (`cost_yesterday`) | `yesterday_cost` |
| 前月料金 (`prev_month_cost`) | `previous_month_cost` |
| 請求期間使用量 (`billing_kwh`) | `billing_period_usage` |
| 請求期間料金 (`billing_cost`) | `billing_period_cost` |

既に recorder に統計メタデータがある環境では、Home Assistant が **修復**（`sensor` ドメイン、翻訳キー `state_class_removed` / issue_id `state_class_removed_sensor.` 付き）を出し、「state class が削除され、長期統計は記録されなくなった」旨が表示されます。連携の不具合ではなく**想定どおりの動作**です。Energy Dashboard で使う長期統計は外部統計（**Octopus Energy Japan consumption**）に影響しません。

**対処:** **設定 → システム → 修復**（Settings → System → Repairs）で該当項目を無視（Dismiss）してください。issue レジストリに `dismissed_version` が残るため、次回の統計コンパイルで再発しても通常は戻りません。

> [!WARNING]
> 任意で、上記6 entity の古い統計行だけを消すには、開発者ツール → サービスで `recorder.clear_statistics` を6つの `entity_id` に対して実行できます。**記録済み統計は永久に削除され、元に戻せません。**

### デバッグログ

`configuration.yaml` に以下を追加して再起動すると詳細ログが出ます：

```yaml
logger:
  default: info
  logs:
    custom_components.octopus_energy_jp: debug
```

## 開発

テストは Home Assistant 本体のテストハーネスを使います
（`pytest-homeassistant-custom-component` が HA 2026.x を固定するため **Python 3.14 以降**が必要）。

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt

ruff check --config pyproject.toml custom_components tests
ruff format --check --config pyproject.toml custom_components tests
python -m compileall -q custom_components
pytest tests/ -q --cov=custom_components.octopus_energy_jp --cov-fail-under=90
```

- `tests/test_utils.py` は HA 非依存のロジックのみを対象にしているため、Home Assistant なしでも実行できます。
- `tests/test_config_flow.py` / `test_init.py` / `test_sensor.py` / `test_api.py` / `test_coordinator.py` / `test_diagnostics.py` は
  HA のテストハーネス上で動作し、ネットワークには一切アクセスしません。
- カバレッジゲートはパッケージ全体で 90%。
  モジュール別の現状: diagnostics 100% / api 99% / utils 98% / coordinator 96% / statistics 95% / sensor 94% / config_flow 91%（`__init__.py` は 58%）。
- CI（`hassfest` / `hacs` / `test`）で同じ検証が自動実行されます。

## ライセンス

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs]: https://github.com/hacs/integration
[test-badge]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/test.yaml/badge.svg
[test]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/test.yaml
[hacs-action-badge]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hacs.yaml/badge.svg
[hacs-action]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hacs.yaml
[hassfest-badge]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hassfest.yaml/badge.svg
[hassfest]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hassfest.yaml
