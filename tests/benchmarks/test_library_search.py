"""The benchmark must reject incorrect backends rather than merely time them."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmarks.library_search import (
    CorrectnessError,
    EngineBackend,
    Query,
    ReferenceBackend,
    Result,
    check_all_pages,
    check_page,
    fixtures,
    main,
    oracle,
    percentile,
    run,
)


class BenchmarkTests(unittest.TestCase):
    """Validate reproducibility, failure detection, scope and adapter translation."""

    def setUp(self):
        self.documents = fixtures(20, 6, 42)
        self.backend = ReferenceBackend()
        self.backend.upsert(self.documents)

    def test_fixture_seed_is_reproducible(self):
        self.assertEqual(self.documents, fixtures(20, 6, 42))
        self.assertNotEqual(self.documents, fixtures(20, 6, 43))
        self.assertEqual(len({document["id"] for document in self.documents}), 120)

    def test_scope_membership_is_exact_not_prefix(self):
        matches = oracle(self.documents, Query(("library_1",)))
        self.assertEqual(len(matches), 6)
        self.assertTrue(all(document["library_id"] == "library_1" for document in matches.values()))

    def test_pagination_retrieves_all_without_order_assumption(self):
        result = check_all_pages(self.backend, self.documents, Query(("library_1", "library_2"), page_size=5))
        self.assertEqual(result["matched"], 12)
        self.assertEqual(result["pages_checked"], 4)

    def test_authorization_and_kind_are_conjunctive(self):
        result = check_all_pages(self.backend, self.documents, Query(("library_1",), "problem", page_size=1))
        self.assertEqual(result["matched"], 2)

    def test_empty_and_unknown_scope(self):
        for scope in ((), ("library_unknown",)):
            self.assertEqual(self.backend.search(Query(scope)), Result((), 0, True))

    def test_rejects_scope_leak(self):
        expected = oracle(self.documents, Query(("library_1",)))
        with self.assertRaisesRegex(CorrectnessError, "unauthorized"):
            check_page(Result((self.documents[0],), 6, True), expected, 20)

    def test_rejects_stale_payload(self):
        expected = oracle(self.documents, Query(("library_0",)))
        with self.assertRaisesRegex(CorrectnessError, "stale"):
            check_page(Result((dict(self.documents[0], title="wrong"),), 6, True), expected, 20)

    def test_rejects_duplicate_pages(self):
        repeated = Mock()
        repeated.search.return_value = Result((self.documents[0],), 6, True)
        with self.assertRaisesRegex(CorrectnessError, "duplicate"):
            check_all_pages(repeated, self.documents, Query(("library_0",), page_size=1))

    def test_rejects_truncation_even_if_count_estimated(self):
        truncated = Mock()
        truncated.search.return_value = Result((), 0, False)
        with self.assertRaisesRegex(CorrectnessError, "missing"):
            check_all_pages(truncated, self.documents, Query(("library_0",)))

    def test_detects_meilisearch_style_thousand_hit_cap(self):
        documents = fixtures(1, 1100, 42)
        capped = Mock()

        def search(query):
            start = (query.page - 1) * query.page_size
            return Result(tuple(documents[:1000][start:start + query.page_size]), 1100, False)

        capped.search.side_effect = search
        with self.assertRaisesRegex(CorrectnessError, "retrieval cap"):
            check_all_pages(capped, documents, Query(("library_0",), page_size=250))

    def test_rejects_oversized_page(self):
        expected = oracle(self.documents, Query(("library_0",)))
        with self.assertRaisesRegex(CorrectnessError, "cap"):
            check_page(Result(tuple(self.documents[:6]), 6, True), expected, 5)

    def test_rejects_false_exact_count(self):
        expected = oracle(self.documents, Query(("library_0",)))
        with self.assertRaisesRegex(CorrectnessError, "count"):
            check_page(Result((), 0, True), expected, 20)

    def test_query_validation_caps(self):
        for values in ({"page": 0}, {"page": True}, {"page_size": 251}, {"page_size": 0}):
            with self.assertRaises(ValueError):
                Query(("library_1",), **values)

    def test_reference_report_is_explicit_and_mutations_converge(self):
        report = run(ReferenceBackend(), libraries=10, documents_per_library=6,
                     authorized_libraries=2, iterations=4, warmup=1, page_size=5)
        self.assertEqual(report["measurement_kind"], "in_memory_reference_only")
        self.assertEqual(report["correctness"]["cases"]["mutation_convergence"]["matched"], 11)
        self.assertEqual(len(report["timings"]["search_ms"]["samples"]), 4)

    def test_nearest_rank_percentiles(self):
        self.assertEqual(percentile(list(range(1, 101)), 50), 50)
        self.assertEqual(percentile(list(range(1, 101)), 95), 95)
        with self.assertRaises(ValueError):
            percentile([], 50)

    def test_adapter_translation_and_empty_scope_without_network(self):
        adapter = EngineBackend.__new__(EngineBackend)
        adapter.api = SimpleNamespace(FilterTerm=lambda field, values: (field, values), SearchQuery=lambda **kwargs: kwargs)
        adapter.engine = Mock()
        self.assertEqual(adapter.search(Query(())), Result((), 0, True))
        adapter.engine.search.assert_not_called()
        adapter.search(Query(("library_1", "library_10"), "problem", page=3, page_size=250))
        adapter.engine.search.assert_called_once_with({
            "filters": (("library_id", ("library_1", "library_10")), ("kind", ("problem",))),
            "page": 3, "page_size": 250,
        })

    def test_retried_mutation_does_not_duplicate(self):
        changed = dict(self.documents[0], library_id="library_9")
        self.backend.upsert([changed])
        self.backend.upsert([changed])
        self.assertEqual(len(self.backend.documents), len(self.documents))
        self.assertEqual(len(self.backend.search(Query(("library_0",))).documents), 5)

    def test_failed_rerun_removes_stale_success_report(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text('{"correctness": {"status": "passed"}}')
            arguments = ["benchmark", "--engine", "meilisearch", "--adapter-source", directory,
                         "--url", "http://localhost:7700", "--output", str(output)]
            with patch("sys.argv", arguments), self.assertRaisesRegex(ValueError, "adapter-source"):
                main()
            self.assertFalse(output.exists())

    def fake_adapter(self, directory, response):
        """Keep transport and adapter task polling independently observable."""
        package = Path(directory) / "openedx_search/backends"
        package.mkdir(parents=True)
        for filename in ("__init__.py", "contracts.py", "transport.py"):
            (package / filename).write_text("# synthetic adapter\n")
        receipt = lambda **kwargs: SimpleNamespace(complete=False, **kwargs)
        engine = Mock()
        engine.create_index.return_value = SimpleNamespace(complete=True)
        engine.configure_index.return_value = SimpleNamespace(complete=True)
        engine.transport.request.return_value = response
        engine.check_write.return_value = SimpleNamespace(complete=True)
        api = SimpleNamespace(
            __file__=str(package / "__init__.py"),
            IndexDefinition=lambda name, *args: SimpleNamespace(name=name),
            HTTPTransport=Mock(), MeilisearchBackend=Mock(return_value=engine),
            WriteReceipt=receipt,
        )
        return api, engine

    def test_installed_adapter_requires_no_sibling_workspace(self):
        with TemporaryDirectory() as directory:
            api, engine = self.fake_adapter(directory, {"taskUid": 35})
            with patch("benchmarks.library_search.importlib.import_module", return_value=api):
                backend = EngineBackend("meilisearch", None, "http://localhost:7700", "disposable", 1)
            self.assertEqual(backend.api, api)
            engine.create_index.assert_called_once()

    def test_explicit_meilisearch_ceiling_uses_transport_and_waits(self):
        with TemporaryDirectory() as directory:
            api, engine = self.fake_adapter(directory, {"taskUid": 35})
            with patch("benchmarks.library_search.importlib.import_module", return_value=api), patch.object(sys, "path", []):
                backend = EngineBackend("meilisearch", directory, "http://localhost:7700", "disposable", 1, 9006)
            engine.transport.request.assert_called_once_with(
                "PATCH", "/indexes/disposable/settings",
                body={"pagination": {"maxTotalHits": 9006}}, engine="meilisearch",
            )
            self.assertEqual(engine.check_write.call_args.args[0].task_id, 35)
            self.assertEqual(backend.settings, {"pagination": {"maxTotalHits": 9006}})
            self.assertEqual([call[0] for call in engine.mock_calls], [
                "create_index", "configure_index", "transport.request", "check_write",
            ])

    def test_meilisearch_settings_receipt_is_validated(self):
        for response in ({}, {"taskUid": True}, {"taskUid": -1}, {"taskUid": "35"}):
            with TemporaryDirectory() as directory:
                api, engine = self.fake_adapter(directory, response)
                with patch("benchmarks.library_search.importlib.import_module", return_value=api), patch.object(sys, "path", []), self.assertRaisesRegex(ValueError, "receipt"):
                    EngineBackend("meilisearch", directory, "http://localhost:7700", "disposable", 1, 9006)
                engine.check_write.assert_not_called()

    def test_invalid_ceiling_rejected_before_import_or_creation(self):
        with patch("benchmarks.library_search.importlib.import_module") as loader:
            for ceiling in (0, -1, True, "10"):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    EngineBackend("meilisearch", "/nonexistent", "http://localhost", "disposable", 1, ceiling)
            with self.assertRaisesRegex(ValueError, "only to Meilisearch"):
                EngineBackend("typesense", "/nonexistent", "http://localhost", "disposable", 1, 1000)
            loader.assert_not_called()

    def test_cli_ceiling_rejected_before_backend_creation(self):
        with TemporaryDirectory() as directory, patch("benchmarks.library_search.EngineBackend") as engine:
            for mode, ceiling in (("typesense", "1000"), ("meilisearch", "0")):
                arguments = ["benchmark", "--engine", mode, "--meilisearch-max-total-hits", ceiling,
                             "--output", str(Path(directory) / "report.json")]
                with patch("sys.argv", arguments), patch("sys.stderr"), self.assertRaises(SystemExit):
                    main()
            engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
