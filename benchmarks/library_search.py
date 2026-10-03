"""Reproducible oracle checks and timing; run with python -m benchmarks.library_search."""

# Primitive integer checks deliberately reject boolean limits and receipts.
# pylint: disable=unidiomatic-typecheck

import argparse
import hashlib
import importlib
import json
import math
import os
import platform
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class CorrectnessError(RuntimeError):
    """An engine returned missing, duplicated, stale, or unauthorized documents."""


@dataclass(frozen=True)
class Query:
    """Exact trusted library scope plus ANDed authoring filters."""

    libraries: tuple[str, ...]
    kind: str | None = None
    page: int = 1
    page_size: int = 20

    def __post_init__(self):
        """Validate primitive integer pagination bounds."""
        if type(self.page) is not int or self.page < 1:
            raise ValueError("page must be a positive integer")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 250:
            raise ValueError("page_size must be between 1 and 250")


@dataclass(frozen=True)
class Result:
    """Backend-neutral result without assuming relevance ordering."""

    documents: tuple[dict, ...]
    total: int
    total_is_exact: bool


def fixtures(libraries, documents_per_library, seed):
    """Generate deterministic flat strings accepted by both initial adapters."""
    if libraries < 1 or documents_per_library < 1:
        raise ValueError("fixture dimensions must be positive")
    rng = random.Random(seed)
    return [
        {
            "id": f"doc_{library}_{offset}",
            "library_id": f"library_{library}",
            "kind": ("problem", "video", "html")[offset % 3],
            "title": f"Synthetic {rng.choice(('algebra', 'biology', 'history'))} lesson {offset}",
        }
        for library in range(libraries)
        for offset in range(documents_per_library)
    ]


def oracle(documents, query):
    """Independent expected set: exact scope membership and conjunctive kind."""
    allowed = frozenset(query.libraries)
    return {
        document["id"]: document
        for document in documents
        if document["library_id"] in allowed and (query.kind is None or document["kind"] == query.kind)
    }


class ReferenceBackend:
    """Linear in-memory implementation; timings are NOT search-engine measurements."""

    mode = "reference"

    def __init__(self):
        """Initialize an empty reference document store."""
        self.documents = {}

    def upsert(self, documents):
        """Immediately apply idempotent writes."""
        for document in documents:
            self.documents[document["id"]] = dict(document)

    def search(self, query):
        """Return exact authorized matches with stable ID pagination."""
        allowed = set(query.libraries)
        matches = sorted(
            (
                dict(document)
                for document in self.documents.values()
                if document["library_id"] in allowed and (query.kind is None or document["kind"] == query.kind)
            ),
            key=lambda document: document["id"],
        )
        start = (query.page - 1) * query.page_size
        return Result(tuple(matches[start:start + query.page_size]), len(matches), True)


class EngineBackend:
    """Use installed document adapters, with an optional development source path."""

    # Preserve the development adapter's positional configuration interface.
    # pylint: disable-next=too-many-positional-arguments
    def __init__(self, mode, source, url, index_name, timeout, meilisearch_max_total_hits=None):
        """Configure the adapter, disposable index and optional retrieval ceiling."""
        if meilisearch_max_total_hits is not None:
            if mode != "meilisearch":
                raise ValueError("maxTotalHits applies only to Meilisearch")
            if type(meilisearch_max_total_hits) is not int or meilisearch_max_total_hits < 1:
                raise ValueError("maxTotalHits must be a positive integer")
        if source is not None:
            source = Path(source).resolve()
            adapter_file = source / "openedx_search/backends/__init__.py"
            if not adapter_file.is_file():
                raise ValueError("adapter-source must contain openedx_search/backends")
            sys.path.insert(0, str(source))
        api = importlib.import_module("openedx_search.backends")
        installed_file = Path(api.__file__).resolve()
        if source is not None and installed_file != adapter_file:
            raise ValueError("A different adapter is already imported; use a fresh process")
        adapter_file = installed_file
        source = adapter_file.parents[2]
        self.api = api
        self.mode = mode
        self.timeout = timeout
        self.settings = {}
        self.provenance = {
            "source": str(source),
            "adapter_sha256": hashlib.sha256(adapter_file.read_bytes()).hexdigest(),
            "contracts_sha256": hashlib.sha256((adapter_file.parent / "contracts.py").read_bytes()).hexdigest(),
            "transport_sha256": hashlib.sha256((adapter_file.parent / "transport.py").read_bytes()).hexdigest(),
        }
        definition = api.IndexDefinition(index_name, ("title",), ("library_id", "kind"))
        engine_type = api.MeilisearchBackend if mode == "meilisearch" else api.TypesenseBackend
        key = os.environ.get("BENCHMARK_ENGINE_API_KEY", "")
        self.engine = engine_type(api.HTTPTransport(url, key), definition)
        self.wait(self.engine.create_index())
        self.wait(self.engine.configure_index())
        if meilisearch_max_total_hits is not None:
            settings = {"pagination": {"maxTotalHits": meilisearch_max_total_hits}}
            response = self.engine.transport.request(
                "PATCH",
                f"/indexes/{definition.name}/settings",
                body=settings,
                engine=mode,
            )
            task_id = response.get("taskUid") if isinstance(response, dict) else None
            if type(task_id) is not int or task_id < 0:
                raise ValueError("Invalid Meilisearch settings task receipt")
            self.wait(api.WriteReceipt(task_id=task_id))
            self.settings = settings

    def wait(self, receipt):
        """Bound asynchronous task polling; never equate submission with completion."""
        deadline = time.monotonic() + self.timeout
        while not receipt.complete:
            if time.monotonic() >= deadline:
                raise TimeoutError("Write did not converge within timeout")
            receipt = self.engine.check_write(receipt)
            if not receipt.complete:
                time.sleep(0.05)

    def upsert(self, documents):
        """Use the adapter's maximum 1000-document write batch."""
        for offset in range(0, len(documents), 1000):
            self.wait(self.engine.upsert(documents[offset:offset + 1000]))

    def search(self, query):
        """Empty authorization scope fails closed without an engine call."""
        if not query.libraries:
            return Result((), 0, True)
        filters = [self.api.FilterTerm("library_id", query.libraries)]
        if query.kind is not None:
            filters.append(self.api.FilterTerm("kind", (query.kind,)))
        return self.engine.search(
            self.api.SearchQuery(
                filters=tuple(filters),
                page=query.page,
                page_size=query.page_size,
            )
        )


def check_page(result, expected, page_size, seen=None):
    """Check zero leaks, correct document payloads, pagination bounds and duplicates."""
    if len(result.documents) > page_size:
        raise CorrectnessError("page-size cap violated")
    if result.total_is_exact and result.total != len(expected):
        raise CorrectnessError("exact count disagrees with oracle")
    seen = set() if seen is None else seen
    for document in result.documents:
        identifier = document.get("id")
        if identifier not in expected:
            raise CorrectnessError("unauthorized or nonmatching document returned")
        if document != expected[identifier]:
            raise CorrectnessError("stale or incorrect document payload returned")
        if identifier in seen:
            raise CorrectnessError("duplicate document across pagination")
        seen.add(identifier)
    return seen


def check_all_pages(backend, documents, query):
    """Compare retrieved IDs with oracle, independent of backend relevance order."""
    expected = oracle(documents, query)
    seen = set()
    pages = math.ceil(len(expected) / query.page_size)
    exact_counts = True
    for page in range(1, pages + 2):
        result = backend.search(Query(query.libraries, query.kind, page, query.page_size))
        exact_counts &= result.total_is_exact
        check_page(result, expected, query.page_size, seen)
        if page > pages and result.documents:
            raise CorrectnessError("documents returned beyond expected last page")
    if seen != set(expected):
        raise CorrectnessError("missing authorized documents or engine retrieval cap reached")
    return {"matched": len(seen), "pages_checked": pages + 1, "counts_exact": exact_counts}


def percentile(samples, percentage):
    """Nearest-rank percentiles, documented and reproducible."""
    if not samples:
        raise ValueError("percentile needs samples")
    return sorted(samples)[max(0, math.ceil(len(samples) * percentage / 100) - 1)]


def run(
    backend,
    *,
    libraries=1000,
    documents_per_library=6,
    seed=42,
    authorized_libraries=20,
    iterations=30,
    warmup=5,
    page_size=20,
):
    """Run scoped correctness, repeat-query timings and authorization mutation checks."""
    if not 1 <= authorized_libraries <= libraries or iterations < 1 or warmup < 0:
        raise ValueError("invalid scope or iteration count")
    documents = fixtures(libraries, documents_per_library, seed)
    fixture_hash = hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()
    scope = tuple(f"library_{index}" for index in range(authorized_libraries))
    query = Query(scope, "problem", page_size=page_size)
    started = time.perf_counter()
    backend.upsert(documents)
    ingest_seconds = time.perf_counter() - started
    correctness = {
        "all_authorized": check_all_pages(backend, documents, Query(scope, page_size=page_size)),
        "kind_and_scope": check_all_pages(backend, documents, query),
        "empty_scope": check_all_pages(backend, documents, Query((), page_size=page_size)),
        "unknown_scope": check_all_pages(backend, documents, Query(("library_unknown",), page_size=page_size)),
        "maximum_page_size": check_all_pages(backend, documents, Query(scope, "problem", page_size=250)),
    }
    expected = oracle(documents, query)
    timings = []
    for iteration in range(warmup + iterations):
        started = time.perf_counter()
        result = backend.search(query)
        elapsed = (time.perf_counter() - started) * 1000
        check_page(result, expected, page_size)
        if iteration >= warmup:
            timings.append(elapsed)
    # Revoke by moving a document outside the trusted scope; also update a payload
    # and retry the identical upsert. This is content convergence, not ACL integration.
    moved = dict(documents[0], library_id="library_revoked", title="Revoked content")
    changed = dict(documents[1] if len(documents) > 1 else documents[0], title="Updated synthetic content")
    mutations = [changed, moved]
    started = time.perf_counter()
    backend.upsert(mutations)
    backend.upsert(mutations)
    current = {document["id"]: document for document in documents}
    current.update({document["id"]: document for document in mutations})
    correctness["mutation_convergence"] = check_all_pages(
        backend,
        list(current.values()),
        Query(scope, page_size=page_size),
    )
    mutation_seconds = time.perf_counter() - started
    return {
        "schema_version": 1,
        "mode": backend.mode,
        "measurement_kind": "in_memory_reference_only" if backend.mode == "reference" else "real_engine_http",
        "performance_claim": (
            "No real-engine scalability claim"
            if backend.mode == "reference"
            else "Client-observed local engine timings; record engine/host metadata before comparison"
        ),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "adapter": getattr(backend, "provenance", None),
        "parameters": {
            "libraries": libraries,
            "documents_per_library": documents_per_library,
            "documents": len(documents),
            "seed": seed,
            "authorized_libraries": authorized_libraries,
            "iterations": iterations,
            "warmup": warmup,
            "page_size": page_size,
        },
        "fixture_sha256": fixture_hash,
        "correctness": {
            "status": "passed",
            "cases": correctness,
            "authorization_leaks": 0,
            "validation_scope": (
                "Explicit exact query filters with backend/admin credentials; "
                "no signed-token, DB-grant or ACL-revocation enforcement is verified"
            ),
        },
        "timings": {
            "ingest_seconds": ingest_seconds,
            "mutation_and_verification_seconds": mutation_seconds,
            "search_ms": {
                "p50": percentile(timings, 50),
                "p95": percentile(timings, 95),
                "min": min(timings),
                "max": max(timings),
                "samples": timings,
            },
        },
    }


def main():
    """Write report only after every correctness check succeeds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("reference", "meilisearch", "typesense"), default="reference")
    parser.add_argument("--adapter-source", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--index-name", default=f"library_benchmark_{time.time_ns()}")
    parser.add_argument("--write-timeout", type=float, default=60)
    parser.add_argument(
        "--meilisearch-max-total-hits",
        type=int,
        help="Explicit retrieval ceiling for this disposable Meilisearch index only",
    )
    parser.add_argument("--libraries", type=int, default=1000)
    parser.add_argument("--documents-per-library", type=int, default=6)
    parser.add_argument("--authorized-libraries", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Re-runs must never leave a previous success artifact attached to a failed run.
    args.output.unlink(missing_ok=True)
    if args.meilisearch_max_total_hits is not None and (
        args.engine != "meilisearch" or args.meilisearch_max_total_hits < 1
    ):
        parser.error("meilisearch-max-total-hits requires Meilisearch and a positive integer")
    if args.engine == "reference":
        backend = ReferenceBackend()
    else:
        if not args.url:
            parser.error("real engines require --url and an installed adapter dependency")
        if args.write_timeout <= 0:
            parser.error("write-timeout must be positive")
        backend = EngineBackend(
            args.engine,
            args.adapter_source,
            args.url,
            args.index_name,
            args.write_timeout,
            args.meilisearch_max_total_hits,
        )
    report = run(
        backend,
        libraries=args.libraries,
        documents_per_library=args.documents_per_library,
        authorized_libraries=args.authorized_libraries,
        seed=args.seed,
        iterations=args.iterations,
        warmup=args.warmup,
        page_size=args.page_size,
    )
    if args.engine != "reference":
        report["index_name"] = args.index_name
        report["engine_version"] = os.environ.get("BENCHMARK_ENGINE_VERSION", "unrecorded")
        report["host_notes"] = os.environ.get("BENCHMARK_HOST_NOTES", "unrecorded")
        report["engine_settings"] = backend.settings
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{args.engine}: correctness passed; {report['parameters']['documents']} documents; report {args.output}")


if __name__ == "__main__":
    main()
