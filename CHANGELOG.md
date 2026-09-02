# Changelog

Everything worth knowing about how VOX got here, newest first.

Versions follow [semantic versioning](https://semver.org). Dates are the day
the work landed on `main`.

Where an entry says a figure — a timing, a token count — it was measured on the
machine and version named, not estimated.

---

## Unreleased

### CONSENSUS

**2026-09-02**

- **`[CNS] … [/CNS]` sends the marked part of a message to the other agents on
  the mesh.** Everything outside the tag stays on this machine — the boundary
  is a pure function in `vox_chat/consensus.py`, and a test asserts that a
  message containing a secret alongside a marked question puts only the
  question on the network. An unclosed tag distributes nothing rather than
  guessing where the span ends.
- **The mesh does work, not just discovery.** A new `ASK` operation rides the
  mTLS channel WHOIS already uses: same port, same identity, same authorizer.
  A peer answers with its own model in a fresh two-message conversation, so no
  context travels either way. The socket timeout is raised before the model is
  called — the 5s handshake cap would kill every real answer — and a semaphore
  allows one answer at a time, so a peer cannot queue generations on somebody
  else's hardware.
- **The agents can be watched while they answer.** A peer streams what it is
  writing — reasoning and answer, tagged apart — as newline-delimited JSON on
  the same mTLS connection, before the final reply. No websocket: that channel
  is already an authenticated two-way stream, so adding one would have meant a
  second listener, a dependency and another handshake for nothing. `F5`
  (`/round`) shows the fragments in arrival order, timestamped, one colour per
  agent, italics for thinking — a separate view from the transcript, which
  groups each answer into a block and so loses who was writing at the same
  time as whom. The caller caps how much a peer may stream. Measured on one
  machine with `qwen2.5-coder:3b` answering: the first fragment 4.1s in, then
  roughly ten tokens a second until the answer completed.
- **Both ends of a round can watch it.** The machine being asked filled no
  view of its own: it wrote two summary lines and kept its reasoning to
  itself. `F5` there now shows `ANSWERING`, who asked, the conversation id and
  its own fragments as they are produced, and the transcript gains the answer
  it gave. That answer is shown but never stored in the local session — it
  belongs to the asker's conversation and must not become context for the
  local model, which is a test. A round you started yourself is not
  overwritten by an incoming question.
- **One conversation id ties the round together.** The session already had an
  id; it now travels with the question, so the asking machine's round view, the
  answering machine's log line and the exported report all name the same
  exchange. Reports gained a `conversation_id` field, in the JSON schema and in
  the HTML and Markdown headers, which is what makes two machines' records of
  the same round joinable after the fact.
- **The replies are reconciled, and the reconciliation is shown.** Answers that
  match once normalised form a vote when they reach the quorum *and* a strict
  majority; two out of five agreeing is a coincidence, not a decision.
  Otherwise the local model writes the answer and is told to name where the
  agents differed. Every reply is kept in the transcript, in the side panel
  (`/panel consensus`), in the session and in the report.
- **Works on the sample authority, and warns every round.** A fresh install
  should be able to try consensus without provisioning anything first, so
  `consensus.allow_sample_ca` is true by default. What it costs is stated
  before every round rather than once: the shipped authority's private key is
  public, so anyone on the segment with a copy of VOX can issue themselves a
  certificate, join, and be one of the agents answering — the peer list shown
  is not the same thing as who can read the text. Setting the flag false turns
  the warning back into a refusal, for asking and answering alike. Refusals
  stay explicit when the mesh is offline, consensus is off, the span is
  oversize, or a tag is unclosed; with no peers to ask, VOX says so and answers
  locally.
- **Measured, not assumed.** Two nodes on one machine with a private authority
  and `qwen2.5-coder:3b` answering: the peer went ACTIVE, was asked, and
  answered in 14.0s, effectively all of it the model. The failure paths were
  exercised against a real mTLS server: an oversize question refused before
  reaching the model, a second concurrent caller told `busy`, and a node with
  no handler answering `ask not supported`.
- Honest about what it is: aggregation, not agreement. mTLS proves who a peer
  is, not that it is truthful, there is no Byzantine tolerance, and a round is
  as slow as its slowest peer.

### The agent mesh

**2026-09-01**

- **VOX can join a peer-to-peer mesh of agents.** `Ctrl+Shift+O` (or
  `/mesh on`) starts a discovery agent: a signed announcement multicast to
  239.17.42.1:45177 with a TTL of 1, a WHOIS handshake over mTLS for every peer
  that is new or has restarted, and a registry that moves peers
  PROBATION -> ACTIVE -> SUSPECT -> DEAD. The border turns red for as long as
  VOX is announcing, and the status bar counts what it sees.
- **`Ctrl+Shift+U` (or `/universe`) shows who is out there**, with the category
  each peer's declared verbs produce, its state, address, age and verbs. It
  refreshes once a second while open; `Ctrl+L` explains the states and the
  taxonomy.
- **Going online says exactly what it did** — the agent id, the category, the
  group and the interval — and names what a second machine needs. Announcing
  presence on a network is not something to do quietly.
- **Names cannot collide, whatever the configuration says.** The agent id ends
  in a fingerprint of the machine: the first 12 hex characters of the SHA-256
  of its MAC address. It is appended even when `mesh.agent_id` is set, so one
  `config.json` copied across a fleet still gives every machine its own name,
  its own certificate and its own SAN — two agents announcing the same id
  would each break the other's mTLS handshake. The address is hashed, never
  announced. Where there is no hardware MAC, `uuid.getnode` invents a random
  node and flags it with the multicast bit; that value changes every run, so
  it is rejected and the fingerprint falls back to a hash of user and host.
- **Every agent signs with its own key, and there is no shared secret.**
  Announcements were signed with a pre-shared key, which meant every machine
  signed with the same one: identical signatures, and anyone holding the key
  could announce as anyone else — the CA only entered at the WHOIS, by which
  point the impostor already sat in the registry on probation. Protocol
  version 2 signs each announcement with the agent's own Ed25519 key and
  carries its certificate in the packet (374 bytes DER, 760 in all, well
  inside the 4096 limit). A receiver checks the certificate against its CA,
  requires the announced id to be one of that certificate's SANs, and only
  then checks the signature. Verified by attempting the forgeries: a member
  announcing as another member, a certificate from a foreign CA, an edited
  body, and a borrowed certificate signed with the wrong key — all four are
  refused, now before the packet reaches the registry. `mesh-psk`,
  `$DISCOVERY_PSK` and `ensure_psk` are gone; the only file two machines share
  is `ca.crt`, which is public by nature. The two protocol versions do not
  interoperate.
- **A sample authority ships with VOX, and says so.** Two fresh installations
  used to be two meshes of one: each provisioned its own authority on first
  use and then dropped the other's announcements — verified on this machine,
  both registries empty. VOX now carries a certificate authority in
  `vox_chat/demo_pki/`, copied into `~/.vox/pki` on first use, so a download
  joins a mesh with no setup. Its private key is public by construction, so it
  is never quiet about it: the header reads `Universe: ON-LINE (SAMPLE CERT)`,
  the status bar appends `DEMO CERT`, the transcript says so on going online,
  and `vox doctor` returns a WARN naming the remedy. `mesh.demo_ca` is true by default,
  and says which authority to be on rather than only what to do when there is
  none: a machine that already held a private authority — every installation
  from before this change — is moved onto the sample one when it next goes
  online, its old authority kept as `.replaced-<timestamp>`. `/mesh new-ca`
  generates an authority private to the machine, drops the certificates issued
  under the old one, reissues this agent and sets the flag false so a later
  start does not undo it; `/mesh sample-ca` goes back. A machine on a private
  authority sees a machine on the sample one as a stranger, which is a test.
  Swapping twice inside one second used to collide on the backup name and fail
  outright on Windows; the backups are now numbered.
- **The identity provisions itself.** The first join creates `~/.vox/pki/ca.crt`
  and a 24-hour certificate whose SAN is the agent id. It is reissued once past
  half its life; short lives are the only practical revocation here.
- **Measured, not assumed.** Two real agents on one machine: VOX (PROCESSOR,
  `infer`) and a second agent declaring `ingest`. It appeared as NEW, completed
  the WHOIS over mTLS, was classified SOURCE and went ACTIVE within a second at
  a 2s announce interval; after the peer was killed the reaper moved it to
  SUSPECT at ~7s and DEAD at ~11s, exactly the 3 and 5 intervals the registry
  promises.
- **The keys are `F3` and `F4`, and no ctrl combination at all.**
  `Ctrl+Shift+O` and `Ctrl+Shift+U` were tried first and never arrived: most
  terminals do not tell `Ctrl+Shift+<letter>` from `Ctrl+<letter>`, and only
  those speaking the Kitty keyboard protocol, or Windows Terminal in
  win32-input mode, deliver them. `Ctrl+O` was tried next and fared no better
  on the same terminal. A function key reaches the application everywhere, so
  that is what the mesh uses; a test asserts no ctrl binding creeps back, and
  that nothing the input box owns is taken. `/mesh` and `/universe` remain the
  same code path.
- **The header says where you stand.** Its last field was a masked API key,
  which told nobody anything; it now reads `Universe: LOCAL` when VOX talks
  only to its own provider and `Universe: ON-LINE` while it is announcing
  itself to other agents. No key, masked or otherwise, is displayed anywhere —
  the masking helper is deleted, not merely unused.
- `cryptography` becomes a required dependency, and `vox doctor` gains a MESH
  line: the group and port, the agent id and its category, the certificate
  expiry, and the authority it trusts.
- The discovery package is vendored under `vox_chat/discovery/` with only its
  imports changed, and translated into English along with its own suite, which
  binds real sockets and therefore runs only under `VOX_TEST_MESH=1`. 31 new
  tests cover the layer VOX added, none of them touching the network.

---

## 0.1.0 — 2026-08-25

The first release: a terminal chat client for coding against any
OpenAI-compatible endpoint, with an optional coding agent, live measurement of
the output distribution, and saveable reports. Runs on Linux, macOS, Windows
and Termux, needs Python 3.11, and installs with one command.

### Linux, properly

**2026-08-25**

- **Python 3.11 is enough.** It was 3.12, which Debian 12 does not carry: its
  stock `python3` is 3.11, so the installer refused on a perfectly capable
  machine. Every module parses under 3.11 rules and no 3.12-only API is used —
  checked with `ast.parse(feature_version=(3, 11))` across all 27 modules, not
  by running the suite on 3.11.
- **The installer installs what is missing.** It detects the package manager
  (apt, dnf, pacman, zypper, apk, brew, pkg), and offers to add a Python, or
  the `python3-venv` that Debian and Ubuntu ship separately and without which
  `python3 -m venv` fails. It shows the exact command, asks before running
  anything with `sudo`, and says plainly what skipping costs — whether that is
  fatal (venv) or not (the clipboard helper).
- **A half-built environment repairs itself.** A failed first attempt can
  leave a virtual environment with no pip, and every later command then dies
  with `No module named pip`. The installer now checks for pip, restores it
  with `ensurepip`, and rebuilds the environment from scratch if that is not
  enough.
- **sudo never blocks.** With `--yes` and no terminal — a piped install, or
  CI — `sudo` would sit waiting for a password nobody could type, which looks
  exactly like a hang. It now checks with `sudo -n` first and says so instead.
- Verified on Ubuntu 24.04 through WSL: a full install into a temporary prefix,
  and a stubbed interpreter reproducing Debian's missing `venv` to check the
  diagnosis rather than assume it.
- A test keeps `install.sh` at LF endings: a CRLF copy dies on Debian with
  `set: Illegal option`, which is exactly what a Windows checkout can produce.

### Results follow the work

**2026-08-25**

- Exports and saved sessions are written **into the directory VOX was started
  in**, not into `~/.vox`: `vox-<timestamp>.{html,json,md}` and
  `vox-session-<name>.json`. `/workspace` moves both.
- The `vox-` prefix means one `.gitignore` line covers them, and a project's
  own `package.json` is never read as a session.
- What stays in `~/.vox` is what belongs to the operator rather than to a
  project: configuration, roles, prompts, input history and logs.

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
