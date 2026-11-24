# Nutrition Agent

[English](README.md) | 繁體中文

個人備餐助理，採用 Python 命令列介面、可持久保存資料的 PostgreSQL，以及 OpenAI Responses API Agent。協助規劃食譜、追蹤食材與備餐批次、記錄食用，並在核准前預覽更正內容。

共用 Agent 預設使用英文。原始食譜引文與使用者提供的資料保留原本語言；iOS 介面仍在規劃中。

## 功能

- 四道保留來源證據與出處的食譜：`curry`、`nikujaga`、`bbq`、`rice_pot`。
- 分別設定每餐與每日的熱量、蛋白質、碳水化合物與脂肪目標；以精確計算判斷四項每餐範圍是否全部符合。
- 同次烹調可建立食材分配不同的多個批次。總用量只扣一次，所有分配必須符合用量守恆。
- 預設加餐與實際攝取分開保存；記錄食用時才扣除食材。
- 食用紀錄需要明確的已食用陳述與可辨識批次；批次歧義或庫存不足時先釐清。
- 個人設定、庫存、烹調、加餐、商品標示與更正透過預覽及應用程式管理的核准處理。
- 交易式寫入、請求去重、可持久保存的工作流程，以及模型中斷後取回已提交食用結果的機制。
- 隔離的評估程式比較 Agent 與固定流程 baseline，保存操作軌跡、評分與報告。

## 快速開始

先啟動 Docker Desktop，再於儲存庫根目錄執行：

```bash
cp -n .env.example .env
docker compose build app
docker compose up -d --wait db
docker compose run --rm app --init-db
docker compose run --rm app --db-info
docker compose run --rm app
```

在本機 `.env` 設定 `OPENAI_API_KEY`，即可使用自然語言互動。初始化、離線查詢指令、自動測試與腳本示範不需要 key。`.env` 僅保留在本機，不提交 Git。範例資料庫密碼供本機開發使用，密碼與連線設定須保持一致；只改 `.env` 不會改變既有資料庫 volume 的密碼。

啟動時檢查 migrations，不會清空資料。需要初始化或套用新 migration 時執行 `--init-db`；未知 migration 或 checksum 改變時會拒絕啟動。

## 使用方式

先分別提供每餐與每日營養目標、時區、廚具及餐盒容量。程式不會將每餐目標乘算後當成每日目標。

1. 輸入 `/new-plan`，核對庫存後以 `/inventory-ok` 確認。
2. 用 `/select curry` 或其他食譜 ID 選料理，預設為同一道料理六份。
3. 要求調整用量或允許的替換。每次提案由應用程式驗算，客製化最多三次。
4. 以 `/accept-recipe <proposal-id>` 接受提案。若未符合全部目標，須加上 `outside` 明確接受，仍保留未達標狀態。
5. 烹調後提供實際用量、成品盒數、各批分配，以及剩料或耗損。核准具體預覽後才扣庫存並建立批次。
6. 設定加餐，再明確陳述已食用餐點。查詢、假設或要求不要記錄的訊息，不構成扣除食材的授權。
7. 更正先核對預覽，再輸入 `/confirm <preview-id>`；取消使用 `/reject <preview-id>`。
8. 重開後可用 `/preview` 檢視待確認變更。若食用已提交但模型中斷，可重送原訊息或用 `/retry` 取回結果，避免再次扣料。正常完成後重複陳述同一餐，則視為另一餐。

其他指令：`/inventory`、`/batches`、`/daily`、`/daily YYYY-MM-DD`、`/help`、`/quit`。預覽在 30 分鐘後或狀態改變時失效；等待使用者輸入期間不持有資料庫交易。

烹調回答使用保留的來源證據或已確認配方步驟。盒內營養、預設整餐與實際加餐分開顯示；每日摘要只涵蓋已記錄備餐，不代表完整飲食。

## 來源與資料限制

[Catalog](demo_data/catalog.json)、[來源 manifest](demo_data/source_manifest.json) 與[保留的來源紀錄](demo_data/sources/) 保存來源身分、歷史估算及版本紀錄。一般食材採用 USDA SR Legacy，保留 record ID 與資料集指紋；已確認商品標示優先用於後續計算，歷史快照維持原樣。

缺少商品標示、單位、生熟狀態、瀝乾重量、密度、蒜與油用量或價格時，需要補充資料，不能自行編造。歷史每盒估算不是新配方的精確答案；四道食譜皆可讀取，但不表示所有食材都有完整計算資料。

來源份量保留原本差異：咖哩為六份完整便當與一份純咖哩；BBQ 為五份含菜與兩份無菜；Nikujaga 原始六份與後續未確認七份方案分開保存。成本顯示已知小計與完整性；保留的商品連結不代表目前價格或當地庫存。

## 模型設定與紀錄

```bash
docker compose run --rm app --model gpt-6-luna
```

模型優先順序為 `--model` → `OPENAI_MODEL` → `gpt-6-luna`。`gpt-6-luna` 預設推理強度為 `medium`；其他模型不傳未驗證的推理設定。可用 `OPENAI_REASONING_EFFORT` 覆寫；設定不相容時明確失敗，不自行切換模型。

互動預算預設為 12 次模型呼叫、24 次工具呼叫與三次澄清。SDK 重試關閉，每次 API 請求逾時為 45 秒。`agent_runs` 保存模型與設定、SDK／工具版本、工具輸入與結果、用量及時間，不保存 API key。本機 PostgreSQL 保存應用資料與操作軌跡；相關對話資料會送至設定的模型服務。

模型不能核准預覽。寫入採序列化與去重，相同 request ID 搭配不同輸入會被拒絕；未經驗證的模型自由文字不用來宣稱寫入成功或營養達標。

## 最新評估結果

[正式 v5 報告](results/formal-50-english-2026-11-20-karry-v5/report.md) 採用講義題目格式，包含 **50 題英文問題與 200 次已評分嘗試**：每題 Agent 三次、baseline 一次。每次從隔離測試 schema 中的全新合成資料開始，使用相同固定應用程式時鐘。

| 指標 | Agent | 固定流程 baseline |
|---|---:|---:|
| 成功次數 | 145 / 150 | 43 / 50 |
| 準確率 | 96.7% | 86% |
| 平均回覆輪數 | 1.51 | 1.38 |
| 平均模型呼叫數 | 2.04 | 1.02 |
| 平均延遲 | 3,496.9 ms | 2,763.3 ms |
| p95 延遲（nearest rank） | 5,876 ms | 5,332 ms |
| 平均輸入＋輸出 tokens | 9,158.8 | 4,504.1 |

Agent **pass@3 為 100%**（至少一次成功），**pass^3 為 94%**（三次全部成功）；A06、A07、P08 在三次之間結果翻轉。兩個控制器皆無失敗的執行，最終回覆皆無 null。成功須同時符合商業終態、動作順序及所有文字 rubric 條件；judge 用量不納入控制器指標。

v5 之前，[v3 報告](results/formal-50-english-2026-11-20-karry-v3/report.md) 在同一批題目上測得 Agent 38%、baseline 60%。v3 的 Agent 失敗全部出在回覆：固定模板漏掉使用者要求的資訊。Commit `0ad1e32` 與 `2f14d8a` 在不顯示未經檢查的模型文字的前提下，讓應用程式產生的回覆變完整；[v4 報告](results/formal-50-english-2026-11-20-karry-v4/report.md) 記錄了一個不公平地拉低 baseline 的工具 schema 問題，已在 v5 修正。這些修正是根據同一批 50 題的 v3 失敗設計的，因此 v5 會高估推廣效果。

結果僅描述這批合成題目。重複題型、有限的預寫澄清答案與模型 judge 的變異，都限制結果的推廣性。[v2 報告](results/formal-50-english-2026-11-20-v2/report.md) 在欄位改名前執行同一批題目（Agent 38%、baseline 50%），差異來自模型與 judge 的變異。[原始 v1 報告](results/formal-50-english-2026-11-20-v1/report.md) 保留回覆擷取問題；由於程式、評分與 fixture 起始狀態已改變，不能直接與之後的分數當作同條件比較。

- [每題問題與完整通過標準](docs/evaluation-task-standards.md)
- [全部題目與結果歷史](docs/evaluation-50-questions-and-results.md)
- [Take-home 報告](Report.md)
- [最新原始嘗試與操作軌跡](results/formal-50-english-2026-11-20-karry-v5/episodes.jsonl)
- [最新凍結執行資訊](results/formal-50-english-2026-11-20-karry-v5/status.json)
- [回覆擷取修正](docs/verification/evaluation-response-capture.md)與 [baseline／fixture 修正](docs/verification/evaluation-baseline-v2.md)

## 執行評估

`tasks.jsonl` 包含營養 10 題、烹調 8 題、加餐 7 題、食用紀錄 8 題、更正 10 題、查詢 5 題與無法回答 2 題；其中五題帶有 edge 標籤。每一列採用講義格式：`id`、`type`（`tool` 33 題、`rubric` 15 題、`unanswerable` 2 題）、`question` 與 `rubric`，另含狀態與順序評分所需的 fixture、預設回答、核准、預期變更與必要順序。`tool` 題除了回覆 rubric，也檢查最終狀態與動作順序。正式 v3–v5 使用此格式；v2 使用欄位改名前的同一批 50 題。四題開發案例獨立保存。`eval_config.json` 凍結題庫 hash、模型、上限、時鐘、migrations 與評分版本。目前版本為 `nutrition-eval-50-v2`、`fixed-workflow-v2`、`terminal-render-v1` 與工具版本 `v1.2`。

不呼叫資料庫或模型，即可產生固定假資料報告：

```bash
mkdir -p results
docker compose --profile test run --build --rm --no-deps --volume "$PWD/results:/app/results" --entrypoint uv app run --locked python -m nutrition_eval --tasks development_tasks.jsonl --config eval_config.json --output results/dev-local-check --dry-run
```

正式評估會產生付費模型呼叫，需要 `OPENAI_API_KEY`、`TEST_DATABASE_URL` 與程式 commit。每次使用新的空輸出目錄：

```bash
docker compose --profile test up -d --wait db_test
mkdir -p results
docker compose --profile test run --build --rm --no-deps --volume "$PWD/results:/app/results" --env "EVAL_PROGRAM_COMMIT=$(git rev-parse HEAD)" --entrypoint uv app run --locked python -m nutrition_eval --tasks tasks.jsonl --config eval_config.json --output results/formal-local-run
```

啟動前驗證凍結設定與專用 `db_test/nutrition_test` 目標，不回退個人資料庫。輸出包含 `episodes.jsonl`、`status.json`；完整執行另有 `summary.md`、`failure_trace.md` 與 `failure_analysis.md`。Harness 或 judge 中斷時保留部分紀錄並標為 incomplete，不產生正式總體指標。上方連結的擴充版 `report.md` 另外說明已保存正式結果的解讀。

## 測試與離線示範

最近一次完整測試（branch `karry_test`）共有 **192 項通過**。資料庫測試使用真實 PostgreSQL 與獨立 schema，外部模型回覆採假資料。

```bash
docker compose --profile test up -d --wait db_test
docker compose --profile test run --build --rm --no-deps --entrypoint uv app run --locked python -m pytest tests/ -q
```

在新的 Compose project 執行離線示範：

```bash
docker compose -p nutrition-demo up -d --wait db
docker compose -p nutrition-demo build app
docker compose -p nutrition-demo run --rm app --init-db
docker compose -p nutrition-demo run --rm app --seed demo_data/scenario.json --scripted
docker compose -p nutrition-demo run --rm app --db-info
```

示範使用合成資料與假模型，透過共用流程執行：規劃六份 → 實際備餐八盒 → 設定加餐 → 記錄一餐 → 更正日期。預期剩七盒、合成食材庫存 200 g、加餐九份，更正後日期記錄 550 kcal。這些不是個人目標或已驗證真實食譜營養。Seed 要求 schema 為空，再次示範請改用新 project 名稱。

## 資料保存、備份與還原

一般容器重建會保留 named volumes：

```bash
docker compose down
docker compose up -d --wait db
docker compose run --rm app --db-info
```

使用新備份檔名，避免覆寫既有備份：

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U nutrition -d nutrition -Fc > backups/nutrition-YYYY-MM-DD.dump
```

還原至新 project；dump 已含 schema，不要先初始化 migrations：

```bash
docker compose -p nutrition-restore up -d --wait db
docker compose -p nutrition-restore exec -T db pg_restore -U nutrition -d nutrition --no-owner --exit-on-error < backups/nutrition-YYYY-MM-DD.dump
docker compose -p nutrition-restore build app
docker compose -p nutrition-restore run --rm app --db-info
```

若自訂資料庫或使用者名稱，需調整指令。還原後核對 migrations、個人設定、庫存、批次、事件與筆數。避免刪除日常資料的 volumes；PostgreSQL major version 升級需要另外規劃遷移。

## 專案狀態與文件

實作、修正與兩次正式報告皆已納入 `main`。雲端介面、獨立的 200+ 題 benchmark、獨立 safety suite、iOS、圖片辨識、即時購物與完整逐步陪煮仍屬後續工作。

- [第一版範圍](docs/specs/v1-python-demo-scope.md)
- [完整產品範圍](docs/specs/full-product-scope.md)
- [V1 實作計畫](docs/superpowers/plans/2026-11-17-nutrition-agent-v1.md)
- [課程要求](docs/course-requirements.md)
- [歷史 V1 驗收證據](docs/verification/v1-acceptance.md)
- [環境紀錄](docs/verification/environment.md)

兩份 README 採相同架構、指令與結果數字；功能或結果改變時應同步更新。詳細連結文件可能保留原本語言。

## 簡報網頁展示

共用的 FastAPI 網頁展示及離線測試、真實模型測試步驟，請見 [docs/web-demo.md](docs/web-demo.md)。展示使用專用的合成資料庫 schema，預設停用模型呼叫。
