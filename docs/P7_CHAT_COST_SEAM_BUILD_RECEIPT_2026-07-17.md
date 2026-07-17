# P7 chat cost seam — build receipt

Status: **READY FOR ONE LOCKED LIVE VERIFICATION; NOT LIVE-VERIFIED**

Branch: `codex/p7-chat-cost-seam-20260717`

Base: `7af4f1637f5b959c051ee7be2bbb41ac75c5006b`

## Contract implemented

- `CHAT_COST_TELEMETRY_ENABLED` ships default `False`.
- Flag OFF is a byte-exact SSE passthrough and does not add
  `stream_options` to provider requests.
- Flag ON creates one request-scoped ledger for the `/api/chat` route.
  Streaming, non-streaming helper, fallback, and transport-retry exposure is
  accounted at the shared `LLMService` boundary.
- One terminal SSE trace titled `Chat synthesis cost ledger` contains every
  synthesis-call row, input/output tokens, exact price inputs, formula,
  computed cost, price-registry SHA-256, and closure state.
- Unknown prices, missing usage, failed helper calls, and prior unmetered
  transport attempts make the ledger `OPEN`; the seam never estimates missing
  usage.
- `run_heldout_eval.py` only collects the additive cost trace and aggregates
  it. `git diff -U0` proves no frozen scoring line changed.
- The standalone aggregator accepts eval JSON or raw SSE and exits nonzero
  unless the run ledger is `CLOSED`.

The versioned price registry SHA-256 is
`9644c084f356db14a0f437ef280f2797cbe362fce264f48f5e4ca767e5f63b6d`.
MiniMax M2.7 uses the already receipted OpenCode card:
`$0.30 / 1M` uncached input tokens and `$1.20 / 1M` output tokens.

## Build and test receipts

All commands were local build/test operations. No live backend deploy, corpus
write, or provider call occurred.

| Gate | Result |
|---|---|
| Backend image build, `docker build -t polymath-p7-chat-cost:test backend` | `EXIT=0`; immutable image `sha256:8b8f4168699a5cf44e095031fa31b5bb450e407e6cc847e7af5b8251267bd505` |
| P7 focused suite | `13 passed`, `EXIT=0` |
| Adjacent LLM/stream/trace/fallback suites | `56 passed`, `EXIT=0` |
| Scoped Black check | 5 files unchanged, `EXIT=0` |
| Python compile | `EXIT=0` |
| `git diff --check` | `EXIT=0` |

Focused coverage asserts:

1. trace-only arithmetic reproduction;
2. secret/query-string exclusion;
3. unknown-price and missing-usage fail-open behavior;
4. retry exposure accounting;
5. multi-request eval aggregation;
6. terminal trace ordering before `done`;
7. closed zero-cost model-skipped requests;
8. byte-exact flag-OFF passthrough and request body;
9. OpenAI stream-usage capture;
10. non-stream helper success and failure accounting; and
11. default-OFF configuration.

## Pending serialized gate

The single small live verification has not run. At build completion,
`/tmp/polymath-eval.lock` was held by `claude-continuation-20260713` for the
STEP0 promotion evaluation. Per directive, P7 will not deploy or call the
provider until the orchestrator signals release and the lock is explicitly
free. The live gate must publish a `CLOSED` run ledger with zero unmetered
synthesis calls and independently recompute its total from the trace.
