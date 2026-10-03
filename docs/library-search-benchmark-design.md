# Library-search benchmark design

## Proposed contribution boundary

The `openedx-search` base currently provides a scaffold rather than usable production
search backends. This change supplies independently runnable benchmark tooling. The
real-engine mode depends on the flat string document adapters in
[openedx-search PR #3](https://github.com/openedx/openedx-search/pull/3). No edx-platform indexing hooks, ACL hooks or migrations are implied.

Fixture schema is `{id, library_id, kind, title}`, all strings. Seeded title choices
vary payloads while kind distribution and library scope membership remain predictable.
The fixture hash includes every serialized document; JSON captures parameters, Python
and platform metadata, correctness counts and raw timing samples. Reference timings
use a linear scan and are intentionally labelled as reference implementation only.

## Correctness contract

The independent oracle evaluates exact library membership with an optional conjunctive
kind restriction. It does not reuse backend matching code. Tests exercise `library_1`
beside `library_10` to detect prefix mistakes. The query suite checks full authorized
scope, kind+scope, empty scope, unknown scope, the 250-document maximum page size and
mutation convergence. Empty scope short-circuits in the live adapter wrapper.

Every returned document must belong to the oracle and match its complete payload.
Every page must meet its cap. Collection checks retrieve all expected pages plus one
additional page, reject duplicate IDs across pages, then compare the entire retrieved
set with the oracle. Exact totals must agree; estimated totals never substitute for
set completeness. Retrieval limits cause explicit failure, including a default 1,000
Meilisearch hit ceiling. No ordering equivalence between engines is assumed; duplicate
pages under tied relevance ranking fail rather than hiding unreliable pagination.

Mutation uses upserts to change one payload and move one document outside the trusted
scope; it submits the same mutation twice and verifies the new complete access set.
Meilisearch tasks are polled with a bounded deadline; Typesense completion is immediate
according to the adapter's receipt. This verifies index visibility after adapter completion,
not the end-to-end delay from Studio publication or ACL changes. Deletes and ACL-cache
invalidation need future contracts.

## Measurement contract

Ingest time includes all batch submissions and receipt polling. Search timing includes
one scoped HTTP call (or one reference scan); correctness checking occurs after the
timer. Warmup samples are discarded. p50/p95 use nearest-rank percentiles; raw samples
are retained. Mutation time includes writes, duplicate retry and pagination verification,
so it is explicitly named `mutation_and_verification_seconds` rather than pure latency.

The first pass is single-process, single-reader, empty-query exact filter evaluation.
It does not measure fuzzy relevance, indexing backlog, recovery, memory, CPU, query
concurrency, production-sized nested documents or large allowed-library filter budgets.
Increasing total library count with a fixed scope measures total-index growth; increasing
authorized-library count measures scope/filter growth. Those are separate experiment axes.
The parent also verified a 1,501-library allowed scope against 10,000 total libraries:
9,006 authorized documents paginated completely in the 60,000-document reference index.
That sample remains reference-only; it does not validate a real engine's large-filter
limits or engine resource use.

Real runs require dedicated engines and the installed adapter dependency.
An optional --adapter-source supports explicit development checkouts.
The optional positive `--meilisearch-max-total-hits` flag updates only the newly created
disposable index through the public HTTP transport, validates the integer task receipt
and waits for completion before ingest/search. It is recorded in live output, rejected
for other engines before creation, and never supplied silently. Default runs retain
retrieval-ceiling diagnostics; explicitly configured runs can traverse larger scopes.
Report provenance records adapter, contract and transport hashes plus user-supplied
engine version and host notes. Live version discovery, automatic index cleanup and
production settings contracts remain proposed next steps. A successful report is written only
after correctness succeeds; missing engines or correctness failures terminate the run.

## Reviewable PR sequence

1. Merge/adopt the adapter contract and standalone synthetic fixture/oracle tooling.
2. Add correctness CI with deliberate leak, stale-data and truncation regressions.
3. Add pinned local engine jobs, configurable retrieval limits and resource metadata.
4. Agree on real Studio fixture policy and hook into trusted platform authorization.
5. Add representative workload matrices and publish stable comparative results before
   selecting numerical performance acceptance thresholds.

Synthetic fixtures contain no learner/author information. Existing checkouts remain
untouched. All changes and generated reports are isolated in this track's worktree.

## Reproduce with the published dependency

Install the adapter revision from PR #3 into a fresh Python 3.12 environment.
Run this benchmark checkout without installing its competing package scaffold:

```sh
uv venv --python 3.12 .venv-benchmark
uv pip install --python .venv-benchmark/bin/python --no-deps \
  'git+https://github.com/shatrughnaupadhyay/openedx-search@ad973c6bea8c6dba195bff2961314878c068e09c'
.venv-benchmark/bin/python -m unittest discover -s tests/benchmarks -v
# Supply BENCHMARK_ENGINE_API_KEY privately for each disposable engine.
.venv-benchmark/bin/python -m benchmarks.library_search --engine meilisearch \
  --url http://127.0.0.1:7700 --meilisearch-max-total-hits 1200 \
  --libraries 10000 --documents-per-library 6 --authorized-libraries 200 \
  --page-size 250 --seed 42 --iterations 10 --warmup 2 --output meilisearch.json
.venv-benchmark/bin/python -m benchmarks.library_search --engine typesense \
  --url http://127.0.0.1:8108 --libraries 10000 --documents-per-library 6 \
  --authorized-libraries 200 --page-size 250 --seed 42 --iterations 10 \
  --warmup 2 --output typesense.json
```

Use Meilisearch 1.36.0 and Typesense 30.2 for the recorded validation. Set
BENCHMARK_ENGINE_VERSION and BENCHMARK_HOST_NOTES to record your environment.
Each live invocation creates a disposable index/collection; after saving the
report, delete only its recorded index_name with the engine administration API.
The harness checks explicit filters using an administrative key. It does not
establish platform authorization or signed-token enforcement. Latency varies
with the host; no performance threshold is claimed.

## Local live evidence

23 harness tests passed. Small 300-document and 60,000-document runs passed all
six correctness cases on each engine. The larger fixture had 10,000 libraries
and 200 allowed libraries (1,200 documents); both engines traversed all pages
with zero explicit-filter leaks and converged after repeated mutations.
Meilisearch maxTotalHits was explicitly 1,200. Typesense initially rejected
a GET query exceeding 4,000 bytes; the adapter dependency now uses POST
multi_search for large queries. These measurements are synthetic local runs,
not platform ACL validation or a production performance promise.
