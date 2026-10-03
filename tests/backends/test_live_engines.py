"""Opt-in real-engine contract smoke tests against disposable indexes only."""

import os
import time
import unittest
import uuid

from openedx_search.backends import (
    BackendError,
    FilterTerm,
    HTTPTransport,
    IndexDefinition,
    MeilisearchBackend,
    SearchQuery,
    TypesenseBackend,
)


class LiveEngineTests(unittest.TestCase):
    """Run both engines with explicit test endpoint/key environment variables."""

    def _exercise(self, backend_type, prefix):
        url, key = os.getenv(prefix + "_TEST_URL"), os.getenv(prefix + "_TEST_KEY")
        if not url or not key:
            self.skipTest(
                f"Set {prefix}_TEST_URL and {prefix}_TEST_KEY for a disposable engine"
            )
        name = "compat_" + uuid.uuid4().hex
        transport = HTTPTransport(url, key)
        backend = backend_type(transport, IndexDefinition(name, ("title",), ("org",)))

        def finish(receipt):
            deadline = time.monotonic() + 30
            while not receipt.complete:
                receipt = backend.check_write(receipt)
                if time.monotonic() > deadline:
                    self.fail("Engine task did not complete within 30 seconds")
                if not receipt.complete:
                    time.sleep(0.1)

        try:
            finish(backend.create_index())
            finish(backend.configure_index())
            finish(
                backend.upsert(
                    [
                        {"id": "1", "title": "algebra lesson", "org": "tenant, (safe)"},
                        {"id": "2", "title": "algebra lesson", "org": "other"},
                    ]
                )
            )
            query = SearchQuery(
                "algebra",
                filters=(FilterTerm("org", ("tenant, (safe)",)),),
                page_size=250,
            )
            self.assertEqual(
                [doc["id"] for doc in backend.search(query).documents], ["1"]
            )
            self.assertEqual(
                backend.search(
                    SearchQuery(filters=(FilterTerm("org", ("absent",)),))
                ).total,
                0,
            )
            finish(
                backend.upsert(
                    [{"id": "1", "title": "geometry lesson", "org": "tenant, (safe)"}]
                )
            )
            self.assertEqual(
                [
                    doc["id"]
                    for doc in backend.search(SearchQuery("geometry")).documents
                ],
                ["1"],
            )
            finish(backend.upsert([{"id": "1", "title": "geometry lesson"}]))
            self.assertEqual(backend.search(query).total, 0)
            self.assertNotIn(
                "org", backend.search(SearchQuery("geometry")).documents[0]
            )
            finish(
                backend.upsert(
                    [
                        {"id": f"page_{number}", "title": "pagination", "org": "pages"}
                        for number in range(251)
                    ]
                )
            )
            scope = (FilterTerm("org", ("pages",)),)
            first = backend.search(SearchQuery(filters=scope, page_size=250)).documents
            second = backend.search(
                SearchQuery(filters=scope, page_size=250, page=2)
            ).documents
            self.assertEqual((len(first), len(second)), (250, 1))
            self.assertEqual(len({doc["id"] for doc in first + second}), 251)
        finally:
            path = f"/indexes/{name}" if prefix == "MEILI" else f"/collections/{name}"
            transport.request("DELETE", path, engine=backend.engine)

    def test_meilisearch(self):
        self._exercise(MeilisearchBackend, "MEILI")

    def test_typesense(self):
        self._exercise(TypesenseBackend, "TYPESENSE")

    def test_typesense_import_failure(self):
        """A real HTTP-success import with invalid documents must raise."""
        url, key = os.getenv("TYPESENSE_TEST_URL"), os.getenv("TYPESENSE_TEST_KEY")
        if not url or not key:
            self.skipTest("Set TYPESENSE_TEST_URL and TYPESENSE_TEST_KEY")
        name = "compat_failure_" + uuid.uuid4().hex
        transport = HTTPTransport(url, key)
        transport.request(
            "POST",
            "/collections",
            engine="typesense",
            body={
                "name": name,
                "fields": [{"name": "title", "type": "int32"}],
            },
        )
        try:
            backend = TypesenseBackend(transport, IndexDefinition(name, ("title",)))
            with self.assertRaisesRegex(BackendError, "partially failed"):
                backend.upsert([{"id": "1", "title": "invalid integer"}])
        finally:
            transport.request("DELETE", f"/collections/{name}", engine="typesense")
