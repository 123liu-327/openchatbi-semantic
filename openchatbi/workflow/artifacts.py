"""Request-scoped datasets and deterministic file serialization."""

import csv
import io
import uuid
from pathlib import Path

from openchatbi.workflow.models import DataSet, FileArtifact


def dataset_from_csv(content: str, source_task: str) -> DataSet:
    reader = csv.reader(io.StringIO(content))
    columns = next(reader, [])
    return DataSet(id=uuid.uuid4().hex, columns=columns, rows=list(reader), source_task=source_task)


def save_csv(dataset: DataSet, directory: str, title: str) -> FileArtifact:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(char for char in title if char.isalnum() or char in "-_")[:60] or "query"
    filename = f"{safe_title}_{uuid.uuid4().hex}.csv"
    target = root / filename
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(dataset.columns)
        writer.writerows(dataset.rows)
    return FileArtifact(
        filename=filename, mime_type="text/csv", download_url=f"/api/download/report/{filename}", rows=len(dataset.rows)
    )
