"""Compose declarative configuration before constructing application services."""

import os
import re
from pathlib import Path

import yaml


def merge_config(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def expand_environment(value):
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        name = match.group(1)
        if not os.environ.get(name):
            raise ValueError(f"Missing configuration environment variable: {name}")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def resolve_paths(data, directory):
    """Fragment file paths belong to their declaring file; SQL URIs stay literal."""

    def resolve(mapping, key):
        value = mapping.get(key)
        if value and not Path(value).is_absolute():
            mapping[key] = str((directory / value).resolve())

    for key in ("report_directory", "vector_db_path", "bi_config_file"):
        resolve(data, key)
    catalog = data.get("catalog_store", {})
    if catalog.get("store_type") == "file_system":
        resolve(catalog, "data_path")
    for key in ("retrieval_path", "retrieval_metadata_path"):
        resolve(data.get("workflow", {}), key)
    for source in data.get("semantic", {}).get("sources", []):
        for key in ("path", "mapping_path"):
            resolve(source, key)


def read_config_file(path, *, active=(), fragment=False):
    path = Path(path).resolve()
    if path in active:
        raise ValueError("Configuration include cycle detected")
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            if data is None:
                data = {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in configuration file {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a YAML mapping")
    data = expand_environment(data)
    includes = data.pop("includes", [])
    if not isinstance(includes, list) or any(not isinstance(item, str) for item in includes):
        raise ValueError("Configuration includes must be a list of file paths")
    result = {}
    for item in includes:
        included = path.parent / item
        if not included.is_file():
            raise ValueError(f"Included configuration file does not exist: {included}")
        result = merge_config(result, read_config_file(included, active=(*active, path), fragment=True))
    if fragment or includes:
        resolve_paths(data, path.parent)
    return merge_config(result, data)
