"""Build and cache the four-domain graph."""

import json
from pathlib import Path

from openchatbi.workflow.registry import Registry


def build_configured_workflow(cfg, catalog, *, checkpointer=None, store=None, provider=None, mcp_tools=()):
    from openchatbi.llm.llm import get_llm
    from openchatbi.semantic.layer import SemanticLayer
    from openchatbi.text2sql.sql_graph import build_sql_graph
    from openchatbi.workflow.graph import build_workflow_graph
    from openchatbi.workflow.runtime import WorkflowRuntime

    semantic = SemanticLayer.from_config(cfg, catalog)
    registry = Registry()
    sql_graph = build_sql_graph(catalog, None, store, llm_provider=provider, prepared_only=True)
    runtime = WorkflowRuntime(cfg, catalog, semantic, sql_graph, registry, get_llm(provider), provider=provider)
    for tool in mcp_tools:
        domain = cfg.workflow.external_tool_domains.get(tool.name)
        if domain:
            registry.register_external(tool, domain)
    return build_workflow_graph(runtime, checkpointer=checkpointer, store=store)


def workflow_cache_identity(cfg):
    from openchatbi.semantic.layer import SemanticLayer
    from openchatbi.workflow.retrieval import EmbeddingSettings, catalog_documents, digest

    layer = SemanticLayer.from_config(cfg, cfg.catalog_store)
    overlay = None
    if cfg.workflow.retrieval_metadata_path:
        overlay = json.loads(Path(cfg.workflow.retrieval_metadata_path).read_text(encoding="utf-8"))
    manifest = Path(cfg.workflow.retrieval_path) / "manifest.json"
    return digest(
        {
            "contract_version": 4,
            "workflow": cfg.workflow.model_dump(),
            "warehouse": cfg.data_warehouse_config,
            "business_prompts": cfg.bi_config,
            "embedding": EmbeddingSettings.from_env().identity(),
            "documents": catalog_documents(cfg.catalog_store, layer, overlay),
            "metrics": [m.model_dump(mode="json") for m in layer.list_metrics()],
            "rules": [r.model_dump(mode="json") for r in layer.list_rules()],
            "semantic": cfg.semantic.model_dump(mode="json"),
            "snapshot": layer.snapshot_id,
            "generation": manifest.read_text(encoding="utf-8") if manifest.exists() else "missing",
            "dialect": cfg.dialect,
            "reports": cfg.report_directory,
        }
    )
