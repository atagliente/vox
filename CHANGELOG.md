# Changelog

Everything worth knowing about how VOX got here, newest first.

Nothing has been released yet: every entry below belongs to the unreleased
0.1.0. Dates are the day the work landed on `main`. Version numbers will follow
[semantic versioning](https://semver.org) once there is a release to number.

Where an entry says a figure — a timing, a token count — it was measured on the
machine and version named, not estimated.

---

## Unreleased — 0.1.0

### Token inspection and reports

**2026-08-25** · [`ebcc1f5`](https://github.com/atagliente/vox/commit/ebcc1f5),
[`12de22f`](https://github.com/atagliente/vox/commit/12de22f)

- **Live inspection.** `Ctrl+T` (or `F2`, or `/inspect`) turns on the
  measurement and opens a full-screen table that fills while the answer
  streams: per token, the probability the model gave it, the entropy of the
  returned top-k in bits, the margin to the runner-up, and the alternatives it
  passed over. Filterable to decision points, thinking or answer.
- **A legend on `Ctrl+L`** explains every column in the units it is in, and
  quotes the decision-point criteria actually in force rather than the
  defaults.
- **Decision points** — positions that were both spread and close — are marked.
  The four criteria (entropy threshold, margin threshold, minimum distance,
  punctuation) are configuration, and the report prints the values that
  produced the numbers.
- **Reports.** `/export` or `Ctrl+E` writes `~/.vox/reports/vox-<timestamp>` as
  HTML, JSON and Markdown: the question, model, endpoint and the parameters
  actually sent, then the exchange with thinking kept separate, then the
  statistics and the decision points, then provenance. The HTML is
  self-contained and carries no JavaScript, so it reads with scripting
  disabled.
- **Off until asked.** Logprobs multiply the size of every response, and not
  every provider supports them. A provider that rejects them is retried
  without, reported once, and the answer is untouched.
- **Deliberately absent**: attention, activations, model internals, and
  regenerating from a decision point with a forced alternative. Nowhere is a
  spread distribution described as the model thinking or hesitating; entropy is
  labelled top-k everywhere, because the API does not return the tail.

Measured on Ollama 0.32.15 before any of it was written: logprobs arrive during
streaming, `top_logprobs` accepts 1–20 and refuses 25 with HTTP 400, logprobs
coexist with tool calls, and thinking tokens carry them too.

Two mistakes found and fixed on the way:

- Attributing a token's phase from its own chunk left **98 of 359 tokens
  unattributed** on a reasoning model, because about a quarter of chunks carry
  a logprob with an empty reasoning string, mid-thinking. The phase now
  continues until a delta says otherwise: the same request gives 246 thinking,
  1 answer, 0 unattributed.
- The view was bound to `Ctrl+I`, which terminals send as the same byte as
  `Tab` — Textual's own alias table says `tab <- ['ctrl+i']` — so the binding
  never fired. The headless tests missed it because `pilot.press("ctrl+i")`
  injects the key name directly, skipping the translation a terminal performs.
  A test now compares every binding against that alias table and fails if one
  shares a byte with another key.

### Shutdown, preloading, installers

**2026-08-24** · [`9f9963f`](https://github.com/atagliente/vox/commit/9f9963f)

- **Quitting no longer holds the shell.** The screen went in 0.14s but the
  process lived on for **124 seconds**, because Textual joins its worker
  threads at interpreter exit and one was blocked on a request the provider
  never answered. Closing the client does not unblock it — verified: the thread
  is still alive ten seconds later — so the app shuts itself down explicitly
  and, if a worker is still stuck after a short grace period, leaves anyway.
  Worst case now **5.06s**; a clean exit pays nothing.
- **Preloading stopped looking like a hang.** It showed one line and then
  nothing, behind a ten-minute provider timeout. It now has a spinner counting
  the seconds that names the model *and the endpoint*, a status bar reading
  PRELOADING instead of IDLE, a bounded timeout
  (`generation.preload_timeout_seconds`, 180 by default) that explains what
  happens next, and `Ctrl+G` to stop waiting.
- **Installers.** The PATH entry was skipped entirely when pipx was used on
  Windows, and both scripts assumed `~/.local/bin` instead of asking pipx where
  it puts launchers. Both now ask, and both offer to put it on the PATH, so
  `vox` works from any directory.

### Diff before a write

**2026-08-24** · [`8ecb2f2`](https://github.com/atagliente/vox/commit/8ecb2f2)

- The confirmation dialog reported *"42 lines, 1200 characters"*, which is not
  enough to decide whether to let a model touch a file. It now shows the
  unified diff of exactly what would change, removals in terracotta, additions
  in sage.
- The **patch preview is produced by applying the diff in memory**, so a patch
  that does not fit the current file is reported *before* it is offered for
  approval, and nothing is written until you approve.
- New files, no-op rewrites, non-UTF-8 files and oversized diffs each get a
  clear message instead of a misleading diff.
- Fixed in passing: building the preview reads the workspace, so it can refuse
  a path; that refusal used to escape the worker instead of being treated as a
  denied call.

### Clipboard and the code panel

**2026-08-24** · [`4e974ef`](https://github.com/atagliente/vox/commit/4e974ef),
[`01f1ef8`](https://github.com/atagliente/vox/commit/01f1ef8)

- **Copy and paste reach the real clipboard.** Textual can only manage OSC 52,
  which many terminals ignore, so VOX shells out to the platform's own helper —
  PowerShell, `pbcopy`/`pbpaste`, `wl-copy`, `xclip`, `xsel`,
  `termux-clipboard-*` — never through a shell, under a timeout, in a worker.
  When no helper exists the key says so instead of doing nothing, and
  `vox doctor` reports what is available.
- **Code on the right.** Fenced blocks from the answer appear in the side
  panel, flush left and without the fences, so a terminal selection yields
  exactly the code. `Ctrl+Y` copies the last block, `/code <n>` copies one by
  number.
- The panel **follows the answer as it streams**: it used to be rebuilt only
  when a turn ended, so on a slow model it kept showing the previous answer's
  code for as long as the new one took to arrive.
- Fixed: the status timer kept running through shutdown and queried widgets
  that were already gone.

### The key map

**2026-08-24** · [`b531a3c`](https://github.com/atagliente/vox/commit/b531a3c)

- `Ctrl+C` copies and `Ctrl+V` pastes, plainly. `Ctrl+Q` is the only key that
  quits, `Ctrl+G` the only one that stops a generation. The previous map
  overloaded `Ctrl+C` with copy, stop and quit, which is not what that key
  means anywhere else.
- `Ctrl+S` opens the settings; saving a session moved to `Ctrl+W`.
- The key legend is ordered by usefulness, so a narrow terminal drops the least
  important entries rather than `quit`.
- Earlier the same day: `Enter` became send, because `Ctrl+Enter` is not
  delivered by most terminals; new lines are `Alt+Enter`, `Ctrl+J` or
  `Shift+Enter`.

### First version

**2026-08-23** · [`1128f96`](https://github.com/atagliente/vox/commit/1128f96),
[`27b8ad4`](https://github.com/atagliente/vox/commit/27b8ad4)

A TUI chat client for coding against any OpenAI-compatible endpoint — Ollama,
llama.cpp server, vLLM, LM Studio, a gateway — on Linux, macOS, Windows and
Termux. 44 files, 8594 lines.

- Streaming that never blocks the UI: generation runs in a worker thread.
- Layered configuration, global plus per project, with a predictable merge; a
  broken edit never destroys the running configuration.
- Roles, reusable prompts and sessions, persisted with atomic writes.
- Token accounting: context fill, tokens, tokens per second, session averages —
  measured from the first token, so loading a cold model is not reported as
  generation speed.
- Reasoning shown in its own block, from a dedicated field or from inline
  `<think>` tags split across chunks.
- Optional coding-agent mode: six tools confined to a workspace, writes and
  commands gated behind an explicit confirmation, no bypass.
- Model preloading and provider `extra_body`, for slow local servers.
- Preflight installers for POSIX and Windows, plus a `vox doctor` check.
- PolyForm Noncommercial 1.0.0.

### Presentation

**2026-08-24** · [`17fb838`](https://github.com/atagliente/vox/commit/17fb838),
[`2969a91`](https://github.com/atagliente/vox/commit/2969a91)

- A screenshot of the running app in the README, and a logo drawn in the same
  mission-control palette as the terminal theme.
- Earlier: the whole interface moved from bright phosphor green to a muted
  1970s mission-control palette, and the framed header learned to measure
  itself against the real terminal width.

---

## Conventions

- **Tests never need a server.** Streaming, the agent loop and the measurements
  are exercised with fake clients; 272 tests at the time of writing, one of
  which is skipped on Windows because it needs symlink rights.
- **Findings live in the commit message.** Where a change was driven by a
  measurement — a latency, a refused parameter, an alias table — the number and
  its source are recorded, so the reasoning does not have to be reconstructed.
- **Nothing claims to be verified that was not.** Anything checked only in
  theory, or only on one platform, says so.
