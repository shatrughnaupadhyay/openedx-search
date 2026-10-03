"""Initial Meilisearch/Typesense document adapter slice, independent of Django."""

import json
import re
from urllib.parse import urlencode

from .contracts import (
    BackendError,
    FilterTerm,
    IndexDefinition,
    SearchQuery,
    SearchResult,
    WriteReceipt,
)
from .transport import HTTPTransport

__all__ = [
    "BackendError",
    "FilterTerm",
    "HTTPTransport",
    "IndexDefinition",
    "MeilisearchBackend",
    "SearchQuery",
    "SearchResult",
    "TypesenseBackend",
    "WriteReceipt",
]


class _Backend:
    """Shared preflight validation; never accept caller-provided filter syntax."""

    def __init__(self, transport: HTTPTransport, definition: IndexDefinition):
        self.transport = transport
        self.definition = definition

    def _request(self, method, path, **kwargs):
        return self.transport.request(method, path, engine=self.engine, **kwargs)

    def _validate_query(self, query):
        for term in query.filters:
            if term.field not in self.definition.filterable_fields:
                raise ValueError("Filter field is not declared filterable")

    def _documents(self, documents):
        documents = list(documents)
        if not 1 <= len(documents) <= 1000:
            raise ValueError("A write batch must contain 1 to 1000 documents")
        fields = set(
            self.definition.searchable_fields + self.definition.filterable_fields
        ) | {"id"}
        for document in documents:
            if not isinstance(document, dict) or set(document) - fields:
                raise ValueError("Document has undeclared fields")
            if not isinstance(document.get("id"), str) or not re.fullmatch(
                r"[A-Za-z0-9_-]+", document["id"]
            ):
                raise ValueError("Document requires an engine-portable string id")
            if any(not isinstance(value, str) for value in document.values()):
                raise ValueError("Initial schema accepts only string values")
        return documents


class MeilisearchBackend(_Backend):
    """Real Meilisearch HTTP adapter with nonblocking task receipts."""

    engine = "meilisearch"

    def _receipt(self, response):
        try:
            task_id = response["taskUid"]
            if type(task_id) is not int or task_id < 0:
                raise ValueError
            return WriteReceipt(task_id=task_id)
        except (KeyError, TypeError, ValueError):
            raise BackendError("Invalid write receipt") from None

    def create_index(self):
        """Submit creation; caller checks completion before configuring."""
        return self._receipt(
            self._request(
                "POST",
                "/indexes",
                body={
                    "uid": self.definition.name,
                    "primaryKey": "id",
                },
            )
        )

    def configure_index(self):
        """Submit searchable/filterable settings after index creation completes."""
        return self._receipt(
            self._request(
                "PATCH",
                f"/indexes/{self.definition.name}/settings",
                body={
                    "searchableAttributes": list(self.definition.searchable_fields),
                    "filterableAttributes": list(self.definition.filterable_fields),
                },
            )
        )

    def upsert(self, documents):
        """Submit a bounded replacement batch without waiting for indexing."""
        return self._receipt(
            self._request(
                "POST",
                f"/indexes/{self.definition.name}/documents",
                body=self._documents(documents),
            )
        )

    def check_write(self, receipt):
        """One poll only. Scheduling/backoff belongs to the indexing worker."""
        if receipt.complete:
            return receipt
        if type(receipt.task_id) is not int or receipt.task_id < 0:
            raise ValueError("Invalid Meilisearch task id")
        response = self._request("GET", f"/tasks/{receipt.task_id}")
        if not isinstance(response, dict):
            raise BackendError("Invalid task response")
        status = response.get("status")
        if status in ("failed", "canceled"):
            raise BackendError("Index write failed")
        if status not in ("enqueued", "processing", "succeeded"):
            raise BackendError("Unknown task status")
        return WriteReceipt(receipt.task_id, status == "succeeded")

    def search(self, query: SearchQuery):
        """Search with escaped structured exact filters and offset pagination."""
        self._validate_query(query)
        clauses = [
            "("
            + " OR ".join(
                f"{term.field} = {json.dumps(value, ensure_ascii=False)}"
                for value in term.values
            )
            + ")"
            for term in query.filters
        ]
        body = {
            "q": query.text,
            "offset": (query.page - 1) * query.page_size,
            "limit": query.page_size,
        }
        if clauses:
            body["filter"] = " AND ".join(clauses)
        response = self._request(
            "POST", f"/indexes/{self.definition.name}/search", body=body
        )
        try:
            documents = tuple(response["hits"])
            total = response["estimatedTotalHits"]
            if (
                type(total) is not int
                or total < 0
                or any(not isinstance(doc, dict) for doc in documents)
            ):
                raise ValueError
            return SearchResult(documents, total, False)
        except (KeyError, TypeError, ValueError):
            raise BackendError("Invalid search response") from None


class TypesenseBackend(_Backend):
    """Real Typesense adapter; synchronous writes are issued by workers."""

    engine = "typesense"

    def create_index(self):
        """Create a string schema with explicit searchable and filterable fields."""
        fields = dict.fromkeys(
            self.definition.searchable_fields + self.definition.filterable_fields
        )
        fields.pop("id", None)  # Typesense provides the reserved document id itself.
        self._request(
            "POST",
            "/collections",
            body={
                "name": self.definition.name,
                "fields": [
                    {
                        "name": name,
                        "type": "string",
                        "optional": True,
                        "facet": name in self.definition.filterable_fields,
                    }
                    for name in fields
                ],
            },
        )
        return WriteReceipt(complete=True)

    def configure_index(self):
        """The initial contract fixes settings at Typesense collection creation."""
        return WriteReceipt(complete=True)

    def upsert(self, documents):
        """Import NDJSON; detect per-document failures despite HTTP 200."""
        documents = self._documents(documents)
        results = self._request(
            "POST",
            f"/collections/{self.definition.name}/documents/import?action=upsert",
            body="\n".join(json.dumps(document) for document in documents),
            ndjson=True,
        )
        if (
            not isinstance(results, list)
            or len(results) != len(documents)
            or any(
                not isinstance(result, dict) or result.get("success") is not True
                for result in results
            )
        ):
            # Some documents may already be committed. Worker retries must be upserts.
            raise BackendError("Document import partially failed")
        return WriteReceipt(complete=True)

    def check_write(self, receipt):
        """Synchronous receipts need no network round-trip."""
        if not receipt.complete:
            raise ValueError("Typesense receipt must already be complete")
        return receipt

    def search(self, query: SearchQuery):
        """Render exact backtick literals and normalize document envelopes."""
        self._validate_query(query)
        clauses = [
            term.field + ":=[" + ",".join(f"`{value}`" for value in term.values) + "]"
            for term in query.filters
        ]
        params = {
            "q": query.text or "*",
            "query_by": ",".join(self.definition.searchable_fields),
            "page": query.page,
            "per_page": query.page_size,
            "max_facet_values": 1000,
        }
        if clauses:
            params["filter_by"] = " && ".join(clauses)
        response = self._request(
            "GET",
            f"/collections/{self.definition.name}/documents/search?{urlencode(params)}",
        )
        try:
            documents = tuple(hit["document"] for hit in response["hits"])
            total = response["found"]
            if (
                type(total) is not int
                or total < 0
                or any(not isinstance(doc, dict) for doc in documents)
            ):
                raise ValueError
            if response.get("search_cutoff", False):
                raise BackendError("Engine search cutoff", retryable=True)
            return SearchResult(documents, total, True)
        except (KeyError, TypeError, ValueError):
            raise BackendError("Invalid search response") from None
