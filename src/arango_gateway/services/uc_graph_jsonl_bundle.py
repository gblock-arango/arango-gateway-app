"""
Unity Catalog graph snapshots as gzip JSONL on Unity Catalog volumes (Approach D).

Coordination:
  * Each run uses ``runs/<run_id>/snapshot/`` under a configurable volume base path.
  * Data files are uploaded first; ``manifest.json`` is written last with SHA-256 and sizes.
  * Before (re)uploading, any existing ``manifest.json`` for that run is removed so consumers
    never trust a manifest that might not match freshly overwritten data files.
  * Optional staging dir ``.staging`` + copy into ``snapshot`` for deployments where you
    prefer a directory-level publish (see ``use_staging_directory``).

Consumers should verify every listed file’s size and checksum before loading into Arango.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from databricks.sdk import WorkspaceClient

GRAPH_SNAPSHOT_FORMAT_VERSION = 1

NODES_FILENAME = "nodes.jsonl.gz"
EDGES_FILENAME = "edges.jsonl.gz"
MANIFEST_FILENAME = "manifest.json"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_byte_length(path: str) -> int:
    return int(os.path.getsize(path))


def _write_jsonl_gzip(records: list[dict[str, Any]], path: str) -> int:
    n = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as gz:
        for rec in records:
            gz.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


def _normalize_volume_base(base: str) -> str:
    b = (base or "").strip()
    if not b.startswith("/Volumes/"):
        raise ValueError(
            "volume_base_path must be a Unity Catalog volume path starting with /Volumes/"
        )
    return b.rstrip("/")


def _safe_remote_delete_file(dbfs: Any, file_path: str) -> None:
    try:
        dbfs.delete(file_path)
    except Exception:
        pass


def _safe_remote_delete_tree(dbfs: Any, dir_path: str) -> None:
    try:
        dbfs.delete(dir_path, recursive=True)
    except Exception:
        pass


@dataclass
class LocalJsonlBundle:
    """Paths to gzipped JSONL + manifest object (not yet written to disk for manifest)."""

    work_dir: str
    nodes_path: str
    edges_path: str
    node_line_count: int
    edge_line_count: int
    nodes_sha256: str
    edges_sha256: str
    nodes_size: int
    edges_size: int
    manifest: dict[str, Any]


def build_local_gzip_jsonl_bundle(
    *,
    graph_result: dict[str, Any],
    run_id: str,
    workspace_host: str,
) -> LocalJsonlBundle:
    """
    Write ``nodes.jsonl.gz`` and ``edges.jsonl.gz`` under a temp directory and build manifest body.

    ``graph_result`` is the dict returned by :func:`datahub_unity_catalog_workflow.extract_unity_catalog_graph`.
    """
    graph = graph_result.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    work_dir = tempfile.mkdtemp(prefix="uc_graph_jsonl_")
    nodes_path = f"{work_dir}/{NODES_FILENAME}"
    edges_path = f"{work_dir}/{EDGES_FILENAME}"

    node_line_count = _write_jsonl_gzip(nodes, nodes_path)
    edge_line_count = _write_jsonl_gzip(edges, edges_path)

    nodes_sha256 = _sha256_file(nodes_path)
    edges_sha256 = _sha256_file(edges_path)
    nodes_size = _file_byte_length(nodes_path)
    edges_size = _file_byte_length(edges_path)

    summary = graph_result.get("summary") or {}
    manifest: dict[str, Any] = {
        "format_version": GRAPH_SNAPSHOT_FORMAT_VERSION,
        "run_id": run_id,
        "status": "complete",
        "created_at": graph_result.get("generated_at"),
        "completed_at": graph_result.get("generated_at"),
        "workspace_host": workspace_host,
        "source": graph_result.get("source"),
        "platform": graph_result.get("platform"),
        "platform_instance_env": graph_result.get("platform_instance_env"),
        "datahub_source_doc": graph_result.get("datahub_source_doc"),
        "extraction_options": graph_result.get("options"),
        "summary": summary,
        "warnings": graph_result.get("warnings") or [],
        "files": [
            {
                "name": NODES_FILENAME,
                "relative_path": NODES_FILENAME,
                "record_kind": "node",
                "line_count": node_line_count,
                "byte_length": nodes_size,
                "sha256": nodes_sha256,
                "content_encoding": "gzip",
                "media_type": "application/x-ndjson",
            },
            {
                "name": EDGES_FILENAME,
                "relative_path": EDGES_FILENAME,
                "record_kind": "edge",
                "line_count": edge_line_count,
                "byte_length": edges_size,
                "sha256": edges_sha256,
                "content_encoding": "gzip",
                "media_type": "application/x-ndjson",
            },
        ],
        "arangodb_hints": {
            "vertex_document_key_field": "id",
            "edge_from_id_field": "from_id",
            "edge_to_id_field": "to_id",
            "suggested_vertex_collection": "uc_graph_nodes",
            "suggested_edge_collection": "uc_graph_edges",
            "note": "Import with arangoimport or map ids to _from/_to using your vertex collection name.",
        },
        "consumer_notes": [
            "Do not load this run until manifest.json is present and checksums match after download.",
            "manifest.json is uploaded last; absence of manifest means the run is incomplete.",
        ],
    }

    return LocalJsonlBundle(
        work_dir=work_dir,
        nodes_path=nodes_path,
        edges_path=edges_path,
        node_line_count=node_line_count,
        edge_line_count=edge_line_count,
        nodes_sha256=nodes_sha256,
        edges_sha256=edges_sha256,
        nodes_size=nodes_size,
        edges_size=edges_size,
        manifest=manifest,
    )


def _upload_file(dbfs: Any, local_path: str, remote_path: str) -> None:
    with open(local_path, "rb") as f:
        dbfs.upload(remote_path, f, overwrite=True)


def _copy_remote_file(
    dbfs: Any, source_path: str, destination_path: str, *, overwrite: bool = True
) -> None:
    with dbfs.open(source_path, read=True) as reader:
        dbfs.upload(destination_path, reader, overwrite=overwrite)


def publish_local_bundle_to_uc_volume(
    *,
    workspace_client: WorkspaceClient,
    volume_base_path: str,
    run_id: str,
    bundle: LocalJsonlBundle,
    use_staging_directory: bool = False,
) -> dict[str, Any]:
    """
    Upload bundle to ``<volume_base>/runs/<run_id>/snapshot/`` (or via ``.staging`` first).

    Returns paths and checksums for the API response.
    """
    base = _normalize_volume_base(volume_base_path)
    dbfs = workspace_client.dbfs

    run_root = f"{base}/runs/{run_id}"
    snapshot_dir = f"{run_root}/snapshot"
    staging_dir = f"{run_root}/.staging"

    if use_staging_directory:
        _safe_remote_delete_tree(dbfs, staging_dir)
        dbfs.mkdirs(staging_dir)
        target_nodes = f"{staging_dir}/{NODES_FILENAME}"
        target_edges = f"{staging_dir}/{EDGES_FILENAME}"
    else:
        dbfs.mkdirs(snapshot_dir)
        target_nodes = f"{snapshot_dir}/{NODES_FILENAME}"
        target_edges = f"{snapshot_dir}/{EDGES_FILENAME}"
        _safe_remote_delete_file(dbfs, f"{snapshot_dir}/{MANIFEST_FILENAME}")

    _upload_file(dbfs, bundle.nodes_path, target_nodes)
    _upload_file(dbfs, bundle.edges_path, target_edges)

    manifest_body = dict(bundle.manifest)
    manifest_body["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest_bytes = json.dumps(manifest_body, ensure_ascii=False, indent=2).encode("utf-8")

    if use_staging_directory:
        _safe_remote_delete_tree(dbfs, snapshot_dir)
        dbfs.mkdirs(snapshot_dir)
        for name in (NODES_FILENAME, EDGES_FILENAME):
            _copy_remote_file(
                dbfs,
                f"{staging_dir}/{name}",
                f"{snapshot_dir}/{name}",
                overwrite=True,
            )
        _safe_remote_delete_file(dbfs, f"{snapshot_dir}/{MANIFEST_FILENAME}")
        dbfs.upload(
            f"{snapshot_dir}/{MANIFEST_FILENAME}",
            io.BytesIO(manifest_bytes),
            overwrite=True,
        )
        _safe_remote_delete_tree(dbfs, staging_dir)
    else:
        dbfs.upload(
            f"{snapshot_dir}/{MANIFEST_FILENAME}",
            io.BytesIO(manifest_bytes),
            overwrite=True,
        )

    return {
        "volume_base_path": base,
        "run_id": run_id,
        "snapshot_directory": snapshot_dir,
        "manifest_path": f"{snapshot_dir}/{MANIFEST_FILENAME}",
        "nodes_path": f"{snapshot_dir}/{NODES_FILENAME}",
        "edges_path": f"{snapshot_dir}/{EDGES_FILENAME}",
        "nodes_sha256": bundle.nodes_sha256,
        "edges_sha256": bundle.edges_sha256,
        "nodes_byte_length": bundle.nodes_size,
        "edges_byte_length": bundle.edges_size,
        "used_staging_directory": use_staging_directory,
    }


def cleanup_local_bundle(bundle: LocalJsonlBundle) -> None:
    shutil.rmtree(bundle.work_dir, ignore_errors=True)


def resolve_run_id(requested: str | None) -> str:
    rid = (requested or "").strip()
    if rid:
        return rid
    return str(uuid.uuid4())


def parse_jsonl_export_config(
    payload: dict[str, Any], default_volume_base: str
) -> dict[str, Any] | None:
    """
    Extract ``jsonl_export`` settings from the request body.

    Returns None if export is disabled.
    """
    raw = payload.get("jsonl_export")
    if raw is None:
        if not (default_volume_base or "").strip():
            return None
        raw = {}
    if raw is False:
        return None

    if not isinstance(raw, dict):
        raise ValueError("jsonl_export must be a JSON object or omitted")

    base = (raw.get("volume_base_path") or default_volume_base or "").strip()
    if not base:
        raise ValueError(
            "jsonl_export.volume_base_path or UC_GRAPH_SNAPSHOT_BASE env must be set to export"
        )

    return {
        "volume_base_path": base,
        "run_id": raw.get("run_id"),
        "use_staging_directory": bool(raw.get("use_staging_directory", False)),
        "include_graph_in_response": bool(raw.get("include_graph_in_response", False)),
    }
