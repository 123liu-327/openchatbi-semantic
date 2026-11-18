"""Shared configuration selection for the workbench and semantic index builder."""

import os
from pathlib import Path


def select_config_file(root: Path) -> Path:
    requested = os.getenv("CONFIG_FILE", "").strip()
    if requested:
        path = Path(requested)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise ValueError(f"CONFIG_FILE does not exist: {path}")
        return path.resolve()

    generated = root / "data/semantic_demo/chat-config.yaml"
    model = os.getenv("CHATBI_MODEL", "").strip()
    if model:
        from scripts.run_semantic_demo import prepare

        prepare(generated.parent, model=model)
    elif not generated.exists():
        raise ValueError("Set CONFIG_FILE, or fill CHATBI_MODEL in .env to prepare the demo configuration")
    return generated
