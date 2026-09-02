# VOX — Change Request / Roadmap

State of the repository on **2026-09-02**, commit `95e087d`, branch `main`, working tree
clean.

Measured, not estimated:

- 21,261 lines across sources and tests; `vox_chat/app.py` is the largest file (2,636 lines)
- suite: **514 passed, 8 skipped, 9m 15s** (`pytest -q`), **76%** coverage
- installed runtime dependencies: `openai 2.41.1`, `textual 8.2.8`, `rich 15.0.0`,
  `cryptography 50.0.1`
- CI on three systems and three interpreters; ruff, mypy and pre-commit configured

Status legend: `[to do]` still to be done · `[doing]` in progress (dirty working tree
or partially covered) · `[done]` already in the repo and verifiable ·
`[to complete]` everything that can be done from here is done, and what is left needs
a decision or an account only the owner has. Each of those carries a note saying
exactly what is missing and where to pick it up.

---

## 1. Toolchain, static quality and CI — `[to complete]`

Everything in this section is in the repository and verifiable. The one
thing left is branch protection, which is a GitHub setting and not a file;
see 1.2.

* **1.1 Static analysis** — `[done]`
  * `[done]` Add **Ruff** (lint + format), configured in `pyproject.toml`; rule set
    `E,F,I,UP,B,SIM,RUF`, `line-length = 88`. `ruff check` and `ruff format --check`
    are clean over 79 files. Three rules are ignored, each with its reason written
    in `pyproject.toml`: `E501` (the formatter owns the width; what is left are string
    literals it cannot split), `RUF012` (`BINDINGS` is Textual's API, not an accident)
    and `RUF001` (the interface text uses real typography on purpose). Adopting it
    turned up two real defects, fixed here: a `threading.Timer` in
    `discovery/agent.py` closing over a loop variable and requeueing the wrong peer
    five seconds later, and `PeerAnswer` unresolvable as an annotation in `mesh.py`.
  * `[done]` Add **mypy** in `strict` mode over the pure modules (`code_blocks.py`,
    `inspection.py`, `models.py`, `reasoning.py`, `usage.py`) with
    `ignore_missing_imports` — all five are already clean. The rest of the tree
    reports about a hundred findings (mostly `no-untyped-def` and `type-arg` around
    the UI, the mesh and the provider); `files` in `pyproject.toml` is the list that
    widens, one module at a time, as each is cleared.
  * `[done]` Add the `vox_chat/py.typed` marker, declared in `package-data`; the CI
    build job asserts it is present inside the wheel.
  * `[done]` `pre-commit` with ruff, ruff-format, `check-merge-conflict`,
    `mixed-line-ending --fix=lf`, end-of-file and trailing-whitespace hooks
  * `[done]` Pervasive type annotations and `from __future__ import annotations` everywhere
  * `[done]` Module docstrings that explain the *why*, not only the *what*

* **1.2 Continuous integration** — `[to complete]` (only branch protection)
  * `[done]` `.github/workflows/ci.yml`: matrix
    `ubuntu-latest × macos-latest × windows-latest` over `3.11`, `3.12`, `3.13`
  * `[done]` Separate parallel jobs: `lint`, `types`, `test`, `build`, `installers`
  * `[done]` `pip` cache per Python version, keyed on `pyproject.toml`
  * `[to complete]` Required status checks on pull requests into `main` (branch
    protection). **Nothing in the repository can set this**: it lives in the GitHub
    settings and needs admin rights on `atagliente/vox`. To finish it: Settings →
    Branches → add a rule for `main`, require the `lint`, `types`, `test` and `build`
    checks, and require a pull request. Note that turning it on also stops the direct
    pushes to `main` this roadmap has been making, so it is worth doing last.
  * `[done]` `dependabot.yml` for `pip` and `github-actions`, weekly
  * `[done]` A job that runs `install.sh --yes` and `install.ps1 -Yes` on clean
    runners, twice each to hold them to the idempotence they claim, then uninstalls

* **1.3 Coverage** — `[done]`
  * `[done]` `pytest-cov` with the threshold set to the measured level: **76%** over
    7,376 statements and 2,222 branches, on 2026-09-02 with 514 tests. `fail_under`
    lives in `pyproject.toml` and only ever moves up. The floors worth attacking
    first, from the same run: `discovery/run_agent.py` 0%, `discovery/agent.py` 16%,
    `discovery/whois.py` 22%, `discovery/transport.py` 27%, `discovery/registry.py`
    35% — all of them mesh code the suite skips unless `VOX_TEST_MESH=1`.
  * `[done]` Coverage report posted as a pull-request comment
  * `[done]` Exclude the purely defensive branches that are already marked as such

---

## 2. Tests: cost and reliability — `[done]`

* **2.1 Run time (8m 24s is the most concrete problem in the repo)** — `[done]`
  * `[done]` `pytest-xdist` with `-n auto` in `addopts`, so it is the default and not
    something to remember. **9m 15s → 1m 43s**, measured, a 5.4× cut. Two shutdown
    tests had to change: they assumed the process held no non-daemon threads besides
    the main one, which the xdist worker's own channel threads break. `_stuck_threads`
    is right and unchanged; the tests now narrow the census to the threads they made,
    and the real census is still asserted directly.
  * `[done]` `slow` marker on what actually waits — real sockets, timers, handshakes —
    rather than on what is merely slow to tear a Textual app down. `pytest -m "not slow"`
    for the development loop; CI runs everything.
  * `[done]` The fixed `time.sleep` calls: the 7, 8 and 9 second waits are all in
    `tests/test_discovery_vendor.py`, which is **skipped unless `VOX_TEST_MESH=1`**, so
    they never cost the ordinary run anything — the roughly 30s attributed to them here
    was not being paid. The one that did cost was
    `test_the_grace_period_lets_a_finishing_worker_end_cleanly`, waiting its full five
    second grace because the xdist threads never cleared; that is gone with the fix above.
  * `[done]` `pytest-timeout` at 120s per test, `timeout_method = "thread"` because
    Windows has no SIGALRM

* **2.2 Test-environment robustness** — `[done]`
  * `[done]` `%TEMP%\pytest-of-<user>` not writable now fails once, at configure time,
    naming the directory, the reason and the two ways out — instead of one identical
    `PermissionError` per test burying the result. The probe is a real `mkdir`, not
    `os.access`: on Windows `os.access` reports only the read-only attribute and calls
    a directory writable that refuses every write in it, which is exactly the case
    seen on this machine.
  * `[done]` `noxfile.py` reproducing lint, types and the interpreter matrix locally
  * `[done]` `conftest.py` redirects `~/.vox` to a temporary home for every test
  * `[done]` No test requires a running inference server

* **2.3 Missing classes of test** — `[done]`
  * `[done]` Property-based tests over `ThinkSplitter` in
    `tests/test_reasoning_properties.py`: where the chunks fall does not change the
    split, a token at a time matches all at once, nothing is dropped or invented, the
    awaited tag never reaches the transcript. The splitter held. What the properties
    did settle is the unbalanced case — a bare `</think>`, a second `<think>` inside a
    thought — which is text by design and is now pinned by a named test rather than
    left to be discovered. Some providers do emit a bare leading `</think>`; treating
    it as the end of an unannounced thought would be a different contract, and that
    is a decision, not a bug fix. See `[to complete]` below.
  * `[done]` Fuzzing `web.py` and `searchd.py` in `tests/test_parser_fuzz.py`. **It
    found a real one**: broken markup ahead of a `<script>` swallowed its opening tag,
    so nothing started skipping while the closing tag still arrived, and the entire
    script body was handed to the model as prose. Four different malformed shapes
    reach that state — a hanging `<!--`, a stray `</`, a mangled end-tag name, an
    unquoted attribute — so chasing it in the parser callbacks did not converge. The
    code elements are now cut out of the source before the parser sees them, which
    closes all four at once.
  * `[done]` Streaming performance floors in `tests/test_streaming_speed.py`: 20k
    tokens/s through the assembly path against a fake provider, with and without
    reasoning tags in the stream, plus a linearity check on the splitter fed one
    character at a time. The floors sit far below what this machine does, to catch an
    order of magnitude rather than measure the hardware.
  * `[to complete]` A bare leading `</think>`, with no `<think>` before it, is
    currently answer text. Several providers emit exactly that when the model was
    already mid-thought. Treating it as the close of an unannounced thought would put
    that text in the reasoning pane instead. Both readings are defensible and the
    choice is yours; `test_an_unmatched_tag_is_ordinary_text` in
    `tests/test_reasoning_properties.py` is where the current one is written down.
  * `[done]` Context-window and prompt-fitting coverage (`tests/test_model_window.py`)

---

## 3. Code architecture

* **3.1 `app.py` is a god object (2,636 lines)** — `[done]`, now **1,925**
  * `[done]` The `cmd_*` registry is a `vox_chat/commands/` package: `spec` holds the
    vocabulary and no application state, `handlers` holds the thirty-eight functions,
    `dispatch` is the table joining them — built from the command table itself, so a
    command declared with nothing to run it fails at import rather than when someone
    types it.
  * `[done]` `GenerationController` in `generation.py`. The `@work` decorator stays on
    `VoxApp.generate`, two lines calling into it: putting something on a thread is
    Textual's business and belongs to the widget.
  * `[done]` `ConsensusController` in `consensus_flow.py`, same arrangement. Those three
    methods really were the longest in the file.
  * `[done]` `commands/ui.py` defines `CommandUI`, the narrow view a handler is allowed
    to use. Honest limit: several handlers still take the application whole, because
    opening a modal or starting a worker *is* asking the Textual loop for something and
    a Protocol that pretended otherwise would be decoration. What it buys is that the
    boundary is written down, and the handlers inside it need no mounted app.
  * `[done]` No method exceeds ~61 lines: the problem is the count, not the length
  * `[done]` The UI thread never performs blocking HTTP (`call_from_thread`)

* **3.2 Module boundaries** — `[done]`
  * `[done]` One HTTP egress point: `vox_chat/http.py`. There is now exactly **one**
    `urlopen` in the whole project, one `HttpError` with a typed `kind`, and one place
    that decides about timeouts, byte caps and decoding. `web`, `searchd` and `ollama`
    still raise their own errors — a bare transport failure tells an operator nothing —
    but they translate one shape instead of each catching their own set.
  * `[done]` A single retry/backoff policy, `http.Retry`, with `OPEN_WEB` for the search
    backends. It deliberately does **not** retry a refused connection: a fixed endpoint
    that is down will not be up half a second later, there is a second backend to fall
    through to, and repeating it only makes the operator wait longer to hear the same
    thing. A test caught that distinction rather than a review.
  * `[done]` All eight remaining `except Exception` handlers now carry a one-line reason.
    Each is a boundary where a wide catch is the right answer — a per-peer round, a
    per-backend search, a server reply, a cancelled stream — and says so.
  * `[done]` `llm_client.py` is already the only place an OpenAI client is constructed
  * `[done]` `LLMError` with a typed `kind`
    (`connection|timeout|http|cancelled|protocol|context`)

* **3.3 Concurrency model**
  * `[to do]` Evaluate `AsyncOpenAI` in place of the synchronous client on a worker
    thread: Textual is already an asyncio application, and threads force
    `call_from_thread` on every update
  * `[to do]` If threads stay, centralise their creation: `threading.Thread` is
    instantiated in four independent places today (`discovery/agent.py`,
    `discovery/whois.py`, `searchd.py`, plus the Textual workers)
  * `[done]` Clean shutdown with detection of stuck threads (`__main__.py`)

---

## 4. LLM capabilities — alignment with the state of the art

* **4.1 MCP (Model Context Protocol) — the largest gap**
  * `[to do]` MCP client: connect to external MCP servers over stdio and streamable HTTP
  * `[to do]` Map discovered MCP tools onto the tool schema `agent.py` already uses,
    reusing the same explicit confirmation before any operation that writes
  * `[to do]` `/mcp add|list|remove` and an `mcp` section in the configuration
  * `[to do]` Expose VOX *as* an MCP server — the workspace tools are already sandboxed
    and tested, which makes them the natural candidate

* **4.2 Modern generation parameters**
  * `[to do]` Expose `reasoning_effort` / `think` for reasoning models (Ollama exposes
    `think` on `/api/chat`; VOX today only knows how to *read* reasoning, not request it)
  * `[to do]` Expose `top_p`, `top_k`, `seed`, `repeat_penalty` and `stop` in the
    configuration and in `/settings` — only `temperature` and `max_tokens` are passed today
  * `[to do]` `response_format` / structured output with a JSON Schema, plus a
    `/format <schema>` command to enforce it
  * `[to do]` Per-model presets: different parameters for different models, stored beside
    the model rather than globally
  * `[done]` `stream_options.include_usage` with automatic degradation on providers that
    reject it
  * `[done]` Reasoning read from `reasoning_content`/`reasoning`/`thinking` and from
    inline `<think>` tags, including tags split across chunks

* **4.3 Multimodality**
  * `[to do]` Image input: `/image <path>` attaching a base64 `image_url` content part —
    useful with local vision models (qwen2.5-vl, llava, gemma3)
  * `[to do]` Detect the model's vision capability automatically, with a clear message
    when it is absent
  * `[to do]` Inline image preview where the terminal protocol allows it (Kitty, iTerm2),
    with a textual fallback elsewhere

* **4.4 Context and memory**
  * `[to do]` Automatic conversation compaction when the window fills (summarise old
    turns instead of truncating), with a configurable threshold
  * `[to do]` Index the workspace with local embeddings (Ollama `/api/embed`) and pull in
    the relevant files ahead of the question
  * `[to do]` Read a project `VOX.md`/`AGENTS.md` automatically as context, following
    what has become the convention among coding clients
  * `[to do]` Prompt caching where the provider supports it, with the tokens saved shown
    in the status bar
  * `[done]` Fitting the prompt to the window, and saying so when it no longer fits
    (`vox_chat/app.py`, `fitting`)
  * `[done]` `/model ctx` to read and rewrite a model's window

---

## 5. Agent mode

* **5.1 The tool loop**
  * `[to do]` Run independent tools in parallel within a turn (the loop is strictly
    sequential today, capped at `max_tool_cycles: 8`)
  * `[to do]` An `edit_file` tool doing exact string replacement, alongside
    `write_file`/`apply_patch` — it is the primitive that produces the fewest pointless
    whole-file rewrites
  * `[to do]` A `todo`/visible-plan tool, so a long task does not lose the thread
  * `[to do]` An undo for the last write, using the snapshot already available when the
    diff is produced
  * `[done]` Explicit confirmation before every write, patch or command, with a unified diff
  * `[done]` Workspace confinement: `..`, symlinks pointing outside and shell operators
    refused, commands run under a timeout

* **5.2 Isolation**
  * `[to do]` Authorisation levels (`ask` / `allowlist` / `deny`) per command, instead of
    the binary `confirm_commands`
  * `[to do]` Optional execution in a container or namespace where available
  * `[to do]` Resource limits on commands (memory, process count), not only time

---

## 6. Security and supply chain

* **6.1 Dependencies**
  * `[to do]` Tighten the constraints: `openai>=1.30` admits all of 1.x as well as the
    2.41.1 in use, and those do not share a surface; same for `textual>=0.60` against the
    8.2.8 actually installed
  * `[to do]` A reproducible lock file (`uv.lock` or `requirements.lock`) for the installer
  * `[to do]` `pip-audit` in CI
  * `[to do]` Add `cryptography` to `_REQUIRED` in `vox_chat/doctor.py:32`: it is a
    declared dependency imported unguarded by `discovery/`, yet `vox doctor` does not
    check for it
  * `[to do]` Reconcile the `openai 3.3.1` line in the `README.md` sample output with the
    2.x that is supported and installed

* **6.2 Project posture**
  * `[to do]` `SECURITY.md` with the reporting procedure
  * `[to do]` Security static analysis (`bandit` or CodeQL) in CI
  * `[to do]` Signed release tags
  * `[done]` Secrets redacted in the logs (`RedactingFilter`)
  * `[done]` Private and loopback addresses refused *after* name resolution, not on the string
  * `[done]` Fetched content labelled as data, not instructions, before it reaches the model
  * `[done]` Short-lived certificates (24h) as the practical form of revocation, with
    automatic renewal
  * `[done]` The `agent_id` = certificate SAN binding, documented and tested

---

## 7. Distribution and packaging

* **7.1 Publishing**
  * `[to do]` Publish to **PyPI**: the only route today is `git clone` plus a script,
    which rules out `pipx install vox` and `uvx vox`
  * `[to do]` A release workflow using OIDC trusted publishing, triggered by the tag
  * `[to do]` Attach the artefacts (wheel + sdist) to the GitHub Release
  * `[to do]` Single-source the version: `0.1.0` is repeated in `pyproject.toml` and
    `vox_chat/__init__.py:3` with nothing checking that they stay in step
  * `[to do]` An optional single-file executable for people without Python
    (`pyinstaller` or `shiv`)

* **7.2 Build**
  * `[to do]` Move to `hatchling` or `uv_build` as the backend: `setuptools` does nothing
    here that the others do not do more simply
  * `[to do]` Add the `Programming Language :: Python :: 3.13` and `3.14` classifiers,
    once CI has verified them
  * `[to do]` Reconcile `requires-python` (`>=3.11`) with `spec.md`, which states Python 3.12

* **7.3 Installation**
  * `[to do]` Exercise the installers in CI on clean runners (see 1.2)
  * `[to do]` A Homebrew formula and an AUR package, once PyPI is in place
  * `[done]` Idempotent POSIX and PowerShell installers, no administrator rights needed
  * `[done]` `vox doctor` with distinct exit codes (0/1/2)

---

## 8. Web search

* **8.1 Source robustness**
  * `[to do]` The `local` backend rests on scraping DuckDuckGo's HTML, which rate-limits
    and sometimes answers with a captcha: add further keyless, stable backends
    (Marginalia, Mojeek, Startpage's API) as additional pillars
  * `[to do]` An on-disk cache of searches and pages with a TTL, so the same query is not
    repeated within a session
  * `[to do]` Honour `robots.txt` in `fetch`, with an option to override it
  * `[to do]` Main-content extraction (readability-style) instead of raw text — `to_text`
    currently keeps navigation and footers too
  * `[done]` The search server runs inside VOX on `127.0.0.1:8888`, no install, no key
  * `[done]` Three documented APIs as the safety net when the general index falls over
  * `[done]` Query language inferred and Wikipedia asked in the right language

* **8.2 Answer quality**
  * `[to do]` Re-rank results by relevance before choosing which to read in full
  * `[to do]` Multi-round search: the model refines the query when the sources fall short
  * `[to do]` Deduplicate sources that repeat the same content

---

## 9. Mesh and CONSENSUS

* **9.1 Protocol**
  * `[to do]` The README states plainly that there is no Byzantine tolerance and that a
    lying member is believed: consider a weighted quorum or flagging outlier answers,
    without promising what is not there
  * `[to do]` A round is as slow as its slowest peer: add a per-round time budget with a
    partial result
  * `[to do]` Explicit revocation of a compromised peer before the 24h expiry
  * `[done]` mTLS with per-agent certificates, `caps_digest` and `incarnation`
  * `[done]` Only the marked `[CNS]…[/CNS]` span leaves the machine, and a test holds that boundary
  * `[done]` A warning every time the sample CA is in use, with `consensus.allow_sample_ca`
    to refuse instead

* **9.2 Mesh observability**
  * `[to do]` Per-peer metrics (latency, response rate, disagreement) on the universe screen
  * `[to do]` A round history readable after the session

---

## 10. Documentation and developer experience

* **10.1 Content**
  * `[to do]` `CONTRIBUTING.md`: how to run the tests, how the code is laid out, what a
    pull request is expected to carry
  * `[to do]` Issue and pull-request templates
  * `[to do]` `spec.md` (25 KB) and `README.md` (15 KB) overlap in several places: decide
    which is the source of truth and have the other point at it
  * `[done]` `CHANGELOG.md` carrying real measurements, not estimates, and the machine
    they were taken on
  * `[done]` `docs/USAGE.md` as the full guide, with mesh diagrams generated by script

* **10.2 Usage**
  * `[to do]` Selectable, persisted themes (`/theme`), using Textual 8.x's theme system
  * `[to do]` `--resume` to reopen the last session without going through `/session-load`
  * `[to do]` A non-interactive mode, `vox -p "question"`, for scripting and pipes
  * `[to do]` Screen-reader check and `NO_COLOR` support
  * `[done]` The bottom row cut down to five keys, the rest in `/help`
  * `[done]` Token inspection with entropy and decision points, and export in four formats

---

## 11. Runtime performance

* **11.1 The generation path**
  * `[to do]` Rate-limit transcript refreshes: every delta currently crosses
    `call_from_thread`, which is the dominant UI-side cost on long answers
  * `[to do]` Measure and record in `CHANGELOG.md` the tokens/s with and without
    `/inspect` — it is already described as "several times heavier" without a figure
  * `[done]` GPU fill measured with `nvidia-smi` rather than inferred, with the real
    numbers on an MX150

* **11.2 Startup**
  * `[to do]` Measure startup time and import `cryptography` and the mesh modules lazily
    when the mesh is off (which is the default)
  * `[done]` Model preload on the server, with a timeout and cancellation

---

## Suggested order

1. **CI + Ruff + test timeouts** (§1.1, §1.2, §2.1) — what makes everything else safe
2. **Suite under two minutes** (§2.1) — the development loop costs 8 minutes a turn today
3. **PyPI + dependency lock** (§7.1, §6.1) — unlocks one-line installation
4. **Break up `app.py`** (§3.1) — before it grows further
5. **MCP** (§4.1) — the most visible functional gap against the state of the art
6. **Reasoning parameters and structured output** (§4.2) — low cost, high return
