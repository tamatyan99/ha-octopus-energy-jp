# Octopus Energy Japan — Home Assistant Custom Integration

[![hacs][hacs-badge]][hacs]
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
- Energy Dashboard 連携（時間別消費量の外部統計を自動投入）
- 段階制料金（グリーンオクトパス等）の料金計算
- 日次履歴の永続化（API保持期間より古い分も保持）
- 日英バイリンガル対応

## インストール

### HACS（カスタムリポジトリ）

1. HACS → 右上メニュー → **カスタムリポジトリ**
2. リポジトリに `https://github.com/tamatyan99/ha-octopus-energy-jp`、カテゴリに **連携** を選んで追加
3. HACS の連携一覧から **Octopus Energy Japan** をダウンロード
4. Home Assistant を再起動

### 手動インストール

1. `custom_components/octopus_energy_jp/` を Home Assistant の `config/custom_components/` 配下にコピー
2. Home Assistant を再起動

## 設定

1. **設定 → デバイスとサービス → 統合を追加** → **Octopus Energy Japan** を検索
2. オクトパスエナジーのアカウントのメールアドレスとパスワードを入力
3. アカウント番号が自動取得され、センサーが作成されます

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

メインセンサーの属性には `avg_rate`（平均単価）、`daily`（日別使用量・料金）、
`yesterday_series` / `today_series`（30分値系列）、`plan_name`、`last_update` も含まれます。

> [!NOTE]
> 30分値は約8時間遅れで届くため、当日値は暫定表示です。統計投入は確定済み時間枠のみ行います。
> 過去月の料金は現在の単価表による近似です（単価改定は考慮しません）。

## Energy Dashboard

**設定 → ダッシュボード → エネルギー** の電力網に消費量ソースとして
`Octopus Energy Japan consumption` を追加できます。

## 開発

```bash
python -m compileall -q custom_components
```

## ライセンス

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs]: https://github.com/hacs/integration
[hacs-action-badge]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hacs.yaml/badge.svg
[hacs-action]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hacs.yaml
[hassfest-badge]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hassfest.yaml/badge.svg
[hassfest]: https://github.com/tamatyan99/ha-octopus-energy-jp/actions/workflows/hassfest.yaml
