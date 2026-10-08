# agent-runtime-gcp

A small, durable agent runtime: an async FastAPI service that runs a real
[LangGraph](https://github.com/langchain-ai/langgraph) graph with a Postgres
checkpointer, so runs survive process crashes and can pause for human approval.
It adds a long-term memory store, provider failover, OpenTelemetry metrics and
traces (cost per run, failures, stuck runs), a Terraform module for Cloud Run,
and a GitHub Actions release pipeline with rollback.

> **Learning project.** I built this to learn durable agent execution and GCP
> deployment end to end. It is not used in production anywhere, and the LLM in
> the default setup is a deterministic mock. See
> [What is real vs stubbed](#what-is-real-vs-stubbed).

## What it does

```
POST /runs ──► LangGraph StateGraph (checkpointed after every node)
                 recall_memory → plan → propose_action ─┬─(no risky action)──────────────► save_memory → finalize
                       ▲            │                   └─► human_approval ── approve ──► execute_action ─┘
                       │            │                         (interrupt)  └─ reject ───► save_memory
         long-term memory store     └─► FailoverRouter: provider A → B (circuit breaker), cost per call
                                                                     │
  Postgres: checkpoints + memory + run registry                     OpenTelemetry metrics + spans → OTLP / console
```

| Capability | How |
|---|---|
| Resumable runs | `AsyncPostgresSaver` (or SQLite / in-memory). Every node's output is checkpointed with `durability="sync"` (committed before the next node starts; LangGraph's default `async` mode lost the last step on a hard kill in testing), so a run restarted after a crash continues from the last finished node. On startup the service resumes runs left `running` by a dead process. |
| Human approval | Risky actions (`deploy`, `delete`, `refund`, `pay`, `email`, `send`) hit a node that calls `langgraph.types.interrupt()`. `POST /runs/{id}/decision` resumes it with `Command(resume=...)`, even from a different process. |
| Side-effect safety on resume | `execute_action` writes an idempotency record before its checkpoint, so a crash between the side effect and the checkpoint doesn't repeat it. |
| Long-term memory | LangGraph `BaseStore` (`AsyncPostgresStore` / `AsyncSqliteStore` / `InMemoryStore`), namespaced per user. "remember that ..." facts are stored and recalled in later runs. |
| Provider failover | Ordered providers with retryable vs fatal errors (429, 5xx and timeouts fail over; 4xx doesn't) and a per-provider circuit breaker. Real OpenAI-compatible provider via `OPENAI_BASE_URL`; mocks otherwise. |
| Observability | OpenTelemetry SDK: counters and histograms for runs, cost, duration, tokens, provider failures and failovers, plus gauges for stuck runs and runs waiting for approval. One span per node and per run segment. Console or OTLP/HTTP exporter. |
| Deploy | `terraform/`: Cloud Run v2 service, Artifact Registry, service account, Secret Manager secrets, optional Cloud SQL Postgres. |
| Release | `.github/workflows/release.yml`: build → local smoke test → push → deploy with no traffic → smoke test → shift traffic, with auto-rollback on failure and a manual rollback action. |

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ,postgres for the Postgres saver/store
pytest -q                        # 41 pass, 1 skipped (Postgres) without a database
python demo/run_demo.py          # offline end-to-end demo (output below)
```

Run the API with Postgres:

```bash
docker compose up --build        # Postgres 16 + API on :8080
curl -s localhost:8080/runs -H 'content-type: application/json' \
  -d '{"user_id":"me","task":"Deploy build 7 to staging"}'            # -> status: waiting_approval
curl -s localhost:8080/runs/<run_id>/decision -H 'content-type: application/json' \
  -d '{"approved":true,"reviewer":"me"}'                              # -> status: succeeded
```

Without Docker: `pip install -e ".[postgres]"`, then
`AGENTRT_PERSISTENCE=postgres AGENTRT_PG_DSN=postgresql://... agent-runtime serve`.

Optional dashboards: `docker compose --profile observability up` adds an
OpenTelemetry Collector, Prometheus and Grafana (http://localhost:3000, with the
"Agent runtime" dashboard provisioned). Set `AGENTRT_OTEL=otlp` for the API.

### API

| Method | Path | |
|---|---|---|
| `POST` | `/runs` | `{user_id, task, run_id?}`. Runs until it finishes or pauses for approval. |
| `GET` | `/runs/{id}` | Status, steps, pending approval payload, result, cost. |
| `GET` | `/runs?status=` | List runs from the registry. |
| `POST` | `/runs/{id}/decision` | `{approved, reviewer, comment}`. Needs `X-Approver-Token` if `AGENTRT_APPROVER_TOKEN` is set. |
| `POST` | `/runs/{id}/resume` | Resume a `failed` (or orphaned `running`) run from its last checkpoint. |
| `GET` | `/users/{id}/memories` | Long-term memories for a user. |
| `GET` | `/metrics/summary` | Stuck and waiting-for-approval run IDs (also exported as OTel gauges). |
| `GET` | `/healthz` | Liveness, used by Cloud Run probes. |

### Configuration

| Env var | Default | |
|---|---|---|
| `AGENTRT_PERSISTENCE` | `memory` | `memory`, `sqlite` or `postgres` |
| `AGENTRT_PG_DSN` / `AGENTRT_SQLITE_PATH` | | Connection for the chosen backend |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` | unset | Puts a real OpenAI-compatible provider first in the failover chain |
| `AGENTRT_MOCK_PROVIDERS` | `mock-primary,mock-backup` | Mock providers in failover order |
| `AGENTRT_FAIL_PROVIDERS` | | Fault injection, e.g. `mock-primary=rate_limit` (`error`, `timeout`, `fatal`, `flaky:N`) |
| `AGENTRT_STUCK_AFTER_S` | `120` | Heartbeat age after which a `running` run counts as stuck/orphaned |
| `AGENTRT_OTEL` | `off` | `console` or `otlp` (uses the standard `OTEL_EXPORTER_OTLP_ENDPOINT`) |
| `AGENTRT_APPROVER_TOKEN` | unset | Shared secret for the decision endpoint |

## Demo run

`python demo/run_demo.py`: 429 failover, approval pause and approve, memory
recall, then a **separate process killed with `os._exit(137)` mid-run** and
recovered by a new process, followed by the OpenTelemetry metrics it recorded.

```text
== 1. primary provider returns 429 -> failover to backup
   status=succeeded provider=mock-backup cost_usd=0.000354 attempts=1
   steps=['recall_memory', 'plan', 'propose_action', 'save_memory', 'finalize']
   result=[Summarise yesterday's error logs. Remember that I prefer bullet points.] Plan: 1) gather context 2) draft res

== 2. risky action pauses at the human-approval interrupt
   status=waiting_approval provider=mock-backup cost_usd=0.000273 attempts=1
   steps=['recall_memory', 'plan', 'propose_action']
   pending_approval={'type': 'deploy', 'description': 'Deploy revision 42 to staging'}
   memories_used=['I prefer bullet points']

== 3. approve via the same API the HTTP endpoint uses
   status=succeeded provider=mock-backup cost_usd=0.000273 attempts=1
   steps=['recall_memory', 'plan', 'propose_action', 'human_approval', 'execute_action', 'save_memory', 'finalize']
   memories_used=['I prefer bullet points']
   result=[Deploy revision 42 to staging] Plan: 1) gather context 2) draft result 3) verify. Action: simulated deploy ex

== 4. long-term memory recalled in a later run
   status=succeeded provider=mock-backup cost_usd=0.000255 attempts=1
   steps=['recall_memory', 'plan', 'propose_action', 'save_memory', 'finalize']
   memories_used=['I prefer bullet points']
   result=[Draft the weekly update] Plan: 1) gather context 2) draft result 3) verify.

== 5. hard crash: a separate process is killed (os._exit 137) inside `propose_action`
   child exit code: 137
   new process recovered: ['run-4']

==    run-4 after recovery (recall_memory/plan were not re-run)
   status=succeeded provider=mock-primary cost_usd=0.000028 attempts=2
   steps=['recall_memory', 'plan', 'propose_action', 'save_memory', 'finalize']
   memories_used=['I prefer bullet points']
   result=[Summarise the incident] Plan: 1) gather context 2) draft result 3) verify.

== 6. OpenTelemetry metrics recorded in this process
   agent_runs_started{} 3
   agent_provider_failures{provider=mock-primary,reason=http_429} 3
   agent_provider_failovers{primary=mock-primary,served_by=mock-backup} 3
   agent_llm_tokens{kind=prompt,model=mock-large,provider=mock-backup} 64
   agent_llm_tokens{kind=completion,model=mock-large,provider=mock-backup} 46
   agent_runs_finished{provider=mock-backup,status=succeeded} 3
   agent_runs_finished{provider=mock-primary,status=succeeded} 1
   agent_run_cost_usd{provider=mock-backup,status=succeeded} count=3 sum=0.000882
   agent_run_cost_usd{provider=mock-primary,status=succeeded} count=1 sum=0.000028
   agent_run_duration_s{provider=mock-backup,status=succeeded} count=3 sum=0.087334
   agent_run_duration_s{provider=mock-primary,status=succeeded} count=1 sum=0.041623
   agent_runs_resumed{} 1
   agent_runs_stuck{} 0
   agent_runs_waiting_approval{} 0
```

In step 5, `steps` lists `recall_memory` and `plan` once, because the first
process ran them before it was killed and the new process continued from the
checkpoint.

## Tests

`pytest -q` gives **41 passed, 1 skipped** offline in about 2 seconds. With
`AGENTRT_PG_DSN` set, **42 passed**, which the `postgres` CI job runs against a
Postgres 16 service container. Coverage is about 91%; the CLI is exercised
through a subprocess test, so it isn't counted.

- `test_providers.py`: mock fault modes, failover order, fatal vs retryable, circuit breaker open/half-open, OpenAI-compatible client against `httpx.MockTransport` (usage parsing, 429/5xx/4xx mapping, timeouts).
- `test_runtime.py`: graph paths, approval/reject, invalid transitions, memory across runs and users, failover inside the graph, all providers down → `failed` → resume from `plan`, orphan recovery by a second manager, stuck-run gauge, no duplicate side effect on resume, SQLite durability across processes, and Postgres durability across connection pools.
- `test_crash_subprocess.py`: the CLI is hard-killed inside a node and a new interpreter resumes the run from SQLite.
- `test_api.py`: the HTTP surface through FastAPI's `TestClient`, including 404/409/401.

CI (`.github/workflows/ci.yml`) runs the tests on Python 3.11 and 3.12, the
demo, the Postgres job, and `terraform fmt -check` plus `terraform validate`.
It never calls GCP.

## Deploying to Cloud Run (Terraform)

```bash
cd terraform
terraform init
terraform apply -var-file=my.tfvars     # see example.tfvars
gcloud secrets versions add agent-runtime-pg-dsn --data-file=- <<< 'postgresql://USER:PASS@/agentrt?host=/cloudsql/<connection_name>'
```

Terraform owns the infrastructure. After the first apply, the release workflow
owns the image and traffic split, so Terraform ignores those fields. Secrets are
added out of band so they never land in state.

## Release pipeline

`release.yml` runs on `v*.*.*` tags or manually (`action: deploy | rollback`):

1. **build**: `docker build`, then run the image and do a real `POST /runs` smoke test against it.
2. **push**: to Artifact Registry over Workload Identity Federation.
3. **deploy**: in the `production` environment, so add required reviewers to make it a manual gate. It deploys with `--no-traffic --tag candidate`, smoke-tests the candidate URL and moves 100% of traffic. If any step fails, traffic goes back to the previously recorded revision.
4. **rollback**: manual. It moves traffic to `rollback_to`, or to the newest revision that isn't currently serving.

Without `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOY_SA` and `GCP_PROJECT_ID`,
every GCP step is skipped with a notice, so forks and CI stay green.

## What is real vs stubbed

**Real (runs and is tested here):**
- LangGraph `StateGraph`, `interrupt()` / `Command(resume=...)`, and the official `langgraph-checkpoint-postgres` saver and store. Tested against a real Postgres (16 in CI, 17 locally), including resuming an approval after a server restart.
- Crash recovery with a real killed process and SQLite checkpoints.
- The OpenTelemetry SDK instruments and spans, checked with in-memory readers in tests and visible in the console exporter.
- `terraform fmt` and `terraform validate` pass against `hashicorp/google ~> 6.0`.
- `actionlint` passes on both workflows.

**Stubbed or not verified:**
- **The LLM is a deterministic mock by default.** Its "plan" is a template string and there is no real reasoning or tool use. The OpenAI-compatible provider is unit-tested against a mocked HTTP transport, not a live API.
- **Actions are simulated.** `execute_action` returns `"simulated deploy executed"`, and nothing is actually deployed, deleted or paid.
- **Memory is naive.** It stores "remember that ..." phrases by regex, with no embeddings or semantic search.
- **Costs use an illustrative price table** and whitespace token counts for mocks. They are not real provider prices.
- **Terraform has never been applied.** It validates, but I haven't run `plan` or `apply` against a GCP project.
- **The release pipeline has never run against GCP.** The GCP steps are skipped without secrets, and the candidate smoke test's identity-token call may need an `--audiences` flag depending on how WIF is set up.
- **Docker Compose, the Collector, Prometheus and Grafana are untested.** The machine this was built on has no Docker. The dashboard's PromQL assumes the Collector's Prometheus naming (`_total` and unit suffixes) and may need small fixes.
- **Approval auth is a single shared token.** There are no users, roles or OIDC.
- **Multi-replica takeover isn't safe.** Orphan recovery uses a heartbeat threshold, not a lease or lock, so two replicas could both resume the same orphan at the same moment.

## Layout

```
src/agent_runtime/  graph.py (LangGraph) · service.py (RunManager) · persistence.py · runs.py (registry)
                    providers.py (failover) · telemetry.py (OTel) · api.py (FastAPI) · cli.py · config.py
tests/              41 offline tests + 1 Postgres test
demo/run_demo.py    end-to-end demo
terraform/          Cloud Run, Artifact Registry, Secret Manager, optional Cloud SQL
deploy/             OTel Collector, Prometheus, Grafana provisioning + dashboard
.github/workflows/  ci.yml, release.yml
```

## License

MIT
