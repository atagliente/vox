# VOX — specification

VOX is a terminal chat client for coding work, built on Python 3.12, `textual`,
`rich` and the `openai` package. It talks to any OpenAI-compatible endpoint
(Ollama, llama.cpp server, vLLM, LM Studio, a remote gateway) and is meant to
run identically on Linux, macOS, Windows and Termux.

This document is the contract the implementation follows. It is the reference
for anyone extending the project.

## Goals

- Terminal UI with `textual` and `rich`.
- Token-by-token streaming with no duplicated fragments.
- Persistent, layered configuration.
- Configurable providers and models, switchable without a restart.
- Configurable agent roles.
- Reusable, persistent prompts.
- Saveable and reloadable chat sessions.
- Live token accounting: context fill, tokens spent, tokens per second and
  session averages.
- Settings editable both inside the TUI and by opening the JSON file in the
  user's editor.
- Optional coding-agent mode with local tools and explicit confirmation before
  any operation that writes files or runs commands.
- Installable as a system command through a preflight installer.

## Runtime dependencies

Only these, unless there is a well-argued reason:

- `openai`
- `textual`
- `rich`

Configuration and persistence use `json`, `pathlib` and the standard library.
`pytest` is the only development dependency.

## Project layout

```text
vox/
├── pyproject.toml
├── README.md
├── install.sh
├── install.ps1
├── vox_chat/
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   ├── agent.py
│   ├── commands.py
│   ├── config.py
│   ├── doctor.py
│   ├── llm_client.py
│   ├── logging_setup.py
│   ├── models.py
│   ├── prompts.py
│   ├── roles.py
│   ├── sessions.py
│   ├── storage.py
│   ├── tools.py
│   ├── usage.py
│   └── ui/
│       ├── __init__.py
│       ├── branding.py
│       ├── modals.py
│       └── widgets.py
└── tests/
```

The application starts with `vox` after installation, or with
`python -m vox_chat` from a checkout.

## Terminal UI

The main screen contains:

1. a top bar with the application name, and the active provider, model and role;
2. a scrollable central area where user, assistant, system, tool and error
   messages are visually distinct;
3. a multiline input field at the bottom;
4. a status bar with connection state, generation state and usage figures,
   above a permanent key legend;
5. a side panel with two modes: the code blocks of the latest answer, laid out
   for copying, and an index of sessions, saved prompts and roles;
6. a modal window for settings;
7. a modal confirmation window for writes and command execution, focused on
   the cancel button, navigable with the arrows and carrying a legend of the
   keys it accepts.

Minimum shortcuts:

- `Enter` — send the message (`Ctrl+Enter` stays bound for terminals that
  deliver it, but most do not)
- `Alt+Enter`, `Ctrl+J`, `Shift+Enter` — insert a new line
- `↑` / `↓` — walk the input history, at the first and last line of the input
- `Ctrl+N` — new session
- `Ctrl+W` — save the session
- `Ctrl+P` — saved prompts
- `Ctrl+R` — role picker
- `Ctrl+S` — settings (`Ctrl+,` is kept as an alias)
- `Ctrl+G` — stop the current generation
- `Ctrl+B` — toggle the side panel
- `Ctrl+Y` — copy the last code block of the answer
- `Ctrl+T` — open the inspection view
- `Ctrl+E` — export the session
- `Ctrl+C`, `Ctrl+Shift+C` — copy the selection, or the last answer
- `Ctrl+V`, `Ctrl+Shift+V` — paste the system clipboard
- `Ctrl+Y` — copy the last code block of the answer
- `Ctrl+T` — open the inspection view
- `Ctrl+E` — export the session
- `Ctrl+Q` — quit, asking for confirmation when there are unsaved changes; it
  is the only key that quits

A key legend occupies the bottom row at all times, dropping entries from the
right when the terminal is too narrow. Submitted lines, messages and slash
commands alike, are appended to `~/.vox/history.json`, capped at 200 entries.

The interface must stay responsive while streaming: no blocking HTTP request
ever runs on the UI thread. Every modal flow runs inside a worker, because
`push_screen_wait` suspends until the screen is dismissed and Textual refuses
that on the main task.

While a request is in flight the transcript shows an animated placeholder
(`WAITING FOR MODEL`, `RUNNING <tool>`) with the elapsed time, and the status
bar spins next to `GENERATING`. Both are driven by their own UI timer, so they
keep moving while the worker thread is blocked on the provider.

### Visual identity

The default look is 1970s mission control: muted, low-saturation colours —
bone white text, amber labels, sage system notes, terracotta errors — on a warm
charcoal panel, with thin rules and no neon.

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ VOX 0.1.0  ·  LINK ONLINE  http://localhost:11434/v1                       │
│ PROVIDER local-ollama  ·  MODEL qwen2.5-coder:3b  ·  ROLE python-developer │
└────────────────────────────────────────────────────────────────────────────┘
```

The header is sized from the real terminal width: the least important field is
truncated first and the box is dropped below 40 columns, so it can never wrap.
The boot banner never repeats the header frame.

`ui.theme` accepts `nasa` (default), `dark` and `light`; `ui.logo` accepts
`frame`, `bar` and `none`. The `wopr` and `norad` names written by earlier
versions remain valid.

## Slash commands

```text
/help                    /sessions
/new                     /session-save [name]
/clear                   /session-load <name>
/settings                /session-delete <name>
/config                  /agent on|off
/provider [name]         /workspace <path>
/model [name]            /connect
/role [name]             /stop
/roles                   /exit
/prompts                 /stats
                         /warm
/prompt <name>
/prompt-save <name>
/prompt-delete <name>
```

Requirements:

- `/config` opens the configuration file in `$VISUAL` or `$EDITOR`, falling
  back to `notepad` on Windows.
- On returning from the editor the JSON is validated and reloaded.
- If the JSON is invalid the previous configuration is kept and an error with
  line and column is shown.
- Unknown commands produce suggestions.
- A message that legitimately starts with a slash is escaped as `//`.

## Configuration

Two layers:

1. global: `~/.vox/config.json`
2. project: `.vox/config.json` in the current workspace

The project file overrides only the keys it declares, through a predictable
recursive merge. On first run a valid global file is created:

```json
{
  "active_provider": "local-ollama",
  "active_model": "qwen2.5-coder:3b",
  "active_role": "python-developer",
  "providers": {
    "local-ollama": {
      "base_url": "http://localhost:11434/v1",
      "api_key": "ollama",
      "timeout_seconds": 600,
      "models": ["qwen2.5-coder:1.5b", "qwen2.5-coder:3b"]
    }
  },
  "generation": {
    "temperature": 0.2,
    "max_tokens": 1800,
    "context_window": 8192,
    "include_usage": true
  },
  "agent": {
    "enabled": false,
    "confirm_writes": true,
    "confirm_commands": true,
    "command_timeout_seconds": 60,
    "max_tool_cycles": 8,
    "max_output_bytes": 8192
  },
  "ui": {
    "theme": "nasa",
    "show_timestamps": true,
    "show_usage": true,
    "logo": "frame",
    "splash": true
  }
}
```

API keys are never displayed in the TUI after loading, and never written to the
log.

## Providers and models

- Several providers can live in the same file.
- Each provider has a base URL, an API key, a timeout and a model list.
- Provider and model can change without a restart.
- Connectivity is checked non-destructively through `/v1/models`.
- When the server does not answer, a readable error is shown and the TUI stays
  usable.
- `APIConnectionError`, `APITimeoutError`, HTTP errors and deliberate
  cancellation are all handled.
- Streaming fragments are accumulated into a single assistant message.
- Reasoning and tool calling are not assumed to be available everywhere.
- A provider may carry an `extra_body` object, merged into every request, for
  provider-specific parameters such as Ollama `keep_alive`.
- The active model is preloaded in the background after connecting and after a
  model change (`generation.preload`, also available as `/warm`), so the cost
  of loading a cold model is paid while the operator is typing.

## Reasoning

Thinking is displayed in its own block, never mixed into the answer. It is
read from a dedicated delta field (`reasoning_content`, `reasoning`,
`thinking`) or extracted from inline `<think>…</think>` tags, including tags
split across streaming chunks. It is persisted with the session and shown
again when the session is reloaded, but it is never sent back to the model.
`ui.show_reasoning` hides it.

## Usage accounting

The status bar shows, live during streaming and after each turn: how full the
context window is, the tokens spent, the current speed in tokens per second and
the session average. `/stats` prints the full breakdown, including peak speed
and first-token latency.

Speed is measured from the first token onwards: the wait for a cold model is
reported as first-token latency, never folded into tokens per second.

Exact counts are requested through `stream_options.include_usage`; a provider
that rejects the option is retried once without it, and the figures fall back
to a character-based estimate that is flagged as approximate. The window the
percentage is measured against is `generation.context_window`, because the API
does not report it. `ui.show_usage` hides the readout.

## Shutdown

Quitting cancels the running generation, closes the client and stops every
timer. A worker thread blocked on a provider that never answers must not keep
the process alive: after a short grace period the process leaves regardless,
which is safe because all state is persisted as it changes.

## Clipboard

Copy and paste reach the real system clipboard through the platform's own
helper (PowerShell, `pbcopy`/`pbpaste`, `wl-copy`, `xclip`, `xsel`,
`termux-clipboard-*`), run without a shell and under a timeout, in a worker so
the UI never blocks. When no helper exists the key reports the failure instead
of doing nothing, and `vox doctor` lists what is available.

## Token inspection

Optional and off by default (`inspect.enabled`). When on, the request carries
`logprobs` with `top_logprobs` (1 to 20; the endpoint refuses more), and the
returned distribution is measured per token: probability, entropy over the
returned top-k in bits, and the margin to the runner-up. A full-screen view
fills while the answer streams, filterable to decision points, thinking or
answer.

A decision point is a position with entropy at or above the threshold, margin
at or below it, that is not punctuation and is at least `min_distance` tokens
after the previous one. All four are configuration and are reported alongside
the numbers they produced.

The phase of a token is taken from the delta its logprob arrived with; a chunk
carrying a logprob but no text continues the phase already open. Only tokens
arriving before any phase is established are unattributed.

These are measurements of the output distribution. Nothing in the view or the
report may describe a spread distribution as the model thinking, hesitating or
deciding; the entropy figure must always be labelled as top-k, because the API
does not return the tail of the vocabulary. Attention, activations and any
other model internals are out of scope, as is regenerating from a decision
point with a forced alternative.

A provider that rejects logprobs is retried once without them and reported
once. Inspection must never change the outcome of a chat.

## Reports

`/export` writes the session into the directory VOX was started in, as
`vox-<timestamp>` in HTML, JSON and Markdown. Results belong to the work, not
to a hidden folder in the home; the `vox-` prefix keeps them one glob away from
being ignored.
Each opens with the question, model, provider, endpoint, role and the
parameters actually sent, followed by the exchange with thinking kept separate,
then the statistics and the decision points, then provenance. The HTML is
self-contained and carries no JavaScript, so it reads with scripting disabled.
The JSON is a single documented schema. A session with inspection off still
exports, and says so.

## Roles

Persisted in `~/.vox/roles.json`. Each role holds a name, a description, a
system prompt, a temperature and an `agent_enabled` flag. The initial roles are
`general-assistant`, `python-developer`, `code-reviewer`, `embedded-developer`
and `debugging-assistant`. Roles can be created, edited, duplicated, selected
and deleted; deletion asks for confirmation. The active role supplies the
`system` message.

## Saved prompts

Persisted in `~/.vox/prompts.json`. Each prompt has a name, a description,
content, tags and a modification date. Prompts can be saved from the current
editor content, opened into the editor without being sent, edited, renamed,
duplicated, deleted and searched by name or tag. Templates use simple
`{{workspace}}`, `{{file}}` and `{{selection}}` variables; unresolved variables
are reported before sending. Substitution never uses `eval()`.

## Sessions

Each session is a JSON file named `vox-session-<name>.json` in the directory
VOX was started in holding its id and title,
creation and modification dates, provider, model and role, the associated
workspace, the full message history including tool calls and their results, and
the agent-mode state. Writes are atomic (temporary file, then replace).
Corrupt files are reported but never prevent startup.

## Coding-agent mode

Disabled by default. When enabled, these tools are exposed to the model:

- `list_files(path)`
- `read_file(path, start_line, end_line)`
- `search_text(query, path)`
- `write_file(path, content)`
- `apply_patch(patch)`
- `run_command(command, cwd)`

Mandatory safety rules:

- every path is resolved and confined to the configured workspace;
- `..` traversal and symlinks leaving the workspace are blocked;
- reads are allowed only inside the workspace;
- writes and patches require explicit user confirmation, shown as the unified
  diff of the change, with the patch applied in memory first so one that does
  not fit is reported before it is offered for approval;
- every command requires confirmation and shows the command and its directory;
- commands run without a shell and under a timeout;
- stdout, stderr and the exit code are captured;
- results sent back to the model are size-limited;
- there is no confirmation bypass anywhere in the code;
- if the model does not support tool calling, agent mode is disabled
  gracefully and the user is told.

A configurable limit on tool cycles per request (default 8) prevents loops.

## Agent mesh

VOX can join a peer-to-peer mesh of agents on the local network segment. It is
off until asked: nothing is announced before the operator presses the key.

- `F3` (`/mesh on|off`) joins or leaves. Joining writes the agent id,
  the category, the group and the port to the transcript — announcing presence
  on a network is not something to do silently — and names the two files a
  second machine needs.
- While online the screen carries the `mesh-online` class: a red border, in
  every theme, and `MESH ONLINE · n agents, n active` in the status bar. The
  header's last field reads `Universe: LOCAL` or `Universe: ON-LINE`
  accordingly; no API key, masked or otherwise, is ever shown on screen.
- `F4` (`/universe`) opens a live table of every agent seen, with its
  category, state, address, age and verbs. `Ctrl+L` shows a legend.
- The mesh uses function keys and no ctrl combination: `Ctrl+Shift+<letter>`
  and `Ctrl+O` were both tried on a real terminal and never arrived. The slash
  commands are always the fallback.

The protocol lives in `vox_chat/discovery/`, vendored with only its imports
changed: multicast announcements (239.17.42.1:45177, TTL 1) signed per agent
with Ed25519 and carrying the signer's certificate, a WHOIS handshake over mTLS
where the certificate SAN must equal the announced agent id, a category derived
deterministically from the declared verbs, and a registry with PROBATION →
ACTIVE → SUSPECT → DEAD.

VOX ships a sample certificate authority in `vox_chat/demo_pki/`, copied into
the PKI directory on first use so that two fresh installations interoperate
without provisioning. Its private key is public by construction, so its use is
declared everywhere it matters: `Universe: ON-LINE (SAMPLE CERT)` in the header,
`DEMO CERT` in the status bar, a line in the transcript on going online, and a
`WARN` from `vox doctor`. `mesh.demo_ca` is true by default and says which authority to be on, so a
machine holding another one is moved to the sample when it goes online.
`/mesh new-ca` replaces it with an authority private to the machine — moving
the sample aside, reissuing this agent, and setting the flag false so the next
start does not undo it — and `/mesh sample-ca` goes back.

There is no shared secret anywhere in the protocol. A receiver validates the
certificate in the packet against its own CA, requires the announced agent id
to be a SAN of that certificate, and then checks the signature — so a member
cannot announce itself as another member, and a stranger's packet is dropped
before it reaches the registry.

The agent id always ends in a fingerprint of the machine — the first 12 hex
characters of the SHA-256 of its MAC address — appended to the label from
`mesh.agent_id`, or to `vox` when that is empty. The identity may not be
dictated by the configuration: one file copied across a fleet must still yield
one name, one certificate and one SAN per machine. The MAC is hashed and never
announced; with no hardware MAC the fingerprint falls back to a hash of user
and host.

`vox_chat/mesh.py` is the only part the interface talks to. It owns the
identity (`~/.vox/pki`, a 24h certificate reissued past half life) and the
agent's life. Start and stop happen in a worker thread, because socket setup
and certificate generation block; a failure is reported to the transcript and
leaves the chat untouched.

## CONSENSUS

`[CNS] … [/CNS]` marks the part of a message that may leave the machine. The
tag is the privacy boundary: everything outside it stays local, the parser is
pure and lives in `vox_chat/consensus.py`, and a test asserts that nothing but
the marked span reaches the network. An unclosed tag distributes nothing.

The marked span is sent to the peers that declare the configured verb (`infer`
by default, so the PROCESSORs; OBSERVERs are never asked) over the mTLS channel
WHOIS already uses, as an `ASK` operation. A peer answers with its own model in
a fresh two-message conversation, so no context travels in either direction. A
node answers one question at a time and refuses a second with `busy`.

A peer streams what it writes — reasoning and answer, tagged — as
newline-delimited JSON on that same connection before the final reply, so the
asker can watch rather than wait. No websocket is involved: the channel is
already an authenticated bidirectional stream. `F5` (`/round`) shows those
fragments in arrival order, timestamped and coloured per agent, which the
transcript cannot do because it groups each answer into one block. The caller
caps how much a peer may stream.

The replies are reconciled, not agreed. Answers that match once normalised form
a vote when they reach both the quorum and a strict majority; otherwise the
local model synthesises a final answer and is told to name the disagreements.
Every reply is kept in the transcript, the side panel, the session and the
report: a verdict that cannot be checked is not worth having.

Distribution is refused, with the remedy named, when consensus is off, the mesh
is offline, the marked text is oversize, or a tag is left unclosed.

On the sample authority a round proceeds and warns first, every time: its
private key is public, so anyone on the segment running VOX can join and read
what is distributed, and the peer list shown is not the same thing as who can
read it. `consensus.allow_sample_ca` (true by default) turns that warning back
into a refusal, for asking and answering alike.

## Quality rules

- Type hints on internal APIs; `dataclasses` where they add clarity.
- UI, configuration, persistence, LLM client and tools stay separated.
- No global singletons or uncontrolled shared state.
- The OpenAI client is constructed in exactly one place.
- No `sys.exit()` deep in the logic: errors are returned to the TUI.
- No `except Exception: pass`.
- Rotating logs, without full prompts or secrets.
- Every file is UTF-8; multiline and Unicode messages work.
- Plain chat works with agent mode off.
- Code returned by the model is never extracted or executed automatically.

## Required tests

Automated tests cover at least: default configuration creation and validation;
global/project merge; rejection of invalid JSON without losing the previous
configuration; role CRUD; prompt CRUD and safe variable substitution; atomic
session save and load; workspace path confinement; rejection of `../` and
external symlinks; command timeout and result rendering; streaming fragment
assembly; slash command parsing; mesh settings, identity provisioning and the
controller against a stubbed discovery agent. No test requires a real inference
server or a real socket: fakes and mocks are used throughout. The vendored
discovery suite binds real multicast and TLS sockets, so it runs only under
`VOX_TEST_MESH=1`.

## Installation

A preflight installer makes `vox` a system command without administrator
rights: `install.sh` for Linux, macOS and Termux, `install.ps1` for Windows.
Each one verifies Python 3.12+, installs through pipx or a dedicated virtual
environment, puts a launcher on the user PATH and ends with `vox doctor`, which
reports the interpreter, the dependencies, the configuration directory, the
editor, the provider link and the active model.
