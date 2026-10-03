# Initial backend compatibility design

## Baseline and sources

Local baseline `f56cef2599f845da9214c4cf7d4243a3fbf4684e` is a generated Django
package skeleton plus `docs/decisions/0001-purpose-of-this-repo.rst`. That ADR
targets Meilisearch and Typesense, caller-built filter terms, a browser-direct
query path, worker-based writes, versioned lifecycle management and real-engine
tests. Its file status is Draft/Provisional; a merged proposal is not evidence
of implemented compatibility.

Protocol references checked during this contribution:

- [Typesense search](https://typesense.org/docs/30.0/api/search.html): exact `:=`
  filters, backtick literals, `query_by`, one-based page/per_page and hit envelopes.
- [Meilisearch search](https://www.meilisearch.com/docs/reference/api/search/search-with-post):
  POST JSON, offset/limit pagination and estimated totals.
- Local ADR decisions 5, 7, 9 and 10 are the architectural authority.

## Contract and mechanics

`IndexDefinition` declares a flat string schema. `FilterTerm` is an OR of exact
string values in one field; terms are ANDed. Field names are syntax-checked and
must be declared filterable. No raw filter-expression API exists. Unsupported
backticks/backslashes/control characters and empty sets fail before network I/O
on both engines. This avoids silently broadening authorization constraints.
It is deliberately restrictive until real-engine escaping cases are verified.

`SearchQuery` has one-based pages and a shared 1..250 page-size bound. Meilisearch
translates to offset/limit; Typesense uses page/per_page. Search relevance,
tokenization and deep result-window ceilings remain engine-specific. Meilisearch
estimated counts are explicitly marked inexact. Typesense cutoffs raise a retryable
failure rather than masquerade as complete results.

Document IDs use the common ASCII alphanumeric/underscore/hyphen subset.
Upserts accept 1..1000 flat string documents. Consumers must slug existing opaque
keys, and batch by byte size externally as well: count bounds alone do not bound
the serialized payload. No automatic retry occurs in an adapter.

Meilisearch `create_index`, `configure_index`, `upsert` return pending task receipts;
`check_write` checks exactly once. Creation must complete before configuration,
and configuration must complete before search. Typesense creates fields with
`facet=true` for filterable fields, imports NDJSON with `action=upsert`, and returns
complete receipts. HTTP-200 import failures still raise because a batch can be
partially applied. Retrying idempotent replacements is the worker's responsibility.

Typesense synchronous engine calls are worker primitives, not a request-path
submission API. A task queue must wrap both engines to satisfy ADR decision 9.
Search methods exist for maintenance/integration verification; production browser
queries should use the vendor InstantSearch adapters, as the ADR requires.

## Deliberate gaps

This slice does not implement nested/array fields, hierarchical facets, sorting,
highlighting, distinct/grouped results, aliases, index reconciliation, scoped
credentials, queue orchestration or Django/MFE integration. It makes no full
Studio parity claim. Settings are fixed at Typesense collection creation; its
`configure_index()` returns a completed receipt and is not schema reconciliation.

The focused suite passed 18 tests, including live Meilisearch 1.36.0 and Typesense
30.2. Live cases verify exact filters, pagination across 251 documents, full
replacement with field removal, task completion, and HTTP-success import errors.
Each case creates and removes a disposable index/collection. Real-engine CI
remains a requirement before upstream acceptance. Whole-repository Django tests
were not run; this work validates the new dependency-free slice only.
