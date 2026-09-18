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
- 請求期間の使用量・料金センサー（v0.2.3〜、基本料金・燃料費調整額・再エネ賦課金の加算に対応）
- Energy Dashboard 連携（時間別消費量の外部統計を自動投入）
- 段階制料金（グリーンオクトパス等）の料金計算
- 日次履歴の永続化（API保持期間より古い分も保持）
- 日英バイリンガル対応

## 前提と制限 / Requirements & Limitations

- Home Assistant 2025.4 以降が必要です（Requires HA 2025.4+。開発・動作確認は 2026.9 系）。
- `recorder` が必須です。無効化していると履歴・統計が記録されません。
- データ更新間隔は1時間です（Data refreshes hourly）。30分値は約8時間遅れで届きます。
- 段階制料金プラン（グリーンオクトパス等）に対応。従量単価は Kraken API の料金表から取得します。
- 複数契約がある場合、最初の供給地点のみ使用します（Only the first supply point is used）。
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
| 基本料金 | 円/日 | 契約の基本料金（日割りで加算） | 未設定 |
| 燃料費調整額 | 円/kWh | 燃料費調整単価（使用量に乗じて加算） | 未設定 |
| 再エネ賦課金 | 円/kWh | 再生可能エネルギー発電促進賦課金単価（使用量に乗じて加算） | 未設定 |

未設定のままではエネルギー料金（従量料金）のみの計算です。
設定すると請求期間センサー（`billing_kwh` / `billing_cost`）に反映されます。

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

メインセンサーの属性には `avg_rate`（平均単価）、`daily`（日別使用量・料金）、
`yesterday_series` / `today_series`（30分値系列）、`plan_name`、`last_update` も含まれます。

> [!NOTE]
> 30分値は約8時間遅れで届くため、当日値は暫定表示です。統計投入は確定済み時間枠のみ行います。
> 過去月の料金は現在の単価表による近似です（単価改定は考慮しません）。

## Energy Dashboard

1. **設定 → ダッシュボード → エネルギー** を開きます
2. 電力網の **消費量を追加** → ソースの一覧から `Octopus Energy Japan consumption` を選択します
3. 複数契約がある場合は契約ごとの統計名（末尾に供給地点IDが付くもの）から該当する契約を選びます
   （Select the statistic matching your supply point when multiple contracts exist）
4. 保存すると時間別グラフに反映されます

> [!NOTE]
> 確定済みの30分枠のみ統計に投入します（暫定の当日値は除外）。
> そのため当日分は翌日以降に反映されます。

> [!IMPORTANT]
> v0.2.0 で外部統計の statistic_id が `octopus_energy_jp:consumption` から契約別の
> `octopus_energy_jp:<供給地点ID>_consumption` に変更されました。v0.1 から更新した場合は
> Energy Dashboard の消費量ソースを選び直してください（旧統計は履歴として残ります）。

> [!WARNING]
> 消費量には**外部統計（`Octopus Energy Japan consumption`）だけ**を選んでください。
> `today_kwh` / `month_kwh` / `cost_today` / `cost_month` も統計を持ちますが、
> 外部統計と併せて選ぶと**二重計上**になります。
> `yesterday_kwh` / `prev_month_*` / `billing_*` / `cost_yesterday` / `prev_month_cost` は
> 確定済み期間のスナップショットのため、長期統計を生成しません（履歴は recorder の通常履歴に残ります）。

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

### デバッグログ

`configuration.yaml` に以下を追加して再起動すると詳細ログが出ます：

```yaml
logger:
  default: info
  logs:
    custom_components.octopus_energy_jp: debug
```

## 開発

```bash
python -m compileall -q custom_components
pytest
hassfest --action validate
hacs validate
```

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
