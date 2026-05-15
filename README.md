# arango-gateway-app

Databricks App: runs inside Databricks as a proxy/gateway for connecting Databricks services with an external Arango cluster. Supports HTTP request validation, batch upserts, chat transport with Arango AI, and remote cluster management from inside Databricks.

Additional GraphML and Databricks Pipelines/Jobs are discussed in the Arango-Databricks suite of github repos.

**Configuration:** `app.yaml` documents env keys: `DATABRICKS_SQL_WAREHOUSE_ID`, `DATABRICKS_HOST`,`DATABRICKS_TOKEN` plus `ARANGO_LICENSE_CLIENT_ID`,`ARANGO_LICENSE_CLIENT_SECRET` 

Set those in the App or a parent bundle (e.g. **arango-platform-bundle** `sql_warehouse_id` → `resources.apps.*.config.env`). 

## Deploy from your laptop

```bash
export DATABRICKS_SQL_WAREHOUSE_ID=yours here...
./deploy_app.sh
```

## Deploy via bundle

```bash
databricks bundle deploy -t dev
```

## API surface

`GET /health`, `GET/POST /api/arango/registry*`, `POST /api/arango/ping`, `POST /api/arango/chat`, `POST /api/databricks-graph/*`, `GET /api/debug/startup-status` (registry + Arango probe only; no Genie).