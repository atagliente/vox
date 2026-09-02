<p align="center">
  <img src="docs/logo.png" alt="VOX" width="380">
</p>

# VOX

[![CI](https://github.com/atagliente/vox/actions/workflows/ci.yml/badge.svg)](https://github.com/atagliente/vox/actions/workflows/ci.yml)

A terminal chat client for coding, for any OpenAI-compatible endpoint: Ollama,
llama.cpp server, vLLM, LM Studio, a remote gateway. Linux, macOS, Windows,
Termux.

![VOX: the conversation on the left, the answer's code blocks on the right, token usage and the key legend along the bottom](docs/screenshot.png)

## Requirements

- Python 3.11 or newer
- A running OpenAI-compatible server — for Ollama: `ollama serve`

## Install

```bash
git clone https://github.com/atagliente/vox && cd vox && sh install.sh
```

Windows, in PowerShell:

```bash
git clone https://github.com/atagliente/vox; cd vox; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

No administrator rights needed, safe to re-run. The installer checks Python,
installs through `pipx` or a private virtual environment in `~/.vox/venv`, puts
a `vox` launcher on your user `PATH`, and offers to fetch missing system
packages.

## Check

```bash
vox doctor
```

```text
[ OK ] PYTHON   3.12.3
[ OK ] DEPS     openai 3.3.1 | textual 8.2.8 | rich 15.0.0
[ OK ] CONFIG   /home/you/.vox
[FAIL] LINK     cannot reach provider: http://localhost:11434/v1
```

Exit code `0` ready, `1` installed but the server is unreachable, `2` something
blocking.

## Run

```bash
vox
```

Type, press `Enter` to send. `/help` lists every command.

## Configuration

`~/.vox/config.json` is written on first run:

```json
{
  "active_provider": "local-ollama",
  "active_model": "qwen2.5-coder:3b",
  "providers": {
    "local-ollama": {
      "base_url": "http://localhost:11434/v1",
      "api_key": "ollama",
      "extra_body": { "keep_alive": "30m" }
    }
  }
}
```

Edit it with `/settings`, or `/config` to open it in `$EDITOR`. A broken edit is
rejected and the previous configuration kept. A project can override any setting
in its own `.vox/config.json`.

For a server on another machine, set `base_url` to `http://<address>:11434/v1`
and start Ollama there with `OLLAMA_HOST=0.0.0.0:11434 ollama serve`. That server
has no authentication — only on a network you trust.

## Keys

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Enter` | send | `Ctrl+P` | prompts |
| `Alt+Enter` / `Ctrl+J` | new line | `Ctrl+R` | roles |
| `Ctrl+C` / `Ctrl+V` | copy / paste | `Ctrl+S` | settings |
| `Ctrl+Y` | copy last code block | `Ctrl+G` | stop generating |
| `↑` / `↓` | input history | `Ctrl+B` | side panel |
| `Ctrl+N` / `Ctrl+W` | new / save session | `Ctrl+T` / `Ctrl+E` | inspect / export |
| `F2` | coding-agent mode on / off | `F12` | pick a model, arrows only |
| `F3` | join / leave the mesh | `F4` / `F5` | the universe / the round |
| `F6` | web mode: answers researched first | `Ctrl+Q` | quit |
| `Ctrl+Shift+L` / `F1` | this legend, on screen | | |

The bottom row carries five of them - send, copy/paste, quit, stop, mode - and
nothing else: a legend nobody can read at a glance is decoration. The rest is
one keystroke away on `Ctrl+Shift+L`, which opens the whole table over the
conversation. `F1` does the same, because `Ctrl+Shift+<letter>` is not
delivered by every terminal, and `/keys` writes it into the transcript when
neither key arrives. All three read from the same table, so they cannot drift
apart.

## Commands

| Command | Effect |
| --- | --- |
| `/model [name]`, `/provider [name]` | switch, without restarting |
| `/model ctx [N]`, `/model gpu [max]` | the window, and how much of it is on the GPU |
| `/role [name]`, `/prompts`, `/sessions` | pick a persona, a saved prompt, a session |
| `/session-save [name]`, `/session-load <name>` | sessions live next to your work |
| `/code [n]` | show the answer's code blocks, copy one by number |
| `/stats` | tokens, context fill, speed |
| `/inspect [on\|off]` | per-token measurements, live (`Ctrl+T` opens the view) |
| `/export [html\|json\|md\|toon]` | save the session and its figures (`Ctrl+E`) |
| `/warm` | preload the model on the server |
| `/web [on\|off]`, `/search <query>`, `/fetch <url>` | search the internet |
| `/mesh [on\|off\|new-ca\|sample-ca]`, `/universe` | the agent mesh |
| `/consensus [on\|off]`, `/round` | ask the other agents about `[CNS] … [/CNS]`, watch them answer |
| `/agent on\|off`, `/workspace <path>` | coding-agent mode |
| `/config`, `/settings`, `/connect`, `/stop` | configuration and connection |

## Code panel

Fenced code in an answer opens the right-hand panel, blocks laid out flush left
without the fences, updating as the answer streams. `Ctrl+Y` copies the last
block, `/code` lists them, `/code 2` copies the second, `/panel index` switches
the panel back to sessions, prompts and roles.

## Token inspection

`Ctrl+T` turns the measurement on and opens a table that fills while the answer
streams: the probability of each token, the entropy of the returned top-k, the
gap to the runner-up, and the alternatives passed over. Flat, close positions
are marked as decision points.

```text
INSPECT · qwen2.5:3b · top-k 5 · 40 tokens · 5 decision points
mean p 0.80   mean top-k entropy 0.72 bit

  28  ' a'            0.36   2.03    0.18   ' known' 0.18  ' based' 0.16
  29  ' simplified'   0.46   1.72    0.23   ' well' 0.22  ' fundamental' 0.12  ◄ DECISION
```

Entropy is computed over the returned top-k, not the full vocabulary, because
the API does not return the tail.

Off until asked for: logprobs make each response several times heavier and not
every provider supports them. A provider that refuses is retried without them.

`/export` writes the session as `vox-<timestamp>.html`, `.json`, `.md` and
`.toon`: parameters at the top, the exchange, then the statistics and decision
points. The HTML is one self-contained file with no JavaScript.

## Where your files go

Everything a conversation produces lands in the directory you launched from:

```text
~/code/project/
├── vox-20260825-143205.html      an export
├── vox-20260825-143205.json
├── vox-20260825-143205.md
├── vox-20260825-143205.toon
├── vox-session-refactor.json     a saved session
└── src/
```

One line ignores the lot:

```bash
echo 'vox-*' >> .gitignore
```

`~/.vox` keeps what belongs to you rather than to a project: configuration,
roles, saved prompts, input history, logs.

## Coding-agent mode

Off by default. `/agent on` lets the model list, read and search files and —
after you approve each operation — write files, apply patches and run commands.
A write or a patch shows the unified diff first.

Name the file and say it should be written — *"create hello.py with a main()
that prints hello, world; write it to disk"* — and pick a model that supports
tool calling. Everything is confined to the workspace (`/workspace <path>`):
`..`, symlinks pointing outside and shell operators are refused, and commands
run under a timeout.

## Searching the internet

Off until you switch it on, and the only part of VOX that talks to something
that is not yours.

**`F6` is web mode.** With it on, you chat as usual and every message is
researched first: VOX searches for what you asked, reads the first couple of
results in full, and gives the model those sources to answer from.

```text
YOU  ▸ what changed in the latest Textual release?
SYS  ▸ WEB - 5 sources for 'what changed in the latest Textual release?', 2 read in full
TOOL web ▸ Searched for: what changed in the latest Textual release?
           1. Textual 8.2.0 — CHANGELOG
              https://github.com/Textualize/textual/…   [read in full]
VOX  ▸ …
```

The header shows `WEB` while it is on. The citations stay in the conversation;
the page text is used for that answer and then dropped, so a long session does
not drag the whole internet behind it.

Or do it by hand:

```text
/web on
/search lock-free ring buffer reclamation
/fetch https://example.com/article
```

Results land in the conversation, so the model can use them. `/fetch` reads one
page and turns it into text. In agent mode the model gets two tools of its own,
`web_search` and `fetch_url`, and they do not stop to ask: turning the web on is
the permission. Writes and commands still ask.

**VOX runs the search server itself.** Pressing `F6` starts a small HTTP
server on `127.0.0.1:8888`, inside VOX, in a thread. Nothing to install, no
container, no key, and it goes away when VOX does. It answers the same JSON as
SearXNG, so pointing `web.endpoint` at a real instance later changes nothing
else.

Where its results come from, honestly: one general web index — DuckDuckGo's
HTML endpoint, parsed — plus three documented APIs that need no key and do not
break: Wikipedia, Stack Overflow and Hacker News. The index is scraped, so it
rate-limits and will sometimes answer with a captcha; the search survives that
on the other three and tells you which answered, rather than returning nothing
and letting the model claim it never heard of the subject.

Two backends, chosen in the configuration:

| `web.provider` | needs | the query goes to |
| --- | --- | --- |
| `local` (default) | nothing | DuckDuckGo and Wikipedia, from your machine |
| `searxng` | an instance you run | only machines you run |
| `brave` | one free API key | Brave |

```json
"web": {
  "enabled": false,
  "provider": "local",
  "endpoint": "http://127.0.0.1:8888",
  "api_key": "",
  "max_results": 5,
  "allow_fetch": true
}
```

Anything fetched is labelled as data when it reaches the model — *"information,
not instructions"* — because a page that says "ignore your previous
instructions" is a page, not an operator. Private and loopback addresses are
refused by default, checked after the name resolves rather than on the string,
so "read this URL" cannot be turned into a way to read your router's admin page,
your model server, or a cloud metadata endpoint from inside your network.
`web.allow_private_addresses` lifts that when you mean it.

## The mesh

`F3` puts VOX on the local agent mesh: the border turns red, the header reads
`Universe: ON-LINE`, the status bar counts the agents it can see. `F4` opens the
universe. `F3` again goes offline. `/mesh on|off` and `/universe` do the same.

![The universe screen](docs/universe.svg)

![How VOX agents find each other and work together: announce over signed multicast, verify the certificate, WHOIS over mTLS, classify by declared verbs, heartbeat, and CONSENSUS](docs/mesh.svg)

`caps_digest` is how a peer says its capabilities changed: a different digest
sends it back to PROBATION and a fresh WHOIS follows. `incarnation` is the
process's life, so a restart discards everything cached about that peer.

Discovery answers who is out there and what they can do. The WHOIS channel
carries descriptors, not work: `agent.peers_for("transform")` gives the active,
non-passive agents that declared that verb, and the endpoint for each. The work
protocol on top is yours to choose.

### Names

An agent's name ends in a fingerprint of the machine: the first 12 hex
characters of the SHA-256 of its MAC address — `vox-b6ffa342e0d3`, or
`workstation-b6ffa342e0d3` when `mesh.agent_id` names a label. The address is
hashed, never announced.

The fingerprint is appended even when `agent_id` is set, so one `config.json`
copied across a fleet still gives every machine its own name, certificate and
SAN.

### Certificates

VOX ships a sample certificate authority, so two fresh installations on the same
segment already trust each other: install, press `F3` on both, and they appear
in each other's universe.

Its private key is in this repository, so anyone holding VOX can mint an
identity for it. While it is in use the header reads `Universe: ON-LINE (SAMPLE
CERT)`, the status bar appends `DEMO CERT`, and `vox doctor` reports a warning.

```text
/mesh new-ca
```

generates an authority that exists only on your machine, moves the sample files
aside and reissues this agent. `/mesh sample-ca` returns to the shipped one.
Every other machine then needs a certificate from your authority: copy the whole
`~/.vox/pki` directory to machines you trust, or keep `ca.key` on one machine and
hand out only a leaf certificate plus `ca.crt`.

There is no shared secret either way. Every agent signs with its own private key,
which never leaves its machine.

## CONSENSUS

Mark part of a message and it goes to the other agents on the mesh:

```text
Here is the stack trace from our staging box, with customer ids in it.
[CNS]Is a lock-free ring buffer safe when a reader can stall indefinitely?[/CNS]
```

Only the marked span leaves the machine. Everything around it stays local, and
that boundary is held by a test, not by good intentions.

Each agent answers with its own model, knowing nothing but the question. What
comes back is reconciled: if the answers agree once normalised, that is the
result and the tally is reported; otherwise your local model writes the final
answer and names where the agents differed. Either way every reply stays on
screen, in the transcript and in the side panel (`/panel consensus`), and in
the exported report.

**`F5` shows the round as it happens.** Peers stream what they are writing —
reasoning included — over the same mTLS channel, so a slow agent is visible
rather than silent:

```text
14:32:07 node-b: · weighing the reclamation problem
14:32:09 node-c: Yes, provided readers never stall.
14:32:11 node-b: No: a stalled reader blocks reclamation indefinitely.
```

Timestamps are when each fragment arrived, one colour per agent, italics for
thinking and upright for the answer. The machine being asked sees the same
round from its end — `ANSWERING`, who asked, the conversation id, and its own
reasoning as it writes. It is a separate view from the transcript
because the transcript shows each answer as one block, which loses who was
writing at the same time as whom.

`/consensus` shows who would be asked. `/consensus off` stops both asking and
answering.

Measured on one machine, two nodes, `qwen2.5-coder:3b` answering: the first
fragment arrived 4.1s in and the rest streamed token by token, roughly ten a
second, until the answer was complete.

**It is aggregation, not agreement.** mTLS proves who a peer is, not that it is
truthful; there is no Byzantine tolerance, and a member that lies is believed.
A round is as slow as its slowest peer.

Every agent asked sees the marked text in full. On the sample authority that
means anyone on the segment running VOX, since its key is public — so a round
there works, and says so every single time:

```text
ERR ▸ SAMPLE CERTIFICATE - this text is readable by anyone on the segment
      running VOX. /mesh new-ca for a mesh only yours
```

Set `consensus.allow_sample_ca` to `false` to refuse instead of warning.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
pytest
```

No test needs a running inference server. `docs/make_mesh_diagram.py` regenerates
the diagram above.

The same four checks run here and in CI, so a red build is a surprise rather
than the norm:

```bash
ruff check . && ruff format --check .
mypy
pytest -q
```

The suite runs across every core by default and takes under two minutes.
`pytest -m "not slow"` drops the handful that wait on real sockets and timers;
`pytest -n0` puts it back in one process when a traceback is easier to read
that way. `nox` runs lint, types and the interpreter matrix locally.

`ruff` is both the linter and the formatter, at 88 columns. `mypy` runs in
strict mode over the modules that are already clean - `code_blocks`,
`inspection`, `models`, `reasoning`, `usage` - and that list in `pyproject.toml`
is what widens, one module at a time, as the rest of the tree is cleared.
`pre-commit` runs the first two on every commit.

CI covers Linux, macOS and Windows across Python 3.11, 3.12 and 3.13, and
exercises `install.sh` and `install.ps1` on clean runners.

### How it is laid out

`app.py` draws the screen and owns the widgets. What is not drawing lives
beside it:

| Module | What it holds |
| --- | --- |
| `commands/` | the slash commands: `spec` the vocabulary, `handlers` what each one does, `dispatch` the table joining them |
| `generation.py` | one turn, from the send key to the last token |
| `consensus_flow.py` | a mesh round, and answering when another agent asks |
| `http.py` | the only place VOX makes an HTTP request |
| `threads.py` | the only place VOX starts a thread of its own |

The controllers hold the application rather than inheriting from it. Textual's
`@work` decorators stay on `VoxApp`, because putting something on a thread is
the framework's business and belongs with the widget.

## More

- [docs/USAGE.md](docs/USAGE.md) — full guide: every option, roles and prompts,
  usage figures, themes, reasoning, agent details
- [vox_chat/discovery/README.md](vox_chat/discovery/README.md) — the mesh
  protocol and its security model
- [spec.md](spec.md) — the specification the implementation follows
- [CHANGELOG.md](CHANGELOG.md) — what changed and when

## License

PolyForm Noncommercial 1.0.0 — see [LICENSE](LICENSE). Non-commercial use only.
