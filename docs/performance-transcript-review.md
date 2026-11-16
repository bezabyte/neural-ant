# Neural Ant performance review

This review applies ideas from the AI Engineer transcripts on code sandboxes, orchestration, and latency budgets to Neural Ant's existing `/setup → /scrape → /rank → /apply` draft workflow.

## What the transcripts suggest

- Compose independent operations in code and return only the fields needed by the next step. This can reduce model turns, context, and cost; it does not guarantee lower wall-clock latency.
- Keep one orchestrator responsible for the workflow graph, bounded concurrency, state, errors, and timings. Parallelize only independent work.
- Measure time from the user's action to usable results, then split it into phases. Optimize the slowest measured phase instead of assuming the model is the bottleneck.
- If arbitrary model-written code runs in a sandbox, constrain tools and network access, protect secrets, cap CPU/memory/time, and evaluate repeated runs. Sandbox startup and operations add their own overhead.
- Use retries, circuit breakers, and cached fallbacks only where they reduce observed tail latency without hiding missing or stale results.

The code-mode transcript reports lower token use and cost in its own evaluation while latency stayed roughly constant. That is evidence for orchestration efficiency, not a speed guarantee.

## Changes made

- Kept the deterministic, allowlisted workflow plan. Board search and the broad index search already run concurrently; ranking waits for both, and document preparation remains bounded. The flow is predictable and does not execute model-generated code.
- Reused a bounded HTTP connection pool for employer boards, the public index, and opted-in AI calls.
- Replaced per-listing sponsor and existing-job lookups with one sponsor index and bounded URL batches in board search, broad-index search, and profile score refresh.
- Removed the post-preparation tracker reload. Opening Applications still loads fresh tracker data when the user asks to review it.
- Parallelized independent profile, tracker, and status requests during app initialization, and reused the profile response to populate the broad-index query.
- Added server fetch / persistence timings and recorded board, index, ranking, and preparation durations locally. Stored timing data contains counts and durations only.

## Before and after

| Area | Before | After |
|---|---|---|
| Search database reads | Each posting checked its URL and sponsor record separately. | Existing URLs and relevant sponsor records are loaded in bounded batches. |
| Outbound connections | Search created a new HTTP client for each board; the index and each AI request also used short-lived clients. | Board, index, and optional AI requests reuse a bounded keep-alive pool. |
| App initialization | Profile, index-query setup, tracker, jobs, and status requests waited in sequence. | Independent profile, tracker, and status requests run together; the loaded profile supplies the index query. |
| After draft prep | The browser fetched applications and jobs again before showing the ready result. | Those two reads wait until the user opens Applications. |
| Timing evidence | The UI showed total phase durations and a directional sequential estimate. | Search APIs expose fetch and local processing time; the browser stores phase durations and counts, still on-device. |

## More transcript lessons and best next moves

- **Measure the whole job before choosing a faster model.** “Is a Faster Model Actually Faster?” recommends comparing time, cost, and result quality on the same end-to-end task. The useful Neural Ant benchmark is click-to-reviewable files, using the same roles, source conditions, and fresh-draft state—not provider tokens-per-second alone.
- **Keep deterministic decisions in code.** “Your Agent Doesn't Need an LLM for Every Decision” argues that the model can provide judgments while ordinary code owns the actual decision. Neural Ant already uses code for fit gates, sponsorship filters, deduplication, and the allowlisted workflow; send only genuinely editorial work to an LLM.
- **Treat human review as an asynchronous checkpoint.** “The Human Is an Async API” shows durable workflows that pause on a human signal without holding a worker thread. A future draft run could persist its stage and progress, return immediately, and resume the review flow after documents finish or after a user answer. This improves responsiveness and recovery; it does not make model generation itself faster.
- **Evaluate model changes against factual quality.** The harness and eval talks recommend regression sets, traces, repeated runs, and measuring whether claims remain grounded. Before reducing resume context, changing provider/model, or removing a review pass, run those changes against representative job/resume pairs and compare factual errors, completeness, time, and cost.
- **Do not execute arbitrary generated scripts in the app process.** “Why, and How You Need to Sandbox AI-Generated Code?” details isolation, network egress, secrets, permissions, and resource limits. For this fixed workflow, the allowlisted plan is simpler and faster to audit. If an open-ended code mode is ever justified, use a separate restricted sandbox and an evaluation gate.

The transcripts mention Arize Phoenix/Harbor and MLflow for traces and evaluations, and Temporal for durable human-in-the-loop workflows. Neural Ant does not need all of those dependencies at once. A next step should first add a repeatable end-to-end benchmark and collect privacy-safe phase percentiles; adopt a tracing or durable-workflow service only if those measurements justify the operational cost.

## Benchmark and limits

A local synthetic run with 6 stubbed employer boards and 240 postings saved all listings in 347 ms on SQLite. The prior implementation made up to 480 listing-by-listing reads for those postings (one existing-URL lookup and one sponsor lookup per row); the new path made 2 matching reads total: one batched URL lookup and one batched sponsor lookup. External network latency and AI-provider latency were stubbed, so 347 ms is not a production end-to-end promise.

For real runs, compare the full click-to-review time and inspect board fetch, index fetch, local persistence/scoring, ranking, and preparation. Draft reuse, number of roles, network conditions, and optional external AI change the result. The browser's sequential estimate is directional, not a controlled benchmark.

## Next measurements before adding more machinery

- Collect privacy-safe P50/P95 phase timings across real runs, including source failure rates and provider wait time.
- If slow or failed sources dominate, consider shorter per-source deadlines and a circuit breaker that preserves successful partial results.
- If optional email or AI work dominates, report that phase separately and move only nonessential work to a background task or streaming status path.
- Increase AI concurrency only after checking provider rate limits and draft quality. Do not remove the independent review gate just to reduce model calls.
- Do not add a general-purpose code sandbox for this fixed workflow unless a measured need outweighs startup cost and the additional network, permission, secret, and resource controls it requires.
