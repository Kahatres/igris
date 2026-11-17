# Free models and accounts: execution plan

> Frozen (see [BACKLOG.md](../BACKLOG.md)). Kept for reference. Paths below predate later moves:
> `HANDOFF.md` was a private working note and is not part of the public repository (direction and
> next steps live in `BACKLOG.md`), and
> the web page is v2 (`igris/static/v2.html`, `v2.css`, `v2.js`); `index.html`, `app.js` and
> `app.css` no longer exist.

Each part below is a self-contained prompt. Give one part to one agent (or work through it yourself),
finish it, run `pytest`, commit, then move to the next. Parts are ordered by dependency.

## Rules that apply to every part

- Python 3.10+, standard library only at runtime. Tests use `pytest`, never call a real model, the
  network or a real CLI; inject fakes (an `opener` or `runner` callable).
- Code, comments, docstrings, CLI text and docs are English. `igris/static/` and `igris/roadmap.json`
  are Turkish.
- Every module ships with `tests/test_<module>.py`. Write the failing test first.
- A module imports only the modules listed as its dependencies in README.md's architecture table.
  Update that table when you add a module.
- Secrets never go into the repo. Read them with `get_secret` (keyring first, then `IGRIS_<NAME>`).
- Respect the existing paid-usage guard: nothing in this plan may start billing a paid account.
- Update `HANDOFF.md` and `igris/roadmap.json` at the end of each part.

---

## Part 0: Close the accounts/server plan

**Goal:** finish roadmap item `hesaplar-internet` as far as it can go without exposing anything to the
internet. Public exposure stays deferred until the owner says otherwise.

**Prompt:**
Read `HANDOFF.md`, `docs/self_hosting.md`, `igris/accounts.py`, `igris/web.py` and
`tests/test_web_accounts.py`. Produce a gap list between what the docs promise and what the tests
cover. Then:

1. Add missing tests for login lockout/rate limiting, session expiry, CSRF failure, logout invalidating
   the session, password change invalidating other sessions, and `IGRIS_PUBLIC_ORIGIN` Host/Origin
   rejection. Fix any bug the new tests expose, one commit per bug.
2. Make the first-run flow explicit: `python -m igris --accounts-init` (or the existing bootstrap
   command) prompts for the `admin` and regular-user passwords without echo, never accepts them as CLI
   arguments, and refuses weak passwords (minimum length 12).
3. Confirm regular users cannot reach host CLI or multiagent endpoints (owner-only). Add a test per
   endpoint that returns 403 for a non-owner session.
4. Finish `docs/self_hosting.md` with a numbered checklist: reverse proxy with HTTPS, `IGRIS_PUBLIC_ORIGIN`,
   backup of the SQLite files, restart recovery.
5. Set the roadmap plan to `in_progress` with the remaining step "owner opens it to the internet".

**Done when:** all tests pass and the checklist exists. Do not open any port or deploy anything.

---

## Part 1: Generic OpenAI-compatible backend

**Goal:** one backend class that talks to any provider exposing `/chat/completions`.

**Files:** `igris/llm_compat.py`, `tests/test_llm_compat.py`.

**Prompt:**
Read `igris/llm.py` (the `Backend` protocol, `Message`, `BackendError`, `RateLimited`),
`igris/llm_extra.py` (`_post_json`, `_retry_after`) and `igris/llm_openai.py`. Reuse `_post_json`;
do not duplicate HTTP code. Implement:

1. A frozen dataclass `Provider(name, base_url, key_name, default_model, trains_on_data: bool,
   extra_headers: dict)`.
2. `CompatBackend(provider, model=None, *, api_key=None, opener=None, timeout=60)` with
   `complete(system, messages) -> str`. Key comes from `get_secret(provider.key_name)`; a missing key
   raises `BackendError` with a message naming the variable `IGRIS_<KEY_NAME>`.
3. Error mapping: 429 -> `RateLimited` (honour `Retry-After`), 402/403 quota text -> `RateLimited`
   with a "quota" reason, 401 -> `BackendError` "invalid key", 5xx and timeouts -> `BackendError`.
   Never include the key in an error message.
4. A `DailyQuota(path, requests_per_day)` helper persisted in SQLite or JSON next to `Budget`'s file,
   resetting at UTC midnight, counting failed requests too (OpenRouter does). `CompatBackend` raises
   `RateLimited` locally once the quota is spent, without calling the network.
5. A `PROVIDERS` registry dict, initially empty except for a fake used in tests. Part 2 fills it.

**Tests (fake `opener` only):** success, each error mapping, key redaction, quota exhaustion,
quota reset across a fake clock, request body shape (system message first, roles preserved).

**Done when:** tests pass and `README.md`'s architecture table lists `llm_compat` with dependency `llm`.

---

## Part 2: Wire the providers, one at a time

**Goal:** register each provider from the research list and make it usable via `IGRIS_BACKENDS`.

**Files:** `igris/llm_compat.py` (registry), `igris/config.py`, `tests/test_llm_compat.py`,
`tests/test_config.py`.

**Prompt (repeat per provider, one commit each):**
For provider X, verify its current base URL, model ids, free-tier limits and data-use terms on the
provider's own documentation page before writing code. Put the verified values in the registry entry
with a comment giving the docs URL and the date checked. Then:

1. Add the `Provider` entry. Set `trains_on_data=True` when the free tier may use content for training
   (Gemini free tier, Mistral Experiment plan).
2. Teach `config._parse_backends` the new backend name and add `IGRIS_<X>_MODEL` if the user may
   override the model.
3. Add a config test and a request-shape test for X.
4. Tell the owner exactly which key to create, where (URL), and the command to store it:
   `python -c "import keyring; keyring.set_password('igris','<KEY_NAME>', input())"` style, or the
   `IGRIS_<KEY_NAME>` variable. Wait for the owner to confirm the key exists.
5. Only after that, run one manual live call (a script under `tools/`, not part of pytest) and paste
   the result into the commit message.

**Order and per-provider notes:**

1. **Gemini** (existing `GeminiBackend`): update the default model to a current Flash model; keep the
   native API. Mark `trains_on_data=True` for the free tier. A Google AI Pro subscription does not
   grant API quota; do not assume it does.
2. **OpenRouter** `:free` models: 20 req/min, 50 req/day (1,000 after a $10 lifetime top-up). Failed
   requests count. Refuse to send when the account balance is negative (HTTP 402). Model list rotates,
   so fetch `/models` once a day and keep only ids ending in `:free`.
3. **Groq**: limits are per model; read them from the response headers rather than hard-coding.
4. **Cerebras**: confirm whether the free tier is permanent or a trial before documenting it.
5. **Mistral**: needs phone verification and training opt-in; `trains_on_data=True`.
6. **NVIDIA NIM**, 7. **Cloudflare Workers AI** (daily Neuron budget, account id in the URL),
8. **Hugging Face**, 9. **SambaNova**, 10. **Cohere** (non-commercial trial key only).
11. Last, only if still available: Ollama Cloud, GitHub Models, trial-credit providers. Mark credits as
    expiring and never rely on them for the default order.

**Done when:** every registered provider has a config test and a request-shape test, and the owner has
confirmed a live call for each one that has a key.

---

## Part 3: Privacy gate and fallback chain

**Goal:** never send private content to a provider that trains on it, and fall through providers
cleanly when quotas run out.

**Files:** `igris/routing.py`, `igris/config.py`, `tests/test_routing.py`.

**Prompt:**
Read `igris/routing.py` and `igris/assistant.py`. Implement:

1. A request sensitivity flag. Default `private=True` for anything involving attachments, workspace
   files, memory or user-specific context; `private=False` only for plain general questions the user
   typed. A config switch `IGRIS_ALLOW_TRAINING_PROVIDERS` (default off) overrides it.
2. A router step that drops providers with `trains_on_data=True` when the request is private.
3. Fallback: on `RateLimited` or `BackendError`, try the next backend in the configured order. Record
   the attempt in a small in-memory health table; a provider that fails 3 times in 10 minutes is skipped
   for 10 minutes. Surface which backend actually answered in the result object.
4. Keep the existing paid-usage guard first in the chain: Claude/Codex subscription backends stay the
   preferred path for long work; free providers are the fast/cheap and fallback path.

**Tests:** private request skips a training provider; the override flag lets it through; fallback order;
health-table skip and recovery with a fake clock; the answering backend is reported.

---

## Part 4: Benchmark harness

**Goal:** measure each free model per domain with automatic scoring, store the results, produce ranks.

**Files:** `igris/modelbench.py`, `igris/bench_cases/` (JSON case files), `tests/test_modelbench.py`.

**Prompt:**
Design the harness so tests can run it entirely with fake backends. Implement:

1. Domains: `code`, `turkish_chat`, `summarization`, `tool_use`, `long_context`, `reasoning`.
   Each case file holds 10 to 20 cases: prompt, expected checker type and parameters.
2. Automatic checkers, no model-as-judge in the first version:
   - code: run the returned function against hidden unit tests in a subprocess with a timeout and no
     network;
   - tool_use: parse the returned JSON tool call and compare name and arguments;
   - summarization and turkish_chat: required-fact and forbidden-phrase checklists, plus a length
     window;
   - long_context: needle retrieval at several depths;
   - reasoning: exact numeric or short-string answers.
3. `run_benchmark(backend, domain, cases, *, clock, runner)` returns per-case score (0 to 1), latency,
   and error. A provider that raises `RateLimited` pauses and resumes instead of scoring zero.
4. Persist runs in SQLite (`model`, `provider`, `domain`, `case_id`, `score`, `latency`, `run_at`).
5. `rank(domain)` returns models ordered by mean score, with latency as the tie-break, and ignores
   results older than 30 days. Require a minimum of 8 scored cases before a model is ranked.
6. A CLI entry `python -m igris --bench [--domain D] [--provider P]` that is manual only and prints a
   table. It must refuse to run when the daily quota for the provider would be exhausted.

**Tests:** each checker with passing and failing inputs, rank ordering, minimum-case rule, expiry,
rate-limit pause and resume with a fake clock. Real benchmark runs are a manual step for the owner.

---

## Part 5: Use the ranks in routing

**Goal:** pick the best available free model per task domain.

**Files:** `igris/routing.py`, `igris/modelbench.py`, `tests/test_routing.py`.

**Prompt:**
Add a lightweight domain classifier (keyword and attachment rules first, no model call) that maps a
request to one of the benchmark domains. In the router, when the chosen path is "free", order candidate
backends by `rank(domain)`, then apply the privacy gate and the health table from Part 3. If no rank
data exists for a domain, fall back to the configured `IGRIS_BACKENDS` order. Log the decision (domain,
ranked list, chosen backend) so the UI can show it.

**Tests:** ranked order is respected, missing ranks fall back to config order, privacy gate wins over
rank, a quota-exhausted top model falls to the second.

---

## Part 6: Web UI "Modeller" panel

**Goal:** show ranks, remaining quota and data-use warnings. UI text is Turkish.

**Files:** `igris/web.py`, `igris/static/index.html`, `app.js`, `app.css`, `tests/test_web.py`,
`tests/test_static.py`.

**Prompt:**
Add an authenticated, read-only endpoint `GET /api/models` returning, per provider: name, models,
domain ranks, requests used today versus the daily limit, health status, and `trains_on_data`. Add a
"Modeller" tab: a rank table per domain, a quota bar per provider, and a visible warning badge
"Verileri eğitimde kullanabilir" for training providers. Only the owner may trigger a benchmark run
from the UI; regular users see the table read-only. Use the existing design tokens and layout
conventions in `app.css`. Add endpoint tests (auth required, owner-only action) and static-file tests.

---

## Part 7: Enable for regular users and close out

**Prompt:**
Add the free providers to the `IGRIS_USER_BACKENDS` documentation and defaults for the regular user
account, keeping the privacy gate on. Update `README.md`, `docs/self_hosting.md`, `HANDOFF.md` and
`igris/roadmap.json` (Turkish) with the new modules and the remaining manual steps (obtaining keys,
running the benchmark). Run the whole suite and report the number of passing and skipped tests.
