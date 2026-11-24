# Nutrition Agent

English | [繁體中文](README.zh-TW.md)

A personal meal preparation assistant with a Python command-line interface, persistent PostgreSQL storage, and an OpenAI Responses API agent. It helps plan recipes, track ingredients and prepared batches, record meals, and preview corrections before approval.

The shared agent defaults to English. Original recipe quotations and user-provided data retain their original language. An iOS interface is planned.

## Features

- Four source-backed recipes: `curry`, `nikujaga`, `bbq`, and `rice_pot`, with retained source evidence and provenance.
- Separate meal and daily targets for calories, protein, carbohydrates, and fat; exact calculations determine whether all four meal ranges pass.
- One cooking run can produce multiple batches with different allocations. Total ingredient usage is deducted once, and allocations must conserve usage.
- Default addons are saved separately from actual intake; ingredients are consumed when a meal is recorded.
- Explicit consumption and an identified batch are required for meal logging. Ambiguous batches or insufficient stock require clarification.
- Profile, inventory, preparation, addon, product-label, and correction changes use previews and application-owned approval.
- Transactional writes, request deduplication, persistent workflows, and recovery of committed meals after a model interruption.
- An isolated evaluation harness comparing the Agent with a fixed-workflow baseline, with saved traces, grading, and reports.

## Quick start

Start Docker Desktop, then run these commands from the repository root:

```bash
cp -n .env.example .env
docker compose build app
docker compose up -d --wait db
docker compose run --rm app --init-db
docker compose run --rm app --db-info
docker compose run --rm app
```

Set `OPENAI_API_KEY` in your local `.env` for natural-language interaction. Initialization, offline query commands, automated tests, and the scripted demo do not require a key. Keep `.env` local and out of Git. The example database password is for local development; keep the password and connection settings consistent. Changing `.env` alone does not change an existing database volume's password.

Startup checks migrations without clearing data. Run `--init-db` when initialization or a new migration is required. Unknown migrations or modified checksums are rejected.

## Using the assistant

First provide separate meal and daily nutrient targets, timezone, equipment, and container capacity. Daily targets are not inferred by multiplying meal targets.

1. Enter `/new-plan`, review inventory, and confirm with `/inventory-ok`.
2. Select a dish with `/select curry` or another recipe ID. The default is six portions of one dish.
3. Request quantity adjustments or permitted substitutions. The application calculates each proposal; customization is limited to three attempts.
4. Accept the proposal with `/accept-recipe <proposal-id>`. Add `outside` to explicitly accept a recipe that does not meet every target; its unmet status remains visible.
5. After cooking, provide actual usage, output counts, allocations, and any remaining ingredients or losses. Approve the specific preview before stock is deducted and batches are created.
6. Configure addons, then explicitly report meals eaten. Queries, hypothetical meals, and requests not to record a meal do not authorize consumption.
7. Review corrections before `/confirm <preview-id>`; cancel with `/reject <preview-id>`.
8. Use `/preview` after reopening to inspect pending changes. If a meal committed before a model interruption, retype the request or use `/retry` to recover the original result without another deduction. Repeating a normally completed meal statement represents another meal.

Other commands: `/inventory`, `/batches`, `/daily`, `/daily YYYY-MM-DD`, `/help`, and `/quit`. Previews expire after 30 minutes or a state change. No database transaction remains open while waiting for user input.

Cooking answers use retained source evidence or confirmed recipe instructions. Box nutrition, default whole-meal nutrition, and actual addons are shown separately. Daily summaries cover recorded prepared meals, not the entire diet.

## Sources and data limits

The [catalog](demo_data/catalog.json), [source manifest](demo_data/source_manifest.json), and [retained source records](demo_data/sources/) preserve source identity, historical estimates, and version history. General ingredient data uses USDA SR Legacy with record IDs and dataset fingerprints; confirmed product labels take precedence for future calculations while historical snapshots are preserved.

Missing labels, units, raw/cooked bases, drained weights, densities, garlic/oil quantities, and prices must be supplied rather than invented. Historical per-box estimates are not exact answers for a new recipe. All four recipes can be read, but not every ingredient has complete calculation data.

Source yields retain their original distinctions: curry includes six complete meals plus one curry-only portion; BBQ includes five portions with vegetables and two without; the original six-portion nikujaga and later unconfirmed seven-portion plan remain separate. Costs show known subtotals and completeness. Retained product links do not establish current prices or local stock.

## Model settings and records

```bash
docker compose run --rm app --model gpt-6-luna
```

Model selection follows `--model` → `OPENAI_MODEL` → `gpt-6-luna`. The default reasoning effort is `medium` for `gpt-6-luna`; other models omit unverified reasoning settings. `OPENAI_REASONING_EFFORT` can override it; incompatible settings fail explicitly without model fallback.

Defaults are 12 model calls, 24 tool calls, and three clarifications per interaction budget. SDK retries are disabled and each API request has a 45-second timeout. `agent_runs` records model/settings, SDK/tool versions, tool inputs/results, usage, and timing, excluding API keys. Local PostgreSQL stores application data and traces; relevant conversation data is sent to the configured model service.

Models cannot approve previews. Writes are serialized and deduplicated; the same request ID with different inputs is rejected. Unchecked model prose is not used to declare successful writes or target compliance.

## Latest evaluation results

The [formal v5 report](results/formal-50-english-2026-11-20-karry-v5/report.md) uses the lecture-slide task format and contains **50 English questions and 200 scored attempts**: three Agent attempts and one baseline attempt per question. Each attempt starts with fresh synthetic data in an isolated test schema and the same fixed application clock.

| Metric | Agent | Fixed-workflow baseline |
|---|---:|---:|
| Successful attempts | 145 / 150 | 43 / 50 |
| Accuracy | 96.7% | 86% |
| Mean turns | 1.51 | 1.38 |
| Mean model calls | 2.04 | 1.02 |
| Mean latency | 3,496.9 ms | 2,763.3 ms |
| p95 latency (nearest rank) | 5,876 ms | 5,332 ms |
| Mean input + output tokens | 9,158.8 | 4,504.1 |

Agent **pass@3 is 100%** (at least one successful attempt) and **pass^3 is 94%** (all three attempts succeed); A06, A07, and P08 flip across runs. Neither controller had a failed episode or a null final reply. Success requires correct final business state, action order, and every response rubric criterion. Judge usage is excluded from controller metrics.

Before v5, the [v3 report](results/formal-50-english-2026-11-20-karry-v3/report.md) measured Agent 38% and baseline 60% on the same tasks. Every v3 Agent failure was in the reply, because fixed templates omitted requested facts. Commits `0ad1e32` and `2f14d8a` made the application-rendered replies complete without displaying unchecked model prose; the [v4 report](results/formal-50-english-2026-11-20-karry-v4/report.md) records a tool-schema problem that unfairly lowered the baseline and was fixed in v5. The fixes were designed from v3 failures on these same 50 tasks, so v5 overstates generalization.

These results describe this synthetic dataset. Repeated task variants, finite scripted clarifications, and model-judge variability limit generalization. The [v2 report](results/formal-50-english-2026-11-20-v2/report.md) ran the same tasks before the field rename (Agent 38%, baseline 50%); the differences reflect model and judge variation. The [original v1 report](results/formal-50-english-2026-11-20-v1/report.md) is preserved with its response-capture limitations; its scores are not directly comparable because code, grading, and fixture states changed.

- [Every task question and full passing standard](docs/evaluation-task-standards.md)
- [All questions and result history](docs/evaluation-50-questions-and-results.md)
- [Take-home report](Report.md)
- [Latest raw episodes and traces](results/formal-50-english-2026-11-20-karry-v5/episodes.jsonl)
- [Latest frozen run metadata](results/formal-50-english-2026-11-20-karry-v5/status.json)
- [Response-capture fix](docs/verification/evaluation-response-capture.md) and [baseline/fixture fixes](docs/verification/evaluation-baseline-v2.md)

## Running the evaluation

`tasks.jsonl` contains nutrition 10, preparation 8, addons 7, meal logging 8, corrections 10, queries 5, and unanswerable 2; five cases carry an edge label. Each row uses the lecture-slide format: `id`, `type` (`tool` 33, `rubric` 15, `unanswerable` 2), `question`, and `rubric`, plus the fixture, scripted answers, approval, expected changes, and required order used for state and order grading. `tool` tasks are graded on final state and action order as well as the response rubric. The formal v3–v5 runs use this format; v2 used the same 50 tasks before the rename. The four development cases are separate. `eval_config.json` freezes the dataset hash, models, limits, clock, migrations, and grading versions. Current revisions are `nutrition-eval-50-v2`, `fixed-workflow-v2`, `terminal-render-v1`, and tool version `v1.2`.

Generate a deterministic fake-client report without database or model calls:

```bash
mkdir -p results
docker compose --profile test run --build --rm --no-deps --volume "$PWD/results:/app/results" --entrypoint uv app run --locked python -m nutrition_eval --tasks development_tasks.jsonl --config eval_config.json --output results/dev-local-check --dry-run
```

A formal evaluation makes paid model calls and requires `OPENAI_API_KEY`, `TEST_DATABASE_URL`, and a program commit. Choose a new, empty output directory for every run:

```bash
docker compose --profile test up -d --wait db_test
mkdir -p results
docker compose --profile test run --build --rm --no-deps --volume "$PWD/results:/app/results" --env "EVAL_PROGRAM_COMMIT=$(git rev-parse HEAD)" --entrypoint uv app run --locked python -m nutrition_eval --tasks tasks.jsonl --config eval_config.json --output results/formal-local-run
```

Preflight verifies frozen settings and the dedicated `db_test/nutrition_test` target, without a personal database fallback. Output includes `episodes.jsonl`, `status.json`, and, for complete runs, `summary.md`, `failure_trace.md`, and `failure_analysis.md`. Harness or judge interruptions preserve partial records and mark the run incomplete without final aggregate metrics. The expanded `report.md` linked above adds interpretation of the saved formal results.

## Tests and offline demo

The latest full test run (branch `karry_test`) passed **192 tests**. Database-backed tests use real PostgreSQL with separate schemas; external model responses are faked.

```bash
docker compose --profile test up -d --wait db_test
docker compose --profile test run --build --rm --no-deps --entrypoint uv app run --locked python -m pytest tests/ -q
```

Run the offline demo in a new Compose project:

```bash
docker compose -p nutrition-demo up -d --wait db
docker compose -p nutrition-demo build app
docker compose -p nutrition-demo run --rm app --init-db
docker compose -p nutrition-demo run --rm app --seed demo_data/scenario.json --scripted
docker compose -p nutrition-demo run --rm app --db-info
```

The demo uses synthetic data and a fake model through the shared workflow: plan six portions → prepare eight boxes → configure addons → record a meal → correct its date. Expected results are seven remaining boxes, 200 g synthetic ingredient stock, nine addon servings, and 550 kcal recorded on the corrected date. These are not personal targets or verified real-recipe nutrition. Seeding requires an empty schema; use a new project name for another demo run.

## Persistence, backup, and restore

Normal container recreation preserves named volumes:

```bash
docker compose down
docker compose up -d --wait db
docker compose run --rm app --db-info
```

Use a new backup filename to avoid overwriting an existing backup:

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U nutrition -d nutrition -Fc > backups/nutrition-YYYY-MM-DD.dump
```

Restore into a new project. Do not initialize migrations first; the dump contains the schema:

```bash
docker compose -p nutrition-restore up -d --wait db
docker compose -p nutrition-restore exec -T db pg_restore -U nutrition -d nutrition --no-owner --exit-on-error < backups/nutrition-YYYY-MM-DD.dump
docker compose -p nutrition-restore build app
docker compose -p nutrition-restore run --rm app --db-info
```

Adjust database/user names if customized. Verify migrations, profile, inventory, batches, events, and counts after restoration. Avoid deleting daily-use volumes; PostgreSQL major-version upgrades require a separate migration plan.

## Project status and documentation

The implementation, fixes, and both formal reports are on `main`. A cloud interface, a separate 200+ task benchmark, an independent safety suite, iOS, image recognition, live shopping, and full guided cooking remain future work.

- [V1 scope](docs/specs/v1-python-demo-scope.md)
- [Full product scope](docs/specs/full-product-scope.md)
- [V1 implementation plan](docs/superpowers/plans/2026-11-17-nutrition-agent-v1.md)
- [Course requirements](docs/course-requirements.md)
- [Historical V1 acceptance evidence](docs/verification/v1-acceptance.md)
- [Environment record](docs/verification/environment.md)

The two README versions share the same structure, commands, and result figures. Update both when features or results change. Detailed linked documents may retain their original language.

## Presentation web demo

The shared FastAPI web demo and its offline/real-model test steps are documented in [docs/web-demo.md](docs/web-demo.md). It uses a dedicated synthetic database schema and disables model calls by default.
