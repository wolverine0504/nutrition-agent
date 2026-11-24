# Nutrition Agent presentation web demo

The approved scope is one publicly viewable, shared synthetic workspace and conversation. An interaction code authorizes messages and preview approval/rejection. A separate presenter code authorizes reset. Accounts, personal workspaces, BYOK, and iOS are deferred.

The existing `DemoAgent`, `Workflow`, `NutritionService`, and deterministic renderer provide the behavior. The web adapter keeps the same `web-shared` workflow session across requests and processes. PostgreSQL stores the transcript and workflow. A database advisory lock serializes messages, decisions, reset, and consistent snapshots across replicas. While an operation runs, other requests receive HTTP 409 and retain the previous visible state.

The public API exposes demo records, limited workflow data for the interface, and tool names and statuses. Agent instructions, API credentials, database settings, model traces, and tool argument payloads are not exposed through the API. Text is inserted with `textContent`, never interpreted as HTML. Codes are sent in headers and are not stored in browser storage or transcript rows.

Model calls default to disabled even when an API key exists. Only `WEB_ENABLE_MODEL_CALLS=true` enables natural-language model calls. The curry chicken walkthrough, including supported recipe-name replies, purchase messages, and simple one-box meal logs, works without a model client. Offline checks do not measure real-model performance.

## Local startup without model calls

From the repository root, add these settings to your existing `.env`:

```dotenv
WEB_DATABASE_SCHEMA=web_demo_curry
WEB_ENABLE_MODEL_CALLS=false
WEB_INTERACTION_CODE=<a random code at least 12 characters long>
WEB_PRESENTER_CODE=<a different random code at least 12 characters long>
```

Use independently generated random values, not the placeholders. The Compose overlay defaults to `web_demo_curry` and accepts a fresh `WEB_DATABASE_SCHEMA` override, independent of the CLI's `DATABASE_SCHEMA`. The web application refuses schemas without the `web_demo_` prefix. Reset replaces that dedicated schema’s application data atomically. If reseeding fails, the previous transcript and records are preserved. It does not touch another schema or the evaluation database.

Build and initialize the empty synthetic workspace:

```sh
export WEB_ENABLE_MODEL_CALLS=false
docker compose -f compose.yaml -f compose.web.yaml up -d --wait db
docker compose -f compose.yaml -f compose.web.yaml build web
docker compose -f compose.yaml -f compose.web.yaml run --rm web .venv/bin/python -m nutrition_demo.web_setup
docker compose -f compose.yaml -f compose.web.yaml up -d web
```

Open http://localhost:8080. Initialization refuses an already populated workspace. For this version, use a fresh schema instead of pointing the new recipe catalog at the old demo records. **Presenter controls → Reset demo** restores the sample pantry, both goal settings, and an empty chat, meal log, and batch list. Initialization and reset make no model calls.

### Offline curry chicken walkthrough

1. Open two browser windows. Both show the starting inventory and goals. Before entering the interaction code, verify that a write is refused.
2. Enter the interaction code. Choose **Confirm inventory**, then click **curry chicken** or type **curry**. Click **Adjust to my goals** with four boxes selected.
3. Verify the proposal passes all four per-meal goals. It needs **1,160 g of chicken** and has **1,000 g on hand**, so it is **160 g short**. **Accept recipe** is unavailable.
4. Type “I bought 200 g of raw chicken breast” or use **Add 200 g chicken**. Rejecting the preview leaves the shortage in place. Submit it again and approve it. The same proposal now has **1,200 g on hand** and can be accepted.
5. Click **Accept recipe**, then **Used plan as written**. The preparation preview lists what will be deducted and the quantities and nutrition for four boxes. Approve it. Both windows should display four boxes, 40 g of chicken in inventory, and no recipe acceptance card.
6. Send “I ate one box from the 4-box curry chicken batch I just saved for lunch.” Both windows should show three boxes remaining and one meal in **Today’s nutrition**. Refresh both windows to confirm the saved state.
7. Use `docker compose -f compose.yaml -f compose.web.yaml restart web` to check restart persistence. Then reset with the presenter code and verify the starting state returns in both windows.

The offline adjustment uses a checked example from the demo fixture and recomputes nutrition and stock through the normal application services. A recipe customized for different goals can be tested by the presenter with the live bot. The example counts the inventory purchased, used in cooking, and eaten as separate actions.

To run without Docker, provide a reachable PostgreSQL `DATABASE_URL` and export the same `WEB_*` settings, then run:

```sh
uv sync --locked
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python -m nutrition_demo.web_setup
.venv/bin/python -m uvicorn nutrition_demo.web:app --host 127.0.0.1 --port 8080
```

The existing core dependency lock remains unchanged. Web adapter dependencies are separately pinned in `requirements-web.txt`; their transitive dependencies are resolved at installation. `Dockerfile.web` leaves the CLI image unchanged.

## Automated offline tests

With Docker available, the following commands use synthetic fixtures, test doubles, and offline commands. Set the codes above and configure `TEST_DATABASE_URL` for `db_test/nutrition_test` in `.env` before running them:

```sh
export WEB_ENABLE_MODEL_CALLS=false
docker compose -f compose.yaml -f compose.web.yaml --profile test up -d --wait db_test
docker compose -f compose.yaml -f compose.web.yaml --profile test run --rm --no-deps web .venv/bin/python -m pytest tests/test_web_*.py -q
```

The integration tests use the existing `TEST_DATABASE_URL` guard requiring `db_test/nutrition_test`. They create and drop isolated `web_demo_` schemas. They verify the complete curry chicken flow, recipe acceptance against current stock, ambiguous batch choices, persistence across workers, lock contention, reset rollback, and correction approval without double consumption.

HTTP tests inject a fake workspace and check public viewing, code authorization, separate reset permissions, validation, native preview commands, and sanitized failures. Coordinator tests require neither FastAPI nor PostgreSQL.

Requests reserve a persistent receipt before invoking the agent. Retrying the same request ID returns that receipt instead of running the operation again. Reusing it with different text returns 409. If a worker dies before storing a final receipt, the receipt warns the user to inspect shared records. Recovery is conservative: it does not automatically replay an uncertain mutation. `/retry` can recover a completed meal when the existing agent recovery state is available. Requests with deliberately new IDs represent new user actions.

## Real-model checks — performed by the presenter

After offline checks pass, supply `OPENAI_API_KEY` through the existing `.env` or the hosting secret store. Enable model calls only when you are ready to run paid tests:

```sh
WEB_ENABLE_MODEL_CALLS=true docker compose -f compose.yaml -f compose.web.yaml up -d --force-recreate web
```

Use the web interface and interaction code:

1. Follow the offline walkthrough through saving the four-box batch. Ask “What prepared batches are available?” Verify the bot reports the saved batch and count.
2. Ask for a recipe adjustment for a different saved goal in plain language. Verify the calculated proposal shows both nutrition and stock checks. Use a separate presenter reset to restore the starting goals afterward.
3. With a logged lunch available, ask to correct its date. Verify the preview shows the old and new records, and that approval changes the date without consuming another box.
4. Ask a hypothetical consumption question and verify that it creates no meal. In a second window, check that overlapping operations are refused as busy.
5. Check that the UI and public API contain only presentation data and do not expose credentials, prompts, or raw model traces.

These are manual acceptance cases, not claimed real-model test results. Stop paid calls afterward:

```sh
WEB_ENABLE_MODEL_CALLS=false docker compose -f compose.yaml -f compose.web.yaml up -d --force-recreate web
```

## Cloud Run and Cloud SQL

Proposed presentation configuration as of November 24, 2026: Oregon (`us-west1`), Cloud SQL Enterprise PostgreSQL on one zonal `db-f1-micro` with 10 GiB SSD, and one request-billed Cloud Run instance at most, scaling to zero when idle.

| Component | Estimated monthly cost |
| --- | ---: |
| Cloud SQL compute at $0.0105/hour × 730 hours | $7.67 |
| 10 GiB SSD at $0.17/GiB-month | $1.70 |
| Cloud Run at 1 vCPU and 512 MiB | $0 if the demo fits within the billing account’s remaining free tier |
| Base total | $9.37 |

The base excludes backups beyond any allowance, registry storage, logging, egress, taxes, and usage above free tiers. Budget $10–15 for a lightly used month. At a 2.5-second polling interval, 50 visible browser tabs for one hour generate about 72,000 state requests. Cloud Run’s free tier is shared across the billing account. Model calls are disabled for this estimate. [Cloud SQL pricing](https://cloud.google.com/sql/pricing) · [Cloud Run pricing](https://cloud.google.com/run/pricing)

After cost approval, prepare the resources in a billing-enabled project:

1. Enable `run.googleapis.com`, `sqladmin.googleapis.com`, `artifactregistry.googleapis.com`, and `secretmanager.googleapis.com`. Create a Docker repository in `us-west1`, and give the image publisher Artifact Registry Writer on that repository.
2. In Cloud SQL, create a PostgreSQL 18 instance and explicitly select Enterprise edition in `us-west1` with a single zone, `db-f1-micro`, 10 GiB SSD, and public IP for the Cloud SQL Auth Proxy path. Leave authorized networks empty when only the proxy is needed. Create the `nutrition_web` database and a dedicated PostgreSQL login. As the database owner, grant that login `CONNECT, CREATE` on `nutrition_web` so the init job can create and own its schema. Use a dedicated instance for synthetic data. [Choose a Cloud SQL edition](https://docs.cloud.google.com/sql/docs/postgres/choose-edition)
3. Create a dedicated runtime service account. Grant it Cloud SQL Client (`roles/cloudsql.client`) in the project and Secret Accessor (`roles/secretmanager.secretAccessor`) on each demo secret. The deployer needs permission to act as this runtime service account.
4. Create Secret Manager secrets named `nutrition-demo-database-url`, `nutrition-demo-interaction-code`, and `nutrition-demo-presenter-code`. Independently generate both codes. Use a libpq DSN of the form `host=/cloudsql/PROJECT:us-west1:INSTANCE dbname=nutrition_web user=DEMO_USER password=DEMO_PASSWORD` and apply libpq quoting if the password contains spaces or other special characters. Cloud Run uses the Cloud SQL Auth Proxy to encrypt the connection. [Connect from Cloud Run](https://docs.cloud.google.com/sql/docs/postgres/connect-run)

With `PROJECT`, `REPOSITORY`, `INSTANCE`, and `RUNTIME_SA` set, where `RUNTIME_SA` is the service account email, build and publish the Linux image, initialize a fresh synthetic schema, and deploy after release approval. Keep the previous revision and schema available for rollback.

```sh
REGION=us-west1
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPOSITORY/nutrition-demo:v1"
CONNECTION="$PROJECT:$REGION:$INSTANCE"
SCHEMA=web_demo_curry

gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker buildx build --platform linux/amd64 -f Dockerfile.web -t "$IMAGE" --push .

gcloud run jobs create nutrition-demo-init --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$RUNTIME_SA" --set-cloudsql-instances "$CONNECTION" \
  --set-env-vars "WEB_DATABASE_SCHEMA=$SCHEMA,WEB_ENABLE_MODEL_CALLS=false" \
  --set-secrets DATABASE_URL=nutrition-demo-database-url:latest \
  --command /app/.venv/bin/python --args=-m,nutrition_demo.web_setup --max-retries 0
gcloud run jobs execute nutrition-demo-init --project "$PROJECT" --region "$REGION" --wait

gcloud run deploy nutrition-demo --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$RUNTIME_SA" --add-cloudsql-instances "$CONNECTION" \
  --set-env-vars "WEB_DATABASE_SCHEMA=$SCHEMA,WEB_ENABLE_MODEL_CALLS=false" \
  --set-secrets DATABASE_URL=nutrition-demo-database-url:latest,WEB_INTERACTION_CODE=nutrition-demo-interaction-code:latest,WEB_PRESENTER_CODE=nutrition-demo-presenter-code:latest \
  --execution-environment gen2 --allow-unauthenticated --cpu-throttling \
  --min 0 --max 1 --cpu 1 --memory 512Mi --timeout 600s
```

Proceed to deploy only after the initialization job succeeds. Verify the public URL and `/api/state` before sharing it. Repeat the public viewing, rejected unauthorized writes, cross-window persistence, and presenter reset checks on the hosted service. Real-model tests remain presenter-run. [Cloud Run job CLI reference](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/create) · [Cloud Run deploy CLI reference](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy)

## Curry chicken local review

The isolated review project used `web_demo_curry_review` on the demo database and `db_test/nutrition_test` for integration tests. The review URL is `http://127.0.0.1:8081`. The local runtime file contains the two review codes, separate from both the repository environment and hosted secrets.

- **162 application tests passed**. The evaluation suite was excluded and the runtime had `WEB_ENABLE_MODEL_CALLS=false`. Starlette reported one TestClient deprecation warning.
- Two independent Chrome sessions completed the offline walkthrough. They verified public viewing, access-code checks, selection by button and by name, the blocked proposal, purchase rejection, the approved 200 g purchase, the same proposal becoming available, four saved boxes, 40 g of chicken left, and three boxes after logging lunch. Both windows returned to the empty starting state after the presenter reset.
- PostgreSQL tests verified stale acceptance after a second worker changes the stock, stale cooking-plan rejection, guarded batch choices, the “2-box curry batch I just saved” request, and request replay without consuming another box.
- No paid model calls, application evaluations, or Cloud Run changes were made during this review.

Run the repeatable browser check against a disposable local workspace using Chrome and its temporary runtime file:

```sh
WEB_ENABLE_MODEL_CALLS=false uv run --no-project --with playwright python tests/browser_curry_walkthrough.py http://127.0.0.1:8081 "$runtime_env"
```

## Original demo verification on November 24, 2026

Docker Engine 29.9.3 was reachable. Docker Hub returned the expected registry authentication challenge and PyPI returned HTTP 200. The default Compose build installed FastAPI 0.115.12 and Uvicorn 0.34.2 in the image. Buildx also built and loaded the Linux amd64 image locally, and that image imported the FastAPI app successfully.

Verification used a fresh Compose project with separate database volumes and a temporary runtime environment file outside the repository. The file contained temporary credentials, `WEB_DATABASE_SCHEMA=web_demo_presentation`, `WEB_ENABLE_MODEL_CALLS=false`, and DSNs targeting only `db/nutrition` and `db_test/nutrition_test` in the isolated Docker network.

```sh
docker info --format '{{.ServerVersion}}'
curl -sSI --max-time 8 https://registry-1.docker.io/v2/
curl -sSI --max-time 8 https://pypi.org/simple/fastapi/
docker compose -p nutrition-presentation-verify -f compose.yaml -f compose.web.yaml build web
docker compose -p nutrition-presentation-verify -f compose.yaml -f compose.web.yaml --profile test up -d --wait db db_test
docker run --rm --network nutrition-presentation-verify_default --env-file "$runtime_env" nutrition-presentation-verify-web .venv/bin/python -m pytest -q tests/test_web_workspace.py tests/test_web_api.py tests/test_web_integration.py
docker run --rm --network nutrition-presentation-verify_default --env-file "$runtime_env" nutrition-presentation-verify-web .venv/bin/python -m pytest -q --ignore-glob='tests/test_eval_*.py'
docker run --rm --network nutrition-presentation-verify_default --env-file "$runtime_env" nutrition-presentation-verify-web .venv/bin/python -m nutrition_demo.web_setup
docker run -d --name nutrition-presentation-verify-web --network nutrition-presentation-verify_default --env-file "$runtime_env" -p 127.0.0.1:8080:8080 nutrition-presentation-verify-web
curl -fsS http://localhost:8080/api/state
WEB_ENABLE_MODEL_CALLS=false docker buildx build --platform linux/amd64 --load -f Dockerfile.web -t nutrition-presentation-verify-web:cloud-amd64 .
```

- The application suite passed 145 tests, including all 23 tests in the three web modules, and emitted one Starlette TestClient deprecation warning. An unrestricted `pytest -q` in the demo image failed with `ModuleNotFoundError: No module named 'nutrition_eval'` because the presentation image omits the separate evaluation package. The passing command excludes evaluation modules.
- The initializer created the dedicated synthetic schema. A repeat initializer exited with `ValueError: Workspace is already initialized; use the presenter reset control`. The server returned a public snapshot with eight synthetic boxes, empty chat, and model calls disabled.
- Two isolated Chrome windows passed the offline checklist via `uv run --no-project --with playwright python /tmp/nutrition-web-verify.DsM5Im/browser.py`. Both displayed shared state without a code. Missing and incorrect codes blocked messages, and a code-free rejection left the preview pending. An authorized rejection retained today's meal and seven boxes. An authorized approval moved the meal to yesterday without consuming another box. After `docker restart nutrition-presentation-verify-web`, both windows reloaded the transcript and a fresh yesterday query confirmed one meal. The interaction code could not reset the workspace. The presenter code cleared both chats and restored eight boxes.
- A write briefly collided with a poll and returned the expected HTTP 409. The successful browser run retried busy requests. The restart check waited for `/api/state` to become available with `curl -fsS --retry 10 --retry-all-errors --retry-delay 1` after observing a transient `curl: (52) Empty reply from server` at startup.
- The demo remains available at `http://localhost:8080` in the isolated Compose network. The temporary access codes are in the owner-readable `/tmp/nutrition-web-verify.DsM5Im/runtime.env` file.
- Cloud Run and Cloud SQL instructions were checked against Google's CLI and connection documentation. Cloud resource creation, registry publishing, and hosted acceptance checks are pending approval.
- Zero paid model calls and zero model-based evaluations. The repository's `.env`, personal database, and evaluation artifacts were preserved.

## Original hosted deployment on November 24, 2026

The public demo is live at [nutrition-demo on Cloud Run](https://nutrition-demo-782866760234.us-west1.run.app) in project `nutrition-agent-515711`, region `us-west1`. Revision `nutrition-demo-00001-l7z` serves the site. Cloud SQL reports PostgreSQL 18 Enterprise, single zone, `db-f1-micro`, and 10 GiB SSD. Initialization job execution `nutrition-demo-init-24dmq` succeeded. The database creation command briefly returned HTTP 409 while instance provisioning finished, and a retry against the same instance succeeded.

The hosted checks passed for the public page, static assets, and PostgreSQL state. Two independent Chrome sessions verified shared chat, persistence after reload, rejected unauthorized writes, and presenter-only reset. The deployed revision and public state both showed model calls disabled. A natural-language request with the interaction code was refused, and the offline `/batches` command worked. The workspace was reset after verification. Zero model calls were made.

Use `/api/state` for a hosted smoke check. The Cloud Run edge returns HTTP 404 for `/healthz`, consistent with [Google's reserved URL paths](https://docs.cloud.google.com/run/docs/known-issues#reserved_url_paths).

The interaction and presenter codes are stored in the project’s Secret Manager as `nutrition-demo-interaction-code` and `nutrition-demo-presenter-code`. Share the interaction code only with participants who should be able to write. Keep the presenter code for the person running the demo.
