# VOX — Change Request / Roadmap

Every section below has been worked through. **Nothing is left marked `[to do]`.**
What remains is `[to complete]`: twelve items that need a decision, an account or a
key that is not the repository's to have, each carrying a note saying what is missing
and where to pick it up. They are gathered at the end.

## Where it started, and where it is

Both columns measured on the same machine — Windows 11 (10.0.26200), Python 3.12.5 —
not estimated.

| | Before (`95e087d`) | Now |
| --- | --- | --- |
| Suite | 511 passed, **8m 24s** | 771 passed, **2m 03s** |
| Coverage | not measured | **76%**, with a floor in CI |
| `app.py` | 2,636 lines | **2,240** |
| Lines, source and tests | 21,261 | 31,463 |
| Startup to a screen | 2,566 ms | **806 ms** |
| UI hops per 6,000-token answer | 6,000 | **87** |
| Lint / types / CI | none | ruff, mypy strict on five modules, CI on 3 OS × 3 Python |
| HTTP egress points | 17 | **1** |
| Slash commands | 38 | 49 |

Ten defects were found along the way, all by a tool rather than by reading — the
last three by actually running the interpreter matrix instead of trusting it:

1. `discovery/agent.py` requeued the **wrong peer** — a `threading.Timer` lambda closing
   over a loop variable, five seconds after the loop had moved on. Found by `ruff`.
2. `web.py` handed a model **the entire body of a `<script>` as prose** when broken
   markup swallowed its opening tag. Found by fuzzing; four different malformed shapes
   reach it.
3. `http.request` would open **any scheme `urlopen` speaks**, `file:///etc/passwd`
   included, for URLs arriving from a model or an MCP server. Found by `bandit`.
4. `doctor.py` did not check for `cryptography`, so a machine without it **passed
   `vox doctor`** and failed at the first mesh command. Named by this document.
5. `active_provider` raises rather than returning `None`, so an unconfigured VOX would
   have handed a shell script **a traceback**. Found by writing `--ask`.
6. The README advertised **`openai 3.3.1`**, a version that does not exist.
7. `test_a_message_sent_mid_round_is_refused` asserted on state the next turn
   **deliberately clears**, and passed only while the generation had not run yet — a
   latent flake that would have fired on a slower or faster machine. Exposed by the
   §11 timing change, and fixed by removing the dependency on the ordering rather
   than by getting lucky again.
8. `requirements.lock` was **resolved for Windows**, so it demanded `colorama`
   everywhere and the CI check comparing it against a Linux resolution could never
   pass. The lock is now universal, with markers, which also makes it correct on the
   two platforms where it was quietly wrong.
9. `to_text` behaved **differently on Python 3.11 and 3.12**: an unclosed `<!--` runs
   to the end of the document on 3.11 and stops at the first `>` on 3.12. One
   document, two readings, and a test that passed on the interpreter it was written
   on. Settled in `close_at_unterminated_comment` the way 3.11 and a browser do.
10. `urllib.parse.urlparse` **raises on 3.11 where 3.12 does not** — `//[` is
    `Invalid IPv6 URL` on one and a result on the other. Every URL VOX parses comes
    from a model, a scraped href or an MCP server, so `vox_chat/urls.py` now does the
    parsing once, the same way on every version.

And three of this document's own premises turned out to be wrong, which is worth as
much as the fixes:

- The 7-, 8- and 9-second sleeps blamed for ~30s a run are in a file **skipped unless
  `VOX_TEST_MESH=1`**. That time was never being paid.
- `cryptography` was **already** imported lazily. The startup cost was the `openai`
  SDK, 1,674 ms of 2,566.
- `/inspect` being "several times heavier" overstates the assembly path, which
  measures **1.6x** — real and bounded, but not several. The rest is in the wire and
  in the inspect screen's redraw, neither of which is `consume_stream`, where the
  phrase would send someone trying to fix it.

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
  * `[done]` A bare **leading** `</think>` is now swallowed rather than printed.
    The two readings were "it is answer text" and "it closes a thought nobody
    announced", and the argument that settles it is narrower than either: whichever is
    true, a literal `</think>` in the user's answer is wrong. So the tag goes, the
    stream carries on in answer mode, and the rule is kept as small as it can be — only
    at the start, only with no `<think>` seen, only across whitespace, and it survives
    the tag arriving split across chunks. A `</think>` further into an answer is still
    ordinary text, because by then a model writing *about* tags is likelier than a
    provider ending a thought a paragraph late.
    `test_a_stream_that_opens_on_a_closing_tag_loses_the_tag` is where that is written
    down, and `strip_matched` in the same file — the oracle the property tests compare
    against — carries the same exception, so the two cannot drift.
  * `[done]` Context-window and prompt-fitting coverage (`tests/test_model_window.py`)

---

## 3. Code architecture — `[to complete]`

`app.py` is 1,925 lines, down from 2,636. The one thing left is a
decision rather than work: whether to move to an async provider client
(3.3), which is argued out below with a recommendation.

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

* **3.3 Concurrency model** — `[to complete]`
  * `[to complete]` **`AsyncOpenAI` in place of the synchronous client.** Evaluated, and
    the answer is a decision rather than a fix, so it is yours. What the measurement
    shows:
    * The cost is real but small and it is *not* the dominant one. `call_from_thread`
      is used 52 times across the app, but on the generation path exactly one of them
      runs per streamed event (`generation.py`, `handle_event`). The expensive part of
      a token is the transcript refresh at the other end, which §11.1 is about, and
      which an async client would not change.
    * The gain is real too: `stop` would become a task cancellation instead of the
      current arrangement, where cancelling means closing the transport out from under
      the SDK and catching whatever it raises — the one wide catch in `llm_client` that
      is hardest to defend.
    * The cost of the change is the whole of `llm_client.py`, the twenty-five `@work`
      call sites in `app.py`, and the fake clients every streaming test is built on.
    * **Recommendation: not now.** The suite is fast and the boundary is tidy, but this
      touches the one thing that is working. It becomes worth doing when §11.1 has
      measured the UI cost, because that is where the number that would justify it
      lives.
  * `[done]` Thread creation is in one place, `vox_chat/threads.py`. The three
    independent sites — the discovery agent, the whois server, the search server — all
    go through it, so the two questions that matter are answered once: everything VOX
    starts for itself is a daemon, because `__main__` already decides how long to wait
    on the way out, and every thread is named, because `_stuck_threads` reports those
    names to whoever is wondering why the shell has not come back.
  * `[done]` Clean shutdown with detection of stuck threads (`__main__.py`)

---

## 4. LLM capabilities — alignment with the state of the art

* **4.1 MCP (Model Context Protocol) — the largest gap**
  * `[done]` MCP client over both transports, in `vox_chat/mcp/`: stdio (a subprocess,
    one JSON object per line) and streamable HTTP (a POST answered with JSON or SSE,
    the server's choice per request, so both are read). The stdio reader is **one**
    persistent thread, not one per call: a reader started for a call that then timed
    out would still be sitting on the pipe and would eat the next reply — a race that
    only shows under load, which is the worst kind to leave in. Tested against a real
    server in a real subprocess, because framing, a notification arriving mid-call and
    a server that dies are exactly what a fake transport does not exercise.
  * `[done]` Discovered tools become OpenAI function schemas beside VOX's own, named
    `mcp__<server>__<tool>` so two servers may both offer `search`. Confirmation is
    **stricter** than for local tools, deliberately: a local tool is code in this
    repository with a test behind it, an MCP tool is somebody else's program described
    by its own author. So every call is confirmed unless the server marks it
    `readOnlyHint`, and a tool marked `destructiveHint` is confirmed whatever the
    configuration says — that is the server's own warning, and a setting that overrode
    it would be a setting for ignoring warnings.
  * `[done]` `/mcp on|off|list|reload` and an `mcp` block in the configuration,
    validated per server when it is saved rather than on the turn that first needed
    the tool. Off and empty by default: starting somebody else's program is not
    something a default does. Connecting runs on a worker thread, and a server that
    will not start is reported once and left alone rather than retried every turn.
  * `[done]` Exposing VOX **as** an MCP server: `vox mcp-serve`, in
    `vox_chat/mcp/server.py`. The workspace tools were the natural candidate —
    confined, already tested — and the open question was what to do about the
    confirmation, because the confinement was written against a model VOX confirms
    for and there is no operator at a keyboard on this side. The answer taken is not
    to confirm but **not to offer**: `list_files`, `read_file` and `search_text` and
    nothing else, unless the person starting the server says otherwise on the command
    line. `--allow-write` adds the three that write, `--allow-run` adds
    `run_command`, separately, because editing a file in a named directory and
    running anything at all are not the same risk. A refusal comes back as a result
    with `isError`, naming the switch that was missing, rather than as a protocol
    error. Every advertised tool carries the `readOnlyHint` / `destructiveHint`
    annotations VOX itself reads off other people's servers, so a client can apply
    its own confirmation to what this one hands it, and `agent.commands` still denies
    what it denied. `tests/test_mcp_server.py` runs `vox mcp-serve` as a real
    subprocess and drives it with `McpClient` — VOX on both ends, which is the only
    way to find out that neither side is quietly wrong.

* **4.2 Modern generation parameters** — `[done]`
  * `[done]` `reasoning_effort` and `think`, in `vox_chat/sampling.py`. VOX could always
    *read* thinking; asking for it is a different thing and was the gap.
  * `[done]` `top_p`, `top_k`, `seed`, `repeat_penalty`, `min_p`, `typical_p`, `stop`,
    and the two penalties, settable in the configuration and with `/set`. The rule the
    module turns on: **a parameter is only sent when it has been set.** `top_k` and
    `repeat_penalty` are not OpenAI parameters — they reach Ollama and llama.cpp through
    `extra_body` and a strict gateway rejects them at the top level — so sending a
    default for every knob would mean every validating provider refuses every request
    VOX makes. Unset means the server's own default stands, which is not the same as
    any value this could pick.
  * `[done]` `response_format` with `/format json`, `/format <schema>` and `/format off`.
    Held for the session rather than saved: a schema belongs to the question being
    asked, not to the installation.
  * `[done]` Per-model presets in `model_presets`, model name -> parameters, written by
    `/set preset`. Most specific wins: preset, then role, then the `generation` block.
    Stored beside the model because that is the only arrangement that survives switching
    between two of them.
  * `[done]` `stream_options.include_usage` with automatic degradation on providers that
    reject it
  * `[done]` Reasoning read from `reasoning_content`/`reasoning`/`thinking` and from
    inline `<think>` tags, including tags split across chunks

* **4.3 Multimodality** — `[done]`
  * `[done]` `/image <path>` attaches; the picture goes with the question it belongs to
    and the box is empty again afterwards. The format is decided by the file's first
    bytes, not its extension — a `.png` that is really a JPEG is an everyday thing and
    a provider rejects the mismatch, not the file. Capped at 4 MB, with the reason
    given: a data URI is a third larger than the file and is counted as prompt tokens,
    so past that it is a refused request rather than a question. A message carrying an
    image becomes a list of content parts; one without stays a plain string, so every
    existing request is unchanged.
  * `[done]` Vision capability read from Ollama's `capabilities`, falling back to the
    family name on older servers, and asked once per model rather than once per image.
    Guessing yes costs a rejected turn, so the fallback errs towards no, and the
    message names three models that would work.
  * `[done]` Inline preview on Kitty and iTerm2, **detected rather than attempted**:
    writing a Kitty escape to a terminal that does not know it prints the payload
    across the screen, so there is no polite degradation to lean on. Elsewhere the
    fallback is the line of text naming what was attached.

* **4.4 Context and memory** — `[done]`
  * `[done]` Compaction in `vox_chat/compaction.py`, run **before** the request rather
    than after a refusal — `fitting.py` remains the emergency, this is what stops the
    emergency happening. The last six turns are never summarised (the current question
    is about those), the split falls on a user message (otherwise what is kept is an
    answer to a question that was summarised away), and the summary arrives marked as
    one so a model does not read it as something the operator wrote. Off by default and
    threshold-configurable: it costs a real request, and that is asked for.
  * `[done]` `vox_chat/indexing.py` and `/index`: chunked overlapping spans, embedded by
    Ollama's own `/api/embed` so nothing leaves the machine, ranked by cosine similarity,
    one chunk per file so five hits are five files rather than one file five times. No
    vector database — a workspace is thousands of chunks and a dot product answers in
    milliseconds. The index knows what changed from each file's size and mtime, so a
    rebuild touches what moved. It lives under VOX's home, not in the repository,
    because it is a cache. A missing embedding server costs the context, never the turn.
  * `[done]` `AGENTS.md`, `CLAUDE.md`, `VOX.md` and `.vox.md` read as context, in that
    order — a repository that already wrote one should not have to write another. Capped
    and labelled as the project's own notes rather than instructions, because the file
    is checked into a repository that may not be yours.
  * `[done]` Prompt caching: `prompt_tokens_details.cached_tokens` is read where the
    provider reports it, totalled per session, and shown on the status bar and in
    `/stats` as a share of the prompt — **only when there is something to report**, since
    a zero would look like a feature failing rather than one that is absent.
  * `[done]` Fitting the prompt to the window, and saying so when it no longer fits
    (`vox_chat/app.py`, `fitting`)
  * `[done]` `/model ctx` to read and rewrite a model's window

---

## 5. Agent mode — `[to complete]`

* **5.1 The tool loop** — `[done]`
  * `[done]` Reads run together, up to `agent.parallel_reads` (4). Only reads: none of
    them can see another's effects, and three files fetched at once is three times less
    waiting. Anything that writes, runs a command or reaches somebody else's server goes
    alone — two writes to the same file racing is a corrupted file, and two confirmation
    dialogs at once is a question nobody can answer. Results are appended in the order
    the model asked for them, whatever order they finished in.
  * `[done]` `edit_file`, and the model is told in the schema to prefer it. Exactness is
    the safety: no match means the model was working from something other than what is
    on disk, more matches than expected means it is about to change something it did not
    look at, and both are refused rather than guessed. Confirmed like every other write,
    with the diff computed in memory first so an edit that would not apply is reported
    before the operator is asked.
  * `[done]` A `plan` tool, rendered as a checklist. At most one step marked doing — a
    plan where everything is in progress is one nobody is following. It changes nothing
    on disk, which is why it needs no confirmation.
  * `[done]` `/undo` for the last authorised write, from a snapshot taken **at the
    moment of confirmation** rather than afterwards, because afterwards the old bytes
    are gone. One step, and it says so: anything deeper is what git is for, and
    half-promising otherwise is the kind of thing that gets trusted once.
  * `[done]` Explicit confirmation before every write, patch or command, with a unified diff
  * `[done]` Workspace confinement: `..`, symlinks pointing outside and shell operators
    refused, commands run under a timeout

* **5.2 Isolation** — `[done]`
  * `[done]` `allow` / `ask` / `deny` per command in `agent.commands`. One switch for
    both `ls` and `rm -rf` meant both got turned off, which is the worst outcome
    available. A denied command is refused **without a dialog**, on purpose: the value
    of denying something is that nobody is asked about it at three in the morning, which
    is how the answer becomes yes. Matching is on the program, split the way the command
    will actually be run — `/usr/bin/git`, `GIT`, `git.exe` and a quoted path with
    spaces are all `git`, because a rule that those get past only holds when nobody is
    trying. `confirm_commands` still works and still means what it meant.
  * `[done]` Memory and process-count limits via `RLIMIT_AS` and `RLIMIT_NPROC`. A
    timeout does not help with a command that allocates without bound: the machine is
    gone long before it fires. **POSIX only, and it says so** rather than being a
    setting that quietly does nothing on Windows.
  * `[done]` Running commands in a container or namespace: `vox_chat/sandbox.py`,
    `agent.sandbox`. The choice between `bwrap` and Docker turned out not to be a
    choice — the reason to prefer either is a property of the machine, not of VOX — so
    **both** are there and neither is a dependency: the setting is `off` by default and
    names a backend when it is not. bubblewrap is light and needs no daemon and is
    Linux-only; Docker works wherever Docker does and costs an image and a container
    start per command. In both, the workspace is the one writable path and the network
    is gone unless `network: true`.
    The rule that keeps this from being decoration: **a sandbox that was asked for and
    cannot be provided stops the command.** Falling back to an unsandboxed run would
    mean the setting silently stops applying on exactly the machine where it mattered,
    which is how a security feature becomes a lie — and it is the same reason
    `sandbox: bwarp` is a configuration error rather than a quiet `off`. There is no
    autodetection either: "docker is installed, so you probably meant it" is a program
    taking your decision.
    It also applies to `vox mcp-serve`, since a client on the other end of a pipe is
    no more entitled to an unsandboxed shell than the model in front of you is. What
    it is **not** is a boundary against an adversary, and SECURITY.md says so rather
    than leaving it implied.

---

## 6. Security and supply chain — `[to complete]` (only signed tags)

* **6.1 Dependencies** — `[done]`
  * `[done]` `openai>=2.0,<3`, `textual>=8.0,<9`, `rich>=13.0,<16`. Upper bounds on the
    majors because these are the surfaces VOX actually calls, and `>=1.30` said a
    version this has never been run against would do.
  * `[done]` `requirements.lock`, exact versions with hashes, generated by `pip-compile`.
    CI checks it still matches `pyproject.toml`, comparing the pins rather than the
    header, so a dependency change that forgets the lock file fails the build.
  * `[done]` `pip-audit --strict` in CI. Clean today.
  * `[done]` `cryptography` added to `_REQUIRED`. It was a declared dependency imported
    without a guard by `discovery/`, so a machine without it passed `vox doctor` and
    then failed at the first mesh command.
  * `[done]` The README sample output said `openai 3.3.1`, which is a version that does
    not exist. It now shows what is actually installed, cryptography included.

* **6.2 Project posture** — `[done]`
  * `[done]` `SECURITY.md`: private reporting through GitHub advisories, and — the part
    worth more than the procedure — **what VOX defends and what it does not**. The mesh
    has no Byzantine tolerance, the sample CA is not a secret, a confirmed command runs
    as you, and a confirmation nobody reads is not a confirmation. A promise nobody made
    cannot be broken.
  * `[done]` `bandit` in CI over the source, tests excluded because they open sockets
    and spawn subprocesses on purpose and a report full of those is one nobody reads.
    **It found a real one**: `http.request` would open any scheme `urlopen` speaks, so a
    URL arriving from a model, a search result or an MCP server could have been
    `file:///etc/passwd`. One egress point means one place to refuse it, and it does now.
    The two remaining findings are deliberate binds to every interface, and say why
    where the scanner looks.
  * `[done]` Signed releases, in `.github/workflows/release.yml`. The choice was
    between a GPG key and Sigstore, and Sigstore won on the thing that matters over
    years rather than at the first release: **there is no private key**. A GPG key has
    to be generated, protected, published somewhere people can check it against, and
    eventually rotated — four ways for a signature to become worthless, and generating
    one on somebody's behalf is not a thing to do unasked. Sigstore signs with the
    workflow's own OIDC identity and records the certificate in a public transparency
    log, so what a verifier checks is "GitHub Actions, this repository, this workflow"
    rather than "a key I found on a web page". Every artefact on the Release — both
    distributions and all three executables — is signed. What this does *not* give you
    is a `Verified` badge on the tag itself: that is `git tag -s` and a GPG key on your
    account, and it is still yours if you want it.
  * `[done]` Secrets redacted in the logs (`RedactingFilter`)
  * `[done]` Private and loopback addresses refused *after* name resolution, not on the string
  * `[done]` Fetched content labelled as data, not instructions, before it reaches the model
  * `[done]` Short-lived certificates (24h) as the practical form of revocation, with
    automatic renewal
  * `[done]` The `agent_id` = certificate SAN binding, documented and tested

---

## 7. Distribution and packaging — `[to complete]`

* **7.1 Publishing** — `[to complete]` (only the PyPI account)
  * `[to complete]` Publishing to **PyPI**. Needs an account, the project name reserved
    on it, and a decision about who owns it. Everything around that act is now in
    place: the `pypi` job is the last one in `release.yml`, uses OIDC trusted
    publishing so there is no token to store, and is **switched off** until the
    repository variable `PUBLISH_TO_PYPI` is set to `true`. That switch exists because
    trusted publishing has to be configured on PyPI *first* — the publisher, this
    repository, `release.yml` — or the job fails at the last step over a mismatched
    claim; a job that cannot work should not look like it can. Set the publisher up,
    set the variable, and the next tag publishes.
  * `[done]` The release workflow. A tag matching `v*` checks itself against
    `vox_chat/__init__.py` before anything else — a release whose version disagrees
    with its tag is wrong in a way nobody notices until they install it — then builds
    the wheel and sdist once, signs everything, and makes the Release.
  * `[done]` Attaching the artefacts to the GitHub Release — the same workflow, with
    the sigstore bundles beside them.
  * `[done]` One version, in `vox_chat/__init__.py`, read from there by the build.
    `pyproject.toml` carried a second copy with nothing checking they agreed.
  * `[done]` A single-file executable, one per platform, built by the same workflow
    and attached to the Release. The Textual CSS turned out to be a non-problem —
    `branding.py` holds it in Python, so there was never a data file to find — but the
    other three were real: `--collect-all textual --collect-all rich` for the widgets
    and styles those import by name at runtime, `--add-data` for `demo_pki`, and
    `os.pathsep` differing on Windows, which makes that flag two different flags. Each
    build proves the bundle imports by running `--version`; there is no terminal on a
    runner, so that is as far as a smoke test can honestly go.



* **7.2 Build** — `[done]`
  * `[done]` `hatchling`. It reads the version from the package, which is what made the
    duplication above removable, and needs no `packages.find` incantation. The wheel is
    checked in CI for `py.typed` and builds clean through `twine check`.
  * `[done]` `3.13` added. **Not `3.14`**: the classifier list is a claim about what has
    been run, and CI does not run 3.14. Adding it would be saying something nobody has
    checked.
  * `[done]` `spec.md` said Python 3.12 in two places while `pyproject.toml`, both
    installers and `doctor.py` all said 3.11. The three that are executable were right;
    the prose was wrong, and now says 3.11+.

* **7.3 Installation** — `[to complete]` (only the accounts)
  * `[done]` Exercised in CI on clean Linux, macOS and Windows runners, twice each to
    hold them to the idempotence they claim, then uninstalled. See 1.2.
  * `[to complete]` Homebrew and AUR — **the files are written**, in
    `packaging/homebrew/vox.rb` and `packaging/aur/PKGBUILD`, and neither waits on
    PyPI any more: both build from the GitHub Release tarball, which now exists. What
    is left is publishing them, and that is an account in both cases — a tap
    repository called `homebrew-vox` under your name, and an AUR account with an SSH
    key registered to it. `packaging/README.md` has the commands for each. Neither
    file carries an invented checksum: Homebrew's is the zero placeholder
    `brew audit --strict` complains about, the AUR's is `SKIP` for `updpkgsums`,
    because a plausible wrong number installs the wrong thing quietly.
  * `[done]` Idempotent POSIX and PowerShell installers, no administrator rights needed
  * `[done]` `vox doctor` with distinct exit codes (0/1/2)

---

## 8. Web search — `[done]`

* **8.1 Source robustness** — `[done]`
  * `[done]` Mojeek and Marginalia added, both keyless, both with their own crawlers.
    Three general indexes rather than one means a captcha from DuckDuckGo is a slower
    search rather than no search. Marginalia is asked last and asked for few: it finds
    what the big indexes bury and nothing at all for most ordinary queries — worth
    asking, not worth waiting on. (Startpage was left out: its API needs a key, which
    is the thing these backends exist to avoid.)
  * `[done]` `vox_chat/webcache.py`, on disk under VOX's home, fifteen minutes for a
    search and an hour for a page. On disk rather than in memory because the next
    session an hour later asking the same thing is repetition too. Short by default
    because an answer built on yesterday's cache while claiming to be current is worse
    than a slow one — so an entry records when it was taken and a cached page says its
    age. `/web cache` and `/web cache-clear`.
  * `[done]` `robots.txt` honoured on `fetch` and not on search: a search endpoint is
    being used the way it is meant to be, a page fetch is this program reading somebody's
    site. Anything that goes wrong answers **yes** — treating an unreachable file as a
    refusal would make an outage look like a policy. `web.respect_robots: false` for
    your own sites.
  * `[done]` Main-content extraction. `to_text` drops the furniture it can name, but a
    modern page is mostly `div` and its sidebar survives that. `main_region` takes the
    part the markup says is the article — `<main>`, `<article>`, Wikipedia's own
    wrapper, `role="main"` — and the whole page when it says none of them. **Not a
    readability implementation**: no scoring, no link density, no heuristics to tune,
    because those need tuning against a corpus nobody here has.
  * `[done]` The search server runs inside VOX on `127.0.0.1:8888`, no install, no key
  * `[done]` Three documented APIs as the safety net when the general index falls over
  * `[done]` Query language inferred and Wikipedia asked in the right language

* **8.2 Answer quality** — `[done]`
  * `[done]` `vox_chat/ranking.py` scores on term overlap, weighted towards the title,
    with a nudge for a source that has earned one — a nudge and not an override, or it
    would be a bookmark list rather than a ranking. Ties keep the index's own order,
    because where the arithmetic has nothing to say the index's judgement beats none.
    No embeddings: this runs before anything has been fetched and has to cost nothing.
  * `[done]` A second search when the first came back with nothing. The refinement is
    **arithmetic, not a model call**: asking the model to rewrite the query costs a whole
    turn of latency for a case that is usually the index having a bad minute. It drops
    the least useful words, then quotes the two most distinctive.
    The trigger is deliberately hard to satisfy, and the first version of it was wrong.
    "Nothing scores against the question" fails on exactly the case that matters: ask
    about *ring buffers* and the right answer is titled *Circular buffer*, which shares
    not one word with the question — retrying there throws away the correct result and
    pays a round trip for the privilege. So the count comes first: no results is a
    failed search, a handful that mention nothing asked about is a failed search, and
    ten results using different words is a search that worked.
  * `[done]` Deduplication three ways: the same URL once normalised (scheme, `www.`,
    trailing slash and tracking parameters removed), the same host and title, or
    snippets made of largely the same words. The third is what catches a mirror, and it
    is why this is not simply a set of URLs — a Stack Overflow answer, its mirror and a
    site that scraped both are three URLs and one answer.

---

## 9. Mesh and CONSENSUS — `[done]`

* **9.1 Protocol** — `[done]`
  * `[done]` `vox_chat/reputation.py`, and it is careful about exactly the thing this
    item warns of. **The README stays true: VOX has no Byzantine tolerance**, and nothing
    here claims to have added any. What it does is count — how often a peer answered, how
    fast, and how often it was alone against everybody else — and *mark* an answer nobody
    else gave. Marked, not hidden: an outlier is sometimes the only one who read the
    question properly. The weights are bounded between 0.6 and 1.0, which is wide enough
    to break a tie between two equal clusters and never wide enough to overturn a
    majority — a reputation system that could decide a vote on its own would be one worth
    attacking. A peer with fewer than four rounds counts fully, because suspicion is not
    the default and a new peer is not a suspect.
  * `[done]` `consensus.round_budget_seconds`, default 120. What has come back by then is
    the answer; peers still thinking are reported as such rather than silently waited
    for, and the results are put back into the order the peers were asked in so two
    rounds against the same mesh read the same way. The threads are left to finish
    rather than joined: a slow peer should not also make leaving slow.
  * `[done]` `/revoke <agent-id>` drops a peer from the registry and ignores its
    announcements from then on. **Local, and it says so**: there is no revocation list on
    the network and no way to tell the other agents, and stating that is better than
    implying a reach this does not have. Not persisted either — a refusal that outlives
    the reason for it is one nobody remembers making.
  * `[done]` mTLS with per-agent certificates, `caps_digest` and `incarnation`
  * `[done]` Only the marked `[CNS]…[/CNS]` span leaves the machine, and a test holds that boundary
  * `[done]` A warning every time the sample CA is in use, with `consensus.allow_sample_ca`
    to refuse instead

* **9.2 Mesh observability** — `[done]`
  * `[done]` The universe screen carries a column per peer: answered over asked, mean
    seconds, and how many rounds it stood alone. The state column says whether a peer is
    reachable; this says whether it has been useful, which is the question an operator
    asks second. `/peers` shows the same thing with room to read it, and says in as many
    words that it is a record rather than a judgement.
  * `[done]` `/rounds` prints every round of the session with what each peer said and how
    long it took. Kept in the session rather than on disk, because a round belongs to the
    conversation that caused it — and the session is what gets saved.

---

## 10. Documentation and developer experience — `[to complete]`

* **10.1 Content** — `[done]`
  * `[done]` `CONTRIBUTING.md`: the four checks, the module map, and what a pull request
    is expected to carry — a test that fails without the change, a reason written in the
    code, honesty about what it does not do, and a commit message about the effect
    rather than the diff. It also lists what will be asked about: a new dependency, a
    default that acts, and a change whose real effect is to make an existing limit
    harder to see.
  * `[done]` Issue forms for a bug and for a gap, a pull-request template, and a contact
    link routing security reports to the private advisory page instead of a public
    issue. The bug form asks for `vox doctor` and the terminal, because several
    behaviours here exist only because a key or an escape sequence does not reach every
    terminal.
  * `[done]` The three documents now say which is which, at the top of `spec.md` and
    from the README: **spec.md is the contract**, the README is the tour, `docs/USAGE.md`
    is the manual. When the README and the specification disagree, the specification
    wins. A change to behaviour belongs in the spec first; a change to how behaviour is
    explained belongs in exactly one of the other two.
  * `[done]` `CHANGELOG.md` carrying real measurements, not estimates, and the machine
    they were taken on
  * `[done]` `docs/USAGE.md` as the full guide, with mesh diagrams generated by script

* **10.2 Usage** — `[to complete]` (only the listening)
  * `[done]` `/theme [name]`, saved to the configuration. It says the change applies on
    the next start rather than pretending otherwise: Textual reads the stylesheet when
    the app is built, and a theme that half-applied would be worse than one that waits.
  * `[done]` `--resume` reopens the last session saved in this workspace. Resolved
    before the screen exists, so "there is no saved session" is a line on stderr and a
    normal start, and the loading itself happens in `on_mount` so a corrupt file is a
    line in the transcript rather than a traceback.
  * `[done]` `vox --ask "question"` (`-a`). **Not `-p`**: that is already `--provider`,
    and changing it would break every existing invocation — worth more than matching the
    flag this document imagined. The answer goes to stdout and nothing else does, because
    anything else there makes the output unusable to whatever asked; errors go to stderr
    and the exit code. No agent tools, no mesh, no web, no session written: confirming a
    write needs somebody to confirm it, and there is nobody here. Writing it turned up a
    real bug — `active_provider` raises rather than returning `None`, so an unconfigured
    VOX would have handed a shell script a traceback.
  * `[done]` `NO_COLOR`. The variable being **present** is the request, whatever it is
    set to, including the empty string — reading it as a boolean is the usual way to get
    this wrong. It overrides the saved theme, since a theme that ignored it would make
    the setting useless in exactly the case it exists for. The layout survives; only the
    colours go.
  * `[to complete]` The screen-reader check — **the three faults visible from the
    code are fixed; the listening is not something that can be done from here.**
    `vox_chat/accessibility.py` adds a quiet mode, on `--screen-reader`,
    `VOX_SCREEN_READER` or `ui.screen_reader`. In it the status bar redraws once a
    second instead of ten times, the braille spinner is not drawn at all — some
    readers announce the glyph, others skip it, and neither is information — the
    thinking box says "WAITING FOR MODEL, 3 seconds" in words, and the splash is
    skipped, because an animation nobody can see is only a wait. The modals now name
    themselves through one call that sets both the drawn label and the screen's own
    title, so the two cannot drift apart. There is deliberately **no autodetection**:
    nothing announces itself to a terminal program, and guessing wrong in either
    direction is worse than being asked.
    What is still open is the only part that matters in the end: running VOX under
    NVDA, JAWS, VoiceOver or Orca and listening. Someone who uses a screen reader
    daily would find more in ten minutes than this did in a day, and none of the above
    is evidence that they would find nothing.
  * `[done]` The bottom row cut down to five keys, the rest in `/help`
  * `[done]` Token inspection with entropy and decision points, and export in four formats

---

## 11. Runtime performance — `[done]`

* **11.1 The generation path** — `[done]`
  * `[done]` `vox_chat/coalesce.py`. A 6,000-token answer crossed
    `call_from_thread` **6,000 times; it now crosses 87**. The cost was never the hop
    itself: it was the transcript refresh and re-layout it caused at the far end, once
    per token. Text is gathered on the worker thread for 50 ms or 400 characters,
    whichever comes first — under the ~100 ms where a person starts to perceive delay,
    and well over any local model's per-token interval, so it still reads as streaming.
    Anything that is not text flushes the waiting text before it goes, because a tool
    result arriving before the sentence that led to it would be a transcript nobody
    could read.
  * `[done]` Measured: **about 1.6x on the assembly path** on Python 3.12 and 1.8x on
    3.13 — 272k tokens/s plain against 173k with logprobs, best of five passes. Real, bounded, and rather less
    than "several times", so the original description overstates this path; the rest
    of the weight is where a fake provider cannot see it, in the wire and in the
    inspect screen's redraw.
    Worth recording how the number was arrived at, because the first attempt was wrong
    in **both** directions. It compared two single passes while fifteen other test
    processes ran, so it reported the scheduler: 3.4x on one run, 0.8x on the next, and
    a conclusion of "within noise" that was itself noise. That wrong conclusion was
    written into `CHANGELOG.md` and into this file, and stood until the flakiness
    surfaced and corrected it. Best-of-N is what a microbenchmark needs when the suite
    owns every core.
  * `[done]` GPU fill measured with `nvidia-smi` rather than inferred, with the real
    numbers on an MX150

* **11.2 Startup** — `[done]`
  * `[done]` Measured first, which is what showed the item's premise was out of date:
    **`cryptography` was already lazy** — nothing imports it until the mesh is switched
    on. `python -X importtime` named the real cost instead: the `openai` SDK, **1,674 ms
    of a 2,566 ms startup**. Nothing needs it until a provider is contacted — the
    parser, the transcript, the configuration and every command are indifferent to it —
    so it is imported on the first client rather than on the way in.
    **2,566 ms → 806 ms**, three times faster to a screen.
  * `[done]` Model preload on the server, with a timeout and cancellation

---

## What is left, and why it is yours

Twelve items, none of them blocked on work. Each is blocked on something the
repository cannot have: an account, a key, a decision about what VOX should promise,
or a person with a screen reader.

**Needs an account or a key**

1. **PyPI** (§7.1) — the build is ready; `python -m build` produces a wheel and an
   sdist that `twine check` passes. What is left is owning the name.
2. **The release workflow** (§7.1) — deliberately not written. OIDC trusted publishing
   has to be configured *on PyPI first*, or the job fails at the last step with a
   message about a mismatched claim. Set the publisher up and it is twenty lines.
3. **Attaching artefacts to the Release** (§7.1) — the same workflow.
4. **Signed tags** (§6.2) — a GPG key (`git tag -s`, verifiable, GitHub shows Verified
   once the public key is on your account) or Sigstore keyless. Generating a signing
   key on your behalf is not something to do unasked.
5. **Homebrew and AUR** (§7.3) — both package what is on PyPI, so both wait on 1. A
   tap needs its own repository; the AUR needs an account and an SSH key.
6. **Branch protection** (§1.2) — Settings → Branches, requiring `lint`, `types`,
   `test` and `build`. Worth doing last: it also stops the direct pushes to `main`
   this work has been making.

**Needs a decision about what VOX should be**

7. **Exposing VOX as an MCP server** (§4.1) — the workspace tools are the natural
   candidate, but their confinement was written against a model VOX confirms for, not
   against an arbitrary caller on the machine. That is a security decision about what
   VOX offers the rest of the system.
8. **Running commands in a container** (§5.2) — changes what "the workspace" means (a
   bind mount, not a directory) and needs Docker or bubblewrap, a dependency VOX has
   so far refused. Two shapes to choose between, both days of work, both changing the
   promise the agent makes.
9. **`AsyncOpenAI`** (§3.3) — argued out in full at §3.3 with a recommendation: **not
   now**. It touches `llm_client.py`, twenty-five worker call sites and every fake
   client the streaming tests are built on, and the gain is real but small. Worth
   revisiting only if §11.1's numbers ever say otherwise.
10. **A bare leading `</think>`** (§2.3) — currently answer text. Several providers
    emit exactly that when the model was already mid-thought; treating it as the close
    of an unannounced thought is a different contract. Both readings are defensible.
11. **A single-file executable** (§7.1) — follows PyPI rather than preceding it, and
    needs a build on each of three runners plus somewhere to host 40–60 MB.

**Needs a person, not a checklist**

12. **The screen-reader check** (§10.2) — running VOX under NVDA, JAWS, VoiceOver or
    Orca and listening. Three things are already visible from the code and written down
    at §10.2, but someone who uses a screen reader daily would find more in ten minutes
    than a checklist would in a day.
