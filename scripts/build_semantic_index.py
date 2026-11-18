"""Explicitly build the new workflow's Chroma metadata index (may call Qwen)."""

import json
import os
from pathlib import Path


def main():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    from scripts.workbench_config import select_config_file

    os.environ["CONFIG_FILE"] = str(select_config_file(Path(__file__).resolve().parents[1]))
    from openchatbi import config
    from openchatbi.semantic.layer import SemanticLayer
    from openchatbi.workflow.retrieval import RetrievalService

    cfg = config.get()
    layer = SemanticLayer.from_config(cfg, cfg.catalog_store)
    overlay = None
    if cfg.workflow.retrieval_metadata_path:
        overlay = json.loads(Path(cfg.workflow.retrieval_metadata_path).read_text(encoding="utf-8"))
    service = RetrievalService(cfg.catalog_store, layer, cfg.workflow.retrieval_path, overlay=overlay)
    manifest = service.build()
    print(
        json.dumps(
            {
                "generation": manifest["generation"],
                "counts": manifest["counts"],
                "snapshot_id": manifest["snapshot_id"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
