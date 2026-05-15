# arango-gateway-app

Databricks App: runs inside Databricks as a proxy/gateway for connecting Databricks services with an external Arango cluster. Supports HTTP request validation, batch upserts, chat transport with Arango AI, and remote cluster management from inside Databricks.

Additional GraphML and Databricks Pipelines/Jobs are discussed in the Arango-Databricks suite of github repos.

**Configuration:** `app.yaml` documents env keys (e.g. `ARANGO_REGISTRY_TABLE`, `DATABRICKS_SQL_WAREHOUSE_ID`, `UC_GRAPH_VOLUME_NAME`). **`DATABRICKS_SQL_WAREHOUSE_ID` has no baked-in default** — set it in the App UI, this file, `export`, or a parent bundle (e.g. **arango-platform-bundle** `sql_warehouse_id` → `resources.apps.*.config.env`). `src/arango_gateway/config.py` reads from the environment only.

## Run locally

```bash
cd arango-gateway-app
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
export DATABRICKS_SQL_WAREHOUSE_ID=…
export ARANGO_REGISTRY_TABLE=catalog.schema.table
export PYTHONPATH=src
python app.py
```

## Deploy from your laptop

```bash
./deploy_app.sh
```

The first run creates the Databricks App if that name does not exist yet (`databricks apps create`), then deploys.

Uses `./update_arango_registry_uc.sh` and **`./update_arango_gateway_registry_uc.sh`** (publishes the app’s public HTTPS URL to **`ARANGO_GATEWAY_REGISTRY_TABLE`** for `arango-dashboard-app` and other consumers). If deploy reports **INSUFFICIENT_PERMISSIONS** on that table, the table was likely created first by the **gateway app SP**; **restart the gateway app** so it runs `GRANT … TO \`account users\``, then re-run **`./deploy_app.sh`**, or ask a metastore admin to **`DROP TABLE`** on that registry and redeploy.

For **`DEBUG_POST_DEPLOY_IMPORT=true`**, this repo includes **`debug_post_deploy_import.sh`** (expects **`arango-import/nodes.jsonl`** and **`edges.jsonl`** next to it). That step uses **`LOCAL_ARANGO_URL`** and the same **`ARANGO_PING_BASIC_AUTH_*`** defaults as **`app.yaml`** / **`deploy_app.sh`** (override with **`export ARANGO_PING_BASIC_AUTH_PASSWORD=…`** or **`ARANGO_ROOT_PASSWORD_FILE`** for a different cluster).

## Deploy via bundle

```bash
databricks bundle deploy -t dev
```

## API surface

`GET /health`, `GET/POST /api/arango/registry*`, `POST /api/arango/ping`, `POST /api/arango/chat`, `POST /api/databricks-graph/*`, `GET /api/debug/startup-status` (registry + Arango probe only; no Genie).
