from __future__ import annotations

import tempfile
import unittest

from woon_knowledge_vector.cli import dispatch


class LanceDBAdapterTest(unittest.TestCase):
    def test_create_upsert_list_search_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as database:
            spec = {
                "contract_version": 1,
                "name": "knowledge-v1",
                "embedding": {
                    "adapter": "fixture",
                    "model": "fixture",
                    "revision": "v1",
                    "dimensions": 2,
                    "normalization": "l2",
                },
                "distance": "cosine",
            }
            dispatch({"operation": "create_index", "database": database, "index": "knowledge-v1", "spec": spec})
            records = [
                {
                    "id": "chunk-a",
                    "source_id": "src-a",
                    "path": "a.md",
                    "ordinal": 0,
                    "content_sha256": "sha-a",
                    "metadata": {"state": "active"},
                    "vector": [1.0, 0.0],
                },
                {
                    "id": "chunk-b",
                    "source_id": "src-b",
                    "path": "b.md",
                    "ordinal": 0,
                    "content_sha256": "sha-b",
                    "metadata": {"state": "active"},
                    "vector": [0.0, 1.0],
                },
            ]
            dispatch({"operation": "upsert", "database": database, "index": "knowledge-v1", "records": records})
            page = dispatch({"operation": "list_records", "database": database, "index": "knowledge-v1", "after": "", "limit": 1})
            self.assertEqual(["chunk-a"], [record["id"] for record in page["records"]])
            self.assertEqual("chunk-a", page["next_cursor"])
            matches = dispatch(
                {
                    "operation": "search",
                    "database": database,
                    "index": "knowledge-v1",
                    "vector": [1.0, 0.0],
                    "limit": 1,
                    "metadata": {"state": "active"},
                }
            )
            self.assertEqual("chunk-a", matches["matches"][0]["record"]["id"])
            dispatch({"operation": "delete_records", "database": database, "index": "knowledge-v1", "ids": ["chunk-a"]})
            page = dispatch({"operation": "list_records", "database": database, "index": "knowledge-v1", "after": "", "limit": 10})
            self.assertEqual(["chunk-b"], [record["id"] for record in page["records"]])
            dispatch({"operation": "delete_index", "database": database, "index": "knowledge-v1"})

    def test_ten_thousand_records_are_pageable(self) -> None:
        with tempfile.TemporaryDirectory() as database:
            spec = {
                "contract_version": 1,
                "name": "knowledge-scale-v1",
                "embedding": {
                    "adapter": "fixture",
                    "model": "fixture",
                    "revision": "v1",
                    "dimensions": 2,
                    "normalization": "l2",
                },
                "distance": "cosine",
            }
            dispatch({"operation": "create_index", "database": database, "index": "knowledge-scale-v1", "spec": spec})
            records = [
                {
                    "id": f"chunk-{index:05d}",
                    "source_id": f"src-{index:05d}",
                    "path": f"note-{index:05d}.md",
                    "ordinal": 0,
                    "content_sha256": f"sha-{index:05d}",
                    "metadata": {"state": "active"},
                    "vector": [1.0, 0.0],
                }
                for index in range(10_000)
            ]
            dispatch({"operation": "upsert", "database": database, "index": "knowledge-scale-v1", "records": records})

            count = 0
            cursor = ""
            while True:
                page = dispatch(
                    {
                        "operation": "list_records",
                        "database": database,
                        "index": "knowledge-scale-v1",
                        "after": cursor,
                        "limit": 500,
                    }
                )
                count += len(page["records"])
                cursor = page["next_cursor"]
                if not cursor:
                    break
            self.assertEqual(10_000, count)


if __name__ == "__main__":
    unittest.main()
