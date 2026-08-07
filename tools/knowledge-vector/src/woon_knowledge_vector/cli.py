from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


INDEX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def main() -> None:
    try:
        request = json.load(sys.stdin)
        response = dispatch(request)
        json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    operation = required_text(request, "operation")
    if operation == "embed":
        return embed(request)
    if operation == "create_index":
        return create_index(request)
    if operation == "delete_index":
        return delete_index(request)
    if operation == "describe_index":
        return describe_index(request)
    if operation == "upsert":
        return upsert(request)
    if operation == "delete_records":
        return delete_records(request)
    if operation == "list_records":
        return list_records(request)
    if operation == "search":
        return search(request)
    raise ValueError(f"unsupported operation {operation!r}")


def embed(request: dict[str, Any]) -> dict[str, Any]:
    from fastembed import TextEmbedding

    model_name = required_text(request, "model")
    cache_dir = request.get("cache_dir") or None
    chunks = request.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("chunks must be an array")
    ids: list[str] = []
    texts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("each chunk must be an object")
        ids.append(required_text(chunk, "id"))
        texts.append(required_text(chunk, "text"))
    model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
    vectors = list(model.passage_embed(texts))
    if len(vectors) != len(ids):
        raise RuntimeError("FastEmbed returned a different vector count")
    return {
        "embeddings": [
            {"chunk_id": chunk_id, "values": vector.astype("float32").tolist()}
            for chunk_id, vector in zip(ids, vectors, strict=True)
        ]
    }


def create_index(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb
    import pyarrow as pa

    database_path = database(request)
    index = index_name(request)
    spec = request.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    dimensions = int(spec.get("embedding", {}).get("dimensions", 0))
    if dimensions <= 0:
        raise ValueError("embedding dimensions must be positive")
    schema = pa.schema(
        [
            pa.field("id", pa.string(), nullable=False),
            pa.field("source_id", pa.string(), nullable=False),
            pa.field("path", pa.string(), nullable=False),
            pa.field("ordinal", pa.int64(), nullable=False),
            pa.field("content_sha256", pa.string(), nullable=False),
            pa.field("metadata_json", pa.string(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), dimensions), nullable=False),
        ]
    )
    with write_lock(database_path):
        manifest = load_manifest(database_path)
        if index in manifest:
            raise ValueError(f"index already exists: {index}")
        connection = lancedb.connect(database_path)
        connection.create_table(index, schema=schema, mode="create")
        manifest[index] = spec
        save_manifest(database_path, manifest)
    return {"ok": True}


def delete_index(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb

    database_path = database(request)
    index = index_name(request)
    with write_lock(database_path):
        manifest = load_manifest(database_path)
        if index not in manifest:
            raise ValueError(f"unknown index: {index}")
        lancedb.connect(database_path).drop_table(index)
        del manifest[index]
        save_manifest(database_path, manifest)
    return {"ok": True}


def describe_index(request: dict[str, Any]) -> dict[str, Any]:
    database_path = database(request)
    index = index_name(request)
    manifest = load_manifest(database_path)
    if index not in manifest:
        raise ValueError(f"unknown index: {index}")
    return {"spec": manifest[index]}


def upsert(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb

    database_path = database(request)
    index = index_name(request)
    records = request.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be an array")
    rows = [record_to_row(record) for record in records]
    if not rows:
        return {"ok": True}
    with write_lock(database_path):
        require_index(database_path, index)
        table = lancedb.connect(database_path).open_table(index)
        table.delete(sql_in("id", [row["id"] for row in rows]))
        table.add(rows)
    return {"ok": True}


def delete_records(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb

    database_path = database(request)
    index = index_name(request)
    ids = request.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        raise ValueError("ids must be a non-empty string array")
    if not ids:
        return {"ok": True}
    with write_lock(database_path):
        require_index(database_path, index)
        lancedb.connect(database_path).open_table(index).delete(sql_in("id", ids))
    return {"ok": True}


def list_records(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb
    import pyarrow.compute as pc

    database_path = database(request)
    index = index_name(request)
    after = str(request.get("after", ""))
    limit = positive_int(request, "limit")
    require_index(database_path, index)
    arrow = lancedb.connect(database_path).open_table(index).to_arrow()
    if after:
        arrow = arrow.filter(pc.greater(arrow["id"], after))
    arrow = arrow.sort_by([("id", "ascending")]).slice(0, limit + 1)
    rows = arrow.to_pylist()
    has_more = len(rows) > limit
    rows = rows[:limit]
    records = [row_to_record(row) for row in rows]
    return {"records": records, "next_cursor": records[-1]["id"] if has_more else ""}


def search(request: dict[str, Any]) -> dict[str, Any]:
    import lancedb

    database_path = database(request)
    index = index_name(request)
    vector = request.get("vector")
    if not isinstance(vector, list) or not vector:
        raise ValueError("vector must be a non-empty array")
    limit = positive_int(request, "limit")
    metadata = request.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    require_index(database_path, index)
    query = lancedb.connect(database_path).open_table(index).search(vector).metric("cosine").limit(max(limit * 5, limit))
    rows = query.to_arrow().to_pylist()
    matches: list[dict[str, Any]] = []
    for row in rows:
        record = row_to_record(row)
        if any(record["metadata"].get(key) != value for key, value in metadata.items()):
            continue
        distance = float(row.get("_distance", 0.0))
        matches.append({"record": record, "score": 1.0 - distance})
        if len(matches) == limit:
            break
    return {"matches": matches}


def record_to_row(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    vector = record.get("vector")
    if not isinstance(vector, list) or not vector:
        raise ValueError("record vector must be a non-empty array")
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("record metadata must be an object")
    return {
        "id": required_text(record, "id"),
        "source_id": required_text(record, "source_id"),
        "path": str(record.get("path", "")),
        "ordinal": int(record.get("ordinal", 0)),
        "content_sha256": required_text(record, "content_sha256"),
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "vector": [float(value) for value in vector],
    }


def row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "path": row["path"],
        "ordinal": row["ordinal"],
        "content_sha256": row["content_sha256"],
        "metadata": json.loads(row["metadata_json"]),
        "vector": list(row["vector"]),
    }


def database(request: dict[str, Any]) -> str:
    value = Path(required_text(request, "database")).expanduser().resolve()
    value.mkdir(parents=True, exist_ok=True)
    return str(value)


def index_name(request: dict[str, Any]) -> str:
    value = required_text(request, "index")
    if not INDEX_NAME.fullmatch(value):
        raise ValueError(f"invalid index name: {value!r}")
    return value


def positive_int(request: dict[str, Any], key: str) -> int:
    value = int(request.get(key, 0))
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def required_text(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def require_index(database_path: str, index: str) -> dict[str, Any]:
    manifest = load_manifest(database_path)
    if index not in manifest:
        raise ValueError(f"unknown index: {index}")
    return manifest[index]


def load_manifest(database_path: str) -> dict[str, Any]:
    path = Path(database_path) / "_woon-indexes.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid LanceDB index manifest")
    return data


def save_manifest(database_path: str, manifest: dict[str, Any]) -> None:
    path = Path(database_path) / "_woon-indexes.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def write_lock(database_path: str) -> Iterator[None]:
    path = Path(database_path) / ".woon-write.lock"
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def sql_in(column: str, values: list[str]) -> str:
    escaped = ["'" + value.replace("'", "''") + "'" for value in values]
    return f"{column} IN ({','.join(escaped)})"


if __name__ == "__main__":
    main()
