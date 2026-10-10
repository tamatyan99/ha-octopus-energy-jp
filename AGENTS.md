# AGENTS.md — 開発者・AI エージェント向けガイド

このファイルは**コードとリリースの不変条件**をまとめたものです。ユーザー向けの説明は [README.md](README.md) を参照してください。
ここに書かれた「破ってはいけない理由」は、実際に発生した障害・レビュー指摘に基づいています。変更前に必ず該当項目を確認してください。

- ドメイン: `octopus_energy_jp` / Home Assistant カスタム連携（HACS 配布）
- 対象: Octopus Energy Japan（Kraken GraphQL API）の使用量・料金を取得し、外部長期統計として Recorder に投入
- 現行バージョン: manifest.json の `version`（執筆時点 `0.4.1`）

## 開発環境

- **Python 3.14 以降が必須**（`pytest-homeassistant-custom-component` が HA 2026.x を固定するため）
- HA 本体はテスト用 venv の site-packages に固定される。開発・動作確認は HA 2026.9 系
- 依存は `requirements_test.txt` のみ。実行時 `requirements` は空（`aiohttp` は HA 同梱）

```bash
ruff check --config pyproject.toml custom_components tests
ruff format --check --config pyproject.toml custom_components tests   # ← check と両方必要
python -m compileall -q custom_components
pytest tests/ -q --cov=custom_components/octopus_energy_jp --cov-fail-under=90
```

- CI は `.github/workflows/` の 3 本（`test` / `hacs` / `hassfest`）。push で**全ブランチ・タグ**に対して実行される
- カバレッジゲートは 90%。現状は 97% 台。**`ruff format --check` を忘れると CI が落ちる**（過去に実際に落ちた）

## 絶対に守る不変条件

### 1. 消費統計の契約を変えない

外部統計 `octopus_energy_jp:<account_slug>_consumption` の行は **`start` と `sum` のみ**。

- `state` を追加しない。既存ユーザーの履歴行を書き換えると Energy Dashboard の差分が壊れる
- `mean_type` / `has_sum` / `unit_class` / `unit_of_measurement` のメタデータも互換を維持（`unit_class="energy"`, `unit_of_measurement="kWh"`）
- 料金統計 `..._cost` は逆に **`state`（区間の円）+ `sum`（累計円）** を持つ（Energy Dashboard のコスト表示に必要）

### 2. 外部統計のメタデータは HA Core の先例に合わせる

料金統計は **`unit_class=None` かつ `unit_of_measurement=None`** で登録する。

- HA Core 自身の料金外部統計（`components/opower/coordinator.py`、`components/srp_energy/coordinator.py`）がこの形
- 通貨単位（`JPY` 等）を「改善」として足さない。金額用の unit converter は存在せず、**既存登録済み統計の単位変更はリスク**（単位変更はデータ再スケール経路に入る）

### 3. Recorder の統計行の `start` は **epoch 秒**

`get_last_statistics()` が返す `start` は**秒**（`Statistics.start_ts` は epoch 秒の DOUBLE 列）。
WebSocket API だけが `* 1000` してフロントエンド向けにミリ秒へ変換する。

- **ミリ秒として扱うと**: 復旧時の `_last_start` が 1970 年付近になり、取得ウィンドウ全体が「新規」と誤認され、復旧済み合計を基準に全行を上書き → **累積の二重計上**（v0.4.1 で修正した実バグ）

### 4. `sum` は単調・連続でなければならない

統計の `sum` をリセットしたり巻き戻したりすると、Energy Dashboard に**負の差分（大きな下向きバー）**として現れる。

- 累積の基準を再計算して「0 から」再投入してよいのは、**ウィンドウが最古の取込バケットから始まる場合（prefix == 0）だけ**
- 復旧時は Recorder の最終行（`_last_start` / `_cumulative`）から継続する。新規バケットのみ投入する

### 5. 改訂再取込のガードを緩めない（重要）

`statistics.py` の次のガードは**意図的に狭くしてある**:

```python
if overlap and settled[0]["start"] == self._earliest_start:
```

理由: この判定は「ウィンドウ内合計」と「**生涯累積**（= ウィンドウより古いバケットを含む）」を比較している。
`settled[0] == _earliest_start` のときだけ prefix（ウィンドウ外の既存バケット）が 0 になり、比較が成立する。

- ガードを外して「ウィンドウ前進後も訂正を拾う」ようにすると、差分は**必ず prefix の値**になり、ウィンドウ前進（＝毎月のロールオーバー）ごとに**誤検知**する
- その結果、再基準化で累積が 0 に潰れ、ウィンドウ全行が書き換わる。実測では 72 時間・各 1.0kWh のフィクスチャで**初回バケットの差分が −71kWh** になった（プローブで確認済み）
- 安全に実装するにはバケット単位の値の永続化（**ストア形式の変更**）が必要。ストア形式を変えるときは破壊的変更として版を分け、移行を書くこと

現状の既知の制限: 取得ウィンドウ（当月 − 2か月）より古い確定値が API 側で訂正されても再投入されない。コード内のコメントも参照。

### 6. 非破壊リリースを基本とする

- HACS（`hacs/default`）審査中。**statistic_id・entity_id・属性・ストアキーの互換**を壊す変更は、版を分けて（minor/major）実施し、README に移行手順を書く
- 統計の冪等性（同じ `start` の再投入は upsert）を前提に設計する

### 7. エンティティ・翻訳の作法

- `_attr_has_entity_name = True`、`unique_id = f"{account}_{description.key}"`、`DeviceInfo` を付与
- 表示名は `translation_key` 経由。**`strings.json` と `translations/{en,ja}.json` を同時に更新**（キー欠落は UI と CI に響く）
- エンティティ ID は表示名から生成される（例: キー `current_rate` → `sensor.octopus_energy_<account>_current_unit_rate`）。README のキー表記と混同しないこと
- `state_class=MEASUREMENT` と device_class（ENERGY/MONETARY 等）は併用不可。スナップショット系は `state_class=None`

### 8. 非同期・ライフサイクル

- event loop でブロッキング I/O をしない（同期 Recorder 呼び出しは executor 経由）
- `hass.async_create_task` を直接使わない。エントリに紐付けてアンロードで確実にキャンセルする（`entry.async_create_background_task`）
- 認証失敗は `ConfigEntryAuthFailed`、一時障害は `UpdateFailed`。初回は `async_config_entry_first_refresh()`
- 削除時はストアと**統計の両方**を片付ける（`async_remove_entry`）

### 9. 秘匿情報を出力しない

診断情報（`diagnostics.py`）でマスクすべきキー: `password` / `token` / `email` / `account_number` / `unique_id`（unique_id は口座番号そのもの）。

## 統計アーキテクチャ早見表

| 項目 | 値 |
|---|---|
| 消費統計 ID | `octopus_energy_jp:<account_slug>_consumption` |
| 料金統計 ID | `octopus_energy_jp:<account_slug>_cost` |
| ストアキー | `octopus_energy_jp_<entry_id>_daily` / `_statistics` / `_statistics_cost` |
| 投入対象 | 確定済みの時間枠のみ（`STATS_IMPORT_BUFFER` 経過分。当日の暫定値は除外） |
| 段階制コスト | 月内累積 + 同日先行スロットから限界単価を算出。**基本料金は含めない**（安定な時間按分が不可のため） |
| 復旧 | ストア消失・破損時は Recorder の最終行から `_last_start` / `_cumulative` を復元 |
| 更新間隔 | 1 時間（API の 30 分値は約 8 時間遅れで確定） |

## 変更時のチェックリスト

- [ ] `ruff check` **と** `ruff format --check` の両方がクリーン
- [ ] `pytest --cov-fail-under=90` がグリーン（新規バグ修正には**観測可能な振る舞い**を検証する回帰テストを付ける）
- [ ] 統計の契約を変えていない（消費 = `start`+`sum` / 料金 = `state`+`sum` / メタデータ互換）
- [ ] `sum` の連続性を壊していない（リセット・巻き戻しがない）
- [ ] `strings.json` と `translations/{en,ja}.json` のキーが整合
- [ ] README の該当節（機能・センサー一覧・Energy Dashboard・前提と制限）を更新
- [ ] 破壊的変更なら版を分け、移行手順を README に記載

## リリース手順

1. `manifest.json` の `version` を上げる（例 `0.4.1` → `0.4.2`）
2. main へ push → CI（test / hacs / hassfest）がグリーンであることを確認
3. タグを作成して push → GitHub Release を作成（本文に変更点と既知の制限を書く）
4. HACS は Release を検知する。ユーザー側は HACS の更新 → HA 再起動で反映
5. **タグは不変が原則。**やむを得ず付け直す場合は、先に Release を削除する（タグを消すと Release は draft に落ちる）

## コミット / PR

- メッセージは Conventional Commits 風（`fix:` / `refactor:` / `test:` / `perf:` / `docs:` / `style:` / `ci:`）
- 破壊的変更は本文にその旨と移行方法を明記
- コメント・docstring は日本語で可（既存スタイルに合わせる）。ただし**コード内の警告コメントは理由まで書く**
