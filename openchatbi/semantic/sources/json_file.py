"""JSON metric adapter; legacy labels require an explicit data mapping."""

import json
import re
from pathlib import Path

from openchatbi.semantic.models import MetricSpec
from openchatbi.semantic.sources import SemanticSourceError
from openchatbi.semantic.sources.yaml_file import YamlJoinSource


class JsonMetricSource:
    def __init__(
        self, path: str | Path, *, config_path: str | Path | None = None, mapping_path: str | Path | None = None
    ) -> None:
        self._path = YamlJoinSource(path, config_path=config_path).path
        self._mapping = YamlJoinSource(mapping_path, config_path=config_path) if mapping_path else None

    def load_metrics(self) -> tuple[MetricSpec, ...]:
        try:
            text = self._path.read_text(encoding="utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("UTF-8 BOM is not allowed")
            data = json.loads(text)
            if "metrics" in data:
                records = data["metrics"]
            else:
                if self._mapping is None:
                    raise ValueError("legacy indicators require mapping_path; otherwise use the migration CLI")
                mapping = self._mapping.document()
                names = mapping["metric_names"]
                records = []
                for row in data["indicators"]:
                    label = row["name"]
                    multi = label in mapping["multi_step_metrics"]
                    sql = None if multi else row["sql_template"]
                    time_field = None if multi else row.get("time_field")
                    aliases = {
                        alias: table
                        for table, alias in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql or "", re.I)
                        if alias.upper() not in {"JOIN", "WHERE", "ON", "GROUP", "LEFT", "INNER"}
                    }
                    if time_field and "." in time_field:
                        alias, column = time_field.split(".", 1)
                        time_field = f"{aliases.get(alias, alias)}.{column}"
                    records.append(
                        {
                            "name": names[label],
                            "label": label,
                            "aliases": list(
                                dict.fromkeys(
                                    [
                                        label,
                                        *row.get("aliases", []),
                                        *mapping.get("metric_aliases", {}).get(names[label], []),
                                    ]
                                )
                            ),
                            "level": mapping["levels"][row["level"]],
                            "definition": row["definition"] + ("\n" + row["notes"] if row.get("notes") else ""),
                            "sql_template": sql,
                            "time_field": time_field,
                            "requires_multi_step": multi,
                            "depends_on": [names[name] for name in row.get("depends_on", [])],
                            "source_tables": re.split(r"\s+(?:JOIN|\+)\s+", row["data_source"]),
                            "filters": row.get("filters", []),
                            "provenance": f"{self._path}#indicators:{label}",
                        }
                    )
            return YamlJoinSource(self._path).records(MetricSpec, "metrics", {"metrics": records})
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise SemanticSourceError(f"{self._path} [metrics]: {exc}") from exc
