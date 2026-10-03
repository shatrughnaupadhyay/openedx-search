"""Adversarial contract tests; these do not establish engine parity."""

import unittest
from urllib.parse import parse_qs, urlsplit

from openedx_search.backends import (
    BackendError,
    FilterTerm,
    IndexDefinition,
    MeilisearchBackend,
    SearchQuery,
    TypesenseBackend,
    WriteReceipt,
)


class RecordingTransport:
    """Record exact protocol calls and return deliberately chosen envelopes."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


class AdapterTests(unittest.TestCase):
    """Validate query construction, pagination, receipts and partial failures."""

    def setUp(self):
        self.definition = IndexDefinition(
            "library-test", ("title",), ("library_id", "org")
        )

    def test_meili_literal_injection_is_quoted(self):
        transport = RecordingTransport({"hits": [], "estimatedTotalHits": 0})
        backend = MeilisearchBackend(transport, self.definition)
        result = backend.search(
            SearchQuery(filters=(FilterTerm("org", ('x" OR org = "secret',)),))
        )
        self.assertEqual(
            transport.calls[0][2]["body"]["filter"], '(org = "x\\" OR org = \\"secret")'
        )
        self.assertFalse(result.total_is_exact)

    def test_typesense_reserved_syntax_stays_in_literal(self):
        transport = RecordingTransport({"hits": [], "found": 0})
        backend = TypesenseBackend(transport, self.definition)
        backend.search(
            SearchQuery(filters=(FilterTerm("org", ("a], org:=secret || [b", "é, x")),))
        )
        params = parse_qs(urlsplit(transport.calls[0][1]).query)
        self.assertEqual(params["filter_by"], ["org:=[`a], org:=secret || [b`,`é, x`]"])
        self.assertEqual(params["q"], ["*"])

    def test_reject_unrepresentable_filters_and_fields(self):
        for value in ("` OR true", "x\\y", "a\nb", "", 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FilterTerm("org", (value,))
        for field in ("org OR title", "org:=x", "org[0]"):
            with self.assertRaises(ValueError):
                FilterTerm(field, ("x",))
        with self.assertRaises(ValueError):
            FilterTerm("org", ())

    def test_undeclared_filter_fails_before_network(self):
        transport = RecordingTransport({})
        for engine in (MeilisearchBackend, TypesenseBackend):
            with self.assertRaises(ValueError):
                engine(transport, self.definition).search(
                    SearchQuery(filters=(FilterTerm("private", ("x",)),))
                )
        self.assertEqual(transport.calls, [])

    def test_page_bounds_and_boolean_rejection(self):
        for kwargs in (
            {"page_size": 251},
            {"page_size": 0},
            {"page": 0},
            {"page": True},
        ):
            with self.assertRaises(ValueError):
                SearchQuery(**kwargs)

    def test_pagination_and_response_mapping(self):
        transport = RecordingTransport(
            {"hits": [{"id": "1"}], "estimatedTotalHits": 11}
        )
        result = MeilisearchBackend(transport, self.definition).search(
            SearchQuery(page=3, page_size=5)
        )
        self.assertEqual(transport.calls[-1][2]["body"]["offset"], 10)
        self.assertEqual(result.documents, ({"id": "1"},))
        transport.response = {"hits": [{"document": {"id": "2"}}], "found": 1}
        result = TypesenseBackend(transport, self.definition).search(
            SearchQuery(page_size=250)
        )
        self.assertTrue(result.total_is_exact)
        self.assertEqual(result.documents, ({"id": "2"},))

    def test_typesense_partial_import_is_failure(self):
        transport = RecordingTransport(
            [{"success": True}, {"success": False, "error": "secret"}]
        )
        with self.assertRaisesRegex(BackendError, "partially failed"):
            TypesenseBackend(transport, self.definition).upsert(
                [{"id": "1"}, {"id": "2"}]
            )
        self.assertTrue(transport.calls[-1][2]["ndjson"])

    def test_bad_document_batch_has_no_side_effects(self):
        transport = RecordingTransport({})
        for docs in (
            [],
            [{"id": "1", "secret": "x"}],
            [{"id": 1}],
            [{"id": "1", "title": []}],
        ):
            with self.assertRaises(ValueError):
                MeilisearchBackend(transport, self.definition).upsert(docs)
        self.assertEqual(transport.calls, [])

    def test_meili_poll_never_sleeps_or_loops(self):
        transport = RecordingTransport({"status": "processing"})
        backend = MeilisearchBackend(transport, self.definition)
        receipt = backend.check_write(WriteReceipt(task_id=4))
        self.assertFalse(receipt.complete)
        self.assertEqual(len(transport.calls), 1)
        transport.response = {"status": "succeeded"}
        self.assertTrue(backend.check_write(receipt).complete)
        transport.response = {"status": "failed", "error": {"message": "private data"}}
        with self.assertRaisesRegex(BackendError, "^Index write failed$"):
            backend.check_write(receipt)

    def test_typesense_receipts_do_not_poll(self):
        transport = RecordingTransport({})
        backend = TypesenseBackend(transport, self.definition)
        self.assertTrue(backend.check_write(WriteReceipt(complete=True)).complete)
        self.assertEqual(transport.calls, [])

    def test_malformed_search_and_cutoff_are_errors(self):
        for engine, response in (
            (MeilisearchBackend, {"hits": [], "estimatedTotalHits": "4"}),
            (TypesenseBackend, {"hits": [{}], "found": 3}),
            (TypesenseBackend, {"hits": [], "found": 3, "search_cutoff": True}),
        ):
            with self.assertRaises(BackendError):
                engine(RecordingTransport(response), self.definition).search(
                    SearchQuery()
                )


if __name__ == "__main__":
    unittest.main()
