# arango-gateway-app — agent-oriented reference

This document is written for **automated coding agents** and **human operators** who need a single, dense map of **intent**, **HTTP surface**, **Unity Catalog contracts**, **configuration precedence**, and **relationships to sibling apps** (`arango-dashboard-app`, `arango-mcp-app`). It complements the shorter `README.md` with formal detail.

---

## 1. Role and intent

**arango-gateway-app** is a **Databricks App** (Flask + Gunicorn) that acts as a **controlled HTTP façade** between the Databricks workspace ecosystem and a **remote ArangoDB** cluster whose network coordinates live in **Unity Catalog**.

At a high level it does **four** things:

1. **Registry and discovery** — Reads and writes `ARANGO_REGISTRY_TABLE` (connection coordinates: host, port, protocol, `is_active`). Optionally auto-creates the Delta table. Publishes this gateway’s own public HTTPS URL into `ARANGO_GATEWAY_REGISTRY_TABLE` so consumers do not hard-code hostnames.

2. **Embed proxy** — Serves Arango’s **Aardvark Web UI** under a same-origin prefix (`/embedded-arango/...`) so the dashboard can iframe Arango while the gateway applies **server-side Basic auth**, rewrites URLs/cookies for cross-site embedding, and avoids conflicting with Aardvark’s JWT login paths.

3. **UC → graph → Arango** — Exposes APIs to **extract Unity Catalog metadata** into a DataHub-aligned graph, optionally **export gzip JSONL** to a UC volume, and **bulk-import** nodes/edges into Arango using credentials from the active registry row and `ARANGO_PING_BASIC_AUTH_*`.

4. **Server-side Arango API access for agents** — `POST /api/arango/http` forwards JSON-shaped REST calls to Arango for identities that **cannot** reach the tunnel/cluster directly (e.g. `arango-mcp-app` MCP). Paths are **allowlisted** (`arango_proxy_path.py`).

Genie-related fields exist on `AppConfig` in `config.py` for historical / shared-dataclass parity; **this app does not implement Genie routes** — Genie lives on **arango-mcp-app** and the dashboard.

---

## 2. Repository layout (where to look)

| Path | Purpose |
|------|---------|
| `app.yaml` | Databricks App **command**, **env** defaults, **sql_warehouse** resource binding (`${DATABRICKS_SQL_WAREHOUSE_ID}`). Merged with bundle `config.env` on deploy. |
| `databricks.yml` | Standalone DAB name + optional **variables** (`sql_warehouse_id`, `arango_registry_table`, …) for `databricks bundle deploy` without the monorepo bundle. |
| `resources/arango_gateway.app.yml` | Minimal app resource when deploying this repo’s bundle in isolation. |
| `deploy_app.sh` | Laptop-oriented **sync → apps create/deploy → UC SQL grants → registry scripts → cloudflared** (tunnel for `update_arango_registry_uc.sh`). |
| `update_arango_registry_uc.sh` | SQL upsert of **Arango tunnel** into `ARANGO_REGISTRY_TABLE`. |
| `update_arango_gateway_registry_uc.sh` | SQL upsert of **gateway public URL** into `ARANGO_GATEWAY_REGISTRY_TABLE` (MERGE-based; safe under concurrent writers). |
| `src/wsgi.py` | Gunicorn entry (`wsgi:app`). |
| `src/arango_gateway/app.py` | `create_app()`: loads `AppConfig`, publishes gateway URL to UC on startup, registers blueprints, `ProxyFix`, CORS/CSP for embedders. |
| `src/arango_gateway/config.py` | `AppConfig` dataclass: **all settings from environment** at instantiation (no secrets file in repo). |
| `src/arango_gateway/routes/api.py` | JSON API under `/api`. |
| `src/arango_gateway/routes/embed.py` | Catch-all reverse proxy under `/embedded-arango`. |
| `src/arango_gateway/services/` | UC SQL (`databricks_sql.py`), registries (`arango_registry.py`, `gateway_url_registry.py`), Arango HTTP (`arango_http.py`), proxy allowlist, UC graph import, DataHub-style UC workflow, JSONL bundle, startup debug. |

---

## 3. HTTP surface (endpoints)

Unless noted, bodies are **JSON** for `POST`/`PUT`/`PATCH`.

### 3.1 Root (application factory)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness: `{"status":"ok"}`. Used by deploy scripts and load balancers. |

Duplicate mirror: `GET /api/health` returns the same payload (blueprint convenience).

### 3.2 Embed proxy (`arango_embed_bp`, prefix `/embedded-arango`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`, `POST`, … | `/embedded-arango/` | Default subpath `""` → upstream `/`. |
| `GET`, `POST`, … | `/embedded-arango/<path:subpath>` | Reverse-proxy to Arango **origin** resolved from `ARANGO_UI_IFRAME_URL` or active UC row (`resolve_arango_http_origin`). Rewrites HTML/JS/CSS responses to prefix asset and API paths; rewrites `Set-Cookie` when `ARANGO_EMBED_COOKIE_SAMESITE_NONE` is true; **does not** attach server Basic auth on `POST …/_open/auth` or `/_open/auth/renew` so Aardvark JWT flow works. |

**Security headers** (from `app.after_request`): removes `X-Frame-Options`, sets `Content-Security-Policy: frame-ancestors *`, permissive CORS for API/embed use cases.

### 3.3 API blueprint (prefix `/api`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | Same as `/health`. |
| `POST` | `/api/arango/chat` | Dashboard “Arango mode”: body `content` or `message`, optional `conversation_id`. Forwards to `ARANGO_CONVERSATION_URL` when set; otherwise stub via `arango_conversation.py`. |
| `GET` | `/api/arango/registry` | Returns tabular SQL result shape (`columns`, `rows`) for **active** UC registry read. |
| `POST` | `/api/arango/registry/init` | `CREATE TABLE IF NOT EXISTS` path for operators. |
| `POST` | `/api/arango/registry` | Upsert active row: `cluster_name`, `ip_address`, `port`, `protocol` (marks prior active rows inactive, inserts new active row — see `upsert_registry_entry`). |
| `POST` | `/api/arango/ping` | Optional JSON `path` (default `/_api/version`). Probes Arango using active UC row + `ARANGO_PING_BASIC_AUTH_*`. |
| `POST` | `/api/arango/http` | **JSON Arango proxy**: body `method`, `path`, optional `body`/`json`. Resolves base URL from UC; enforces `arango_http_proxy_path_allowed`. Intended for **arango-mcp-app** MCP and similar. |
| `GET` | `/api/debug/startup-status` | Query `refresh=true` to re-run `run_startup_debug_check`: UC registry + Arango ping (no Genie). |
| `POST` | `/api/databricks-graph/uc-tables` | UC table discovery for dashboard multiselect (options via `discovery_options_from_request_payload`). |
| `POST` | `/api/databricks-graph/extract-schema` | Full UC metadata graph extract + optional JSONL export + optional Arango import. Body options in `datahub_unity_catalog_workflow.options_from_request_payload`. If `stream_progress: true`, returns **NDJSON** stream (`application/x-ndjson`). |
| `POST` | `/api/databricks-graph/build-corpus-graphs` | Placeholder / future AutoGraph wiring. |
| `POST` | `/api/databricks-graph/documents` | Multipart upload of `.pdf`, `.txt`, `.md` to temp dir (placeholder for ML pipeline). |

---

## 4. Unity Catalog tables and contracts

### 4.1 `ARANGO_REGISTRY_TABLE` (default `workspace.default.arango_connection_registry`)

**Columns** (Delta): `cluster_name`, `ip_address`, `port`, `protocol`, `is_active`, `updated_at`.

- **Write paths:** `deploy_app.sh` / `update_arango_registry_uc.sh` (tunnel host), `POST /api/arango/registry`, and `ensure_registry_table` when `ARANGO_REGISTRY_AUTO_CREATE` is true.
- **Read paths:** embed origin resolution, ping, HTTP proxy, graph import, startup debug.
- **Semantics:** “Newest active row” pattern: readers use `WHERE is_active IS TRUE ORDER BY updated_at DESC LIMIT 1` (see `get_active_registry_row`).

After `ensure_registry_table`, the code attempts **`GRANT SELECT, MODIFY … TO account users`** so human operators can inspect the table in the workspace UI even when the app service principal created it first.

### 4.2 `ARANGO_GATEWAY_REGISTRY_TABLE` (default `workspace.default.arango_gateway_registry`)

**Columns:** `base_url`, `app_name`, `is_active`, `updated_at`.

- **Purpose:** Publish the gateway Databricks App’s **public** `https://…databricksapps.com` URL for **arango-dashboard-app** (iframe/embed targets) and **arango-mcp-app** (optional explicit gateway resolution elsewhere).
- **Writers:** `publish_self_gateway_url_to_uc_if_configured` on **each Gunicorn worker** startup (MERGE + retry), and `update_arango_gateway_registry_uc.sh` from `deploy_app.sh`. Both use atomic **MERGE** semantics to avoid duplicate `is_active=true` rows under concurrency.

### 4.3 UC volume (`UC_GRAPH_VOLUME_NAME` / `UC_GRAPH_SNAPSHOT_BASE`)

Default snapshot base derives from `ARANGO_REGISTRY_TABLE` + volume name:

`/Volumes/<catalog>/<schema>/<UC_GRAPH_VOLUME_NAME>/uc_graph_snapshots`

`deploy_app.sh` runs `CREATE VOLUME IF NOT EXISTS` and grants the app SP read/write on that volume. Graph extract may write JSONL bundles there when requested in the API payload.

---

## 5. Configuration — full inventory and semantics

All runtime configuration is **`os.environ` → `AppConfig`** (`config.py`). Databricks injects `app.yaml` `env` entries as environment variables; the parent bundle **overrides** matching keys.

### 5.1 Required for UC SQL

| Variable | Meaning |
|----------|---------|
| `DATABRICKS_SQL_WAREHOUSE_ID` | Warehouse used by `WorkspaceClient().statement_execution.execute_statement`. **No default in code** — must be set in `app.yaml`, bundle, or App UI. |

`app.yaml` binds the `sql-warehouse` resource with `id: ${DATABRICKS_SQL_WAREHOUSE_ID}` so the platform grants **CAN_USE** to the app for that warehouse.

### 5.2 Registry and graph export

| Variable | Default / notes |
|----------|-----------------|
| `ARANGO_REGISTRY_TABLE` | `workspace.default.arango_connection_registry` |
| `ARANGO_REGISTRY_AUTO_CREATE` | `true` → `CREATE SCHEMA/TABLE IF NOT EXISTS` on first use |
| `ARANGO_GATEWAY_REGISTRY_TABLE` | `workspace.default.arango_gateway_registry` |
| `ARANGO_GATEWAY_REGISTRY_AUTO_CREATE` | `true` → create table + MERGE publish on startup |
| `UC_GRAPH_VOLUME_NAME` | `arango_agent_volume` |
| `UC_GRAPH_SNAPSHOT_BASE` | If unset in env key presence logic, derived path under `/Volumes/.../uc_graph_snapshots` |

### 5.3 Arango connectivity (ping, embed, proxy, import)

| Variable | Notes |
|----------|--------|
| `ARANGO_PING_BASIC_AUTH_USER` / `ARANGO_PING_BASIC_AUTH_PASSWORD` | Used for server-side Basic toward Arango; embed proxy; HTTP proxy; bulk import. Password has a **dev default** in `config.py` — replace in production. Prefer Databricks **secrets** for the deployed app. |
| `ARANGO_PING_TIMEOUT_SECONDS` | Ping timeout |
| `ARANGO_PING_TLS_VERIFY` | TLS verification for outbound Arango |
| `ARANGO_UI_IFRAME_URL` | Optional **full override** of Arango HTTP origin for embed proxy (bypasses UC row when set) |
| `ARANGO_EMBED_COOKIE_SAMESITE_NONE` | Rewrite cookies for iframe embedding |
| `ARANGO_DATABASE` | DB name for bulk import (`_system` default) |
| `ARANGO_UC_IMPORT_BATCH_SIZE` | Batch size for HTTP document posts to Arango |
| `ARANGO_HTTP_PROXY_ALLOW_ADMIN` | When `true`, allows `/_admin` paths through `POST /api/arango/http` (default `false`) |
| `ARANGO_HTTP_PROXY_TIMEOUT_SECONDS` | Proxy request timeout |

### 5.4 Dashboard / optional conversation bridge

| Variable | Notes |
|----------|--------|
| `ARANGO_CONVERSATION_URL` | If set, `POST /api/arango/chat` forwards to this URL; empty → stub behavior |
| `ARANGO_CONVERSATION_TIMEOUT_SECONDS` | Outbound timeout for that forward |

### 5.5 Diagnostics

| Variable | Notes |
|----------|--------|
| `DEBUG_STARTUP_CHECKS` | `true` → run UC registry ensure + read active row + Arango ping at startup; result in `app.extensions['startup_debug_status']` |
| `DEBUG_WEBHOOK_URL` | Optional POST of startup debug JSON |

### 5.6 Genie-related keys on `AppConfig`

`GENIE_SPACE_ID`, `GENIE_SPACE_REGISTRY_TABLE`, `GENIE_AUTO_PROVISION`, etc. are defined on `AppConfig` but **are not wired** in `arango_gateway` routes. **Do not assume** Genie behavior from this app — use **arango-mcp-app** and its `genie_registry.py` / `app.yaml`.

---

## 6. How configuration flows from **arango-platform-bundle** to this app

The monorepo bundle lives at `arango-platform-bundle/` with `databricks.yml` variables and `resources/apps.yml`.

1. **Bundle variables** (`arango-platform-bundle/databricks.yml`): e.g. `sql_warehouse_id`, `arango_registry_table`, `uc_graph_volume_name`, `uc_graph_snapshot_base`, `arango_gateway_registry_table`, plus dashboard/agent-only vars.

2. **App resource** (`resources/apps.yml` → `resources.apps.arango_gateway`):
   - `source_code_path: ../apps/arango-gateway-app` (symlink in repo layout).
   - `config.env` is **merged** with the app’s `app.yaml` `env` list. **Matching `name` entries from the bundle override** literals in `app.yaml`.

3. **Concrete overrides today** (from `resources/apps.yml`):
   - `DATABRICKS_SQL_WAREHOUSE_ID` ← `${var.sql_warehouse_id}`
   - `ARANGO_REGISTRY_TABLE` ← `${var.arango_registry_table}`
   - `UC_GRAPH_VOLUME_NAME` ← `${var.uc_graph_volume_name}`
   - `UC_GRAPH_SNAPSHOT_BASE` ← `${var.uc_graph_snapshot_base}`
   - `ARANGO_GATEWAY_REGISTRY_TABLE` ← `${var.arango_gateway_registry_table}`

4. **Standalone deploy** (`./deploy_app.sh` from this directory) does **not** run bundle substitution; it uses `databricks sync` + `databricks apps deploy` and passes **warehouse id** into SQL helper scripts. Operators must ensure `app.yaml` (or post-deploy App settings) match bundle conventions if both paths are used.

5. **This repo’s own `databricks.yml`** defines variables for solo `databricks bundle deploy` of **arango-gateway-app** only; defaults are empty strings except table names — useful for CI that injects `--var sql_warehouse_id=…`.

---

## 7. How this app cooperates with **arango-dashboard-app** and **arango-mcp-app**

### 7.1 arango-dashboard-app

- **Reads** `ARANGO_GATEWAY_REGISTRY_TABLE` (when `ARANGO_GATEWAY_BASE_URL` is not set) to discover the gateway’s public URL for iframes and same-origin fetches.
- **Reads** `ARANGO_REGISTRY_TABLE` for display / consistency (exact usage is in dashboard templates and deploy script).
- **Calls** gateway HTTP APIs for UC graph extract, streaming progress, registry init, etc. (see dashboard `app.yaml` and templates).
- Does **not** need direct network access to Arango if all Arango traffic goes through the gateway embed path and APIs.

### 7.2 arango-mcp-app

- **Reads** `ARANGO_GATEWAY_REGISTRY_TABLE` (and optional `ARANGO_GATEWAY_BASE_URL`) to find the gateway base URL for gateway-backed Arango operations.
- **Reads** `ARANGO_REGISTRY_TABLE` (SELECT) for parity with deploy grants — cluster coordinates are still “owned” by the gateway + deploy scripts for writes.
- **Calls** `POST /api/arango/http` for MCP tools that need Arango REST from a workspace-isolated identity.

### 7.3 deploy scripts and UC ownership

- `deploy_app.sh` **pre-creates** registry Delta tables and grants **`account users`** and the **gateway app service principal** so first-boot races (app UNAVAILABLE before SQL) do not fail `GRANT` with `TABLE_DOES_NOT_EXIST`.
- `update_arango_gateway_registry_uc.sh` and in-app `gateway_url_registry.py` use **MERGE** upserts so **two Gunicorn workers** plus deploy script do not leave multiple `is_active=true` rows.

---

## 8. Process model and startup order

- **Command** (`app.yaml`): `gunicorn wsgi:app` with **`--workers 2`**. Each worker runs `create_app()` independently.
- **First actions in `create_app()`:** load `AppConfig`; **`publish_self_gateway_url_to_uc_if_configured`** (UC MERGE); register routes; optionally **`run_startup_debug_check`** when `DEBUG_STARTUP_CHECKS=true`.
- **`ProxyFix`:** trusts `X-Forwarded-*` from the Databricks Apps edge so URL generation and logging see external scheme/host.

---

## 9. Conventions for agents editing this codebase

1. **Prefer UC + Statement Execution API** for registry (`databricks_sql.execute_sql`) — same auth model as the rest of the workspace app.
2. **Any new “single active row” UC writer** must use **one atomic MERGE** (or equivalent) plus retry on Delta concurrent-write errors — not `UPDATE` + `INSERT` split across statements.
3. **New Arango-facing HTTP** from workspace agents should go through **`POST /api/arango/http`** with path validation, or extend the allowlist deliberately in `arango_proxy_path.py` with security review.
4. **Embed changes** must preserve Aardvark auth behavior documented in `README.md` (`/_open/auth` without server Basic, cookie SameSite rules).
5. **Do not assume Genie** in this package — keep Genie changes in **arango-mcp-app**.

---

## 10. Quick reference — files to open for common tasks

| Task | Start here |
|------|------------|
| Add a JSON API route | `routes/api.py`, register in `app.py` if not using blueprint only |
| Change UC table schema | `arango_registry.py` / `gateway_url_registry.py` + migration note; align `deploy_app.sh` `CREATE TABLE` and `update_*_uc.sh` |
| Tighten Arango proxy | `arango_proxy_path.py` + tests if any |
| Change embed rewrite rules | `routes/embed.py` (`_rewrite_*`, `_fix_double_proxy_path_segments`) |
| Bundle env override | `arango-platform-bundle/resources/apps.yml` under `arango_gateway.config.env` |
| Laptop deploy behavior | `deploy_app.sh`, `update_arango_registry_uc.sh`, `update_arango_gateway_registry_uc.sh` |

---

## 11. Related reading

- `README.md` in this directory — operator quick start, local run, deploy notes.
- `arango-platform-bundle/README.md` — multi-app bundle layout.
- `arango-mcp-app/README_Agent.md` — agent app architecture (Genie, MCP, gateway consumption).

This file should be updated when **new `/api` routes**, **new env vars**, or **bundle variable contracts** change.
