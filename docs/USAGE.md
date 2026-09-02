# VOX — full guide

Everything beyond the quick start in the [README](../README.md): installation
options, configuration, keys, commands, usage figures, appearance, reasoning
and coding-agent mode.

## Install

The installers need no administrator rights and are safe to re-run.

**Linux, macOS, Termux**

```bash
git clone https://github.com/atagliente/vox && cd vox && sh install.sh
```

**Windows (PowerShell)**

```bash
git clone https://github.com/atagliente/vox; cd vox; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Both scripts verify Python 3.12+, install through `pipx` when available (a
dedicated virtual environment in `~/.vox/venv` otherwise), put a `vox` launcher
on your user `PATH`, and finish by running the system check.

Useful flags: `--yes` / `-Yes` (non-interactive), `--no-path` / `-NoPath`
(leave `PATH` alone), `--uninstall` / `-Uninstall` (remove the command; your
settings and sessions in `~/.vox` are kept).

### Manual install

```bash
python3.12 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
vox                            # or: python -m vox_chat
```

## Configure a provider

On first run VOX writes `~/.vox/config.json` pointing at a local Ollama:

```json
{
  "active_provider": "local-ollama",
  "active_model": "qwen2.5-coder:3b",
  "providers": {
    "local-ollama": {
      "base_url": "http://localhost:11434/v1",
      "api_key": "ollama",
      "timeout_seconds": 600,
      "models": ["qwen2.5-coder:1.5b", "qwen2.5-coder:3b"]
    }
  }
}
```

Edit it from inside the TUI with `/settings`, or in your own editor with
`/config` — the file is validated on return and the previous configuration is
kept if the JSON is broken.

A project can override any subset of these keys in `.vox/config.json`; only the
keys it declares are replaced.

### Pointing at a server on your network

Set `base_url` to that machine's address, for example
`http://192.168.1.50:11434/v1`. For Ollama, the server must be told to listen
beyond loopback:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Check the endpoint before starting VOX:

```bash
curl http://192.168.1.50:11434/v1/models
```

> **Security.** An Ollama server bound to `0.0.0.0` has no authentication:
> anyone on the network can use the machine's models, read the prompts they
> send and load or delete models. Only do this on a network you trust, keep it
> off the public internet, and prefer an SSH tunnel or a reverse proxy with
> authentication if you need remote access. The `api_key` in the config exists
> for gateways that require one; a local Ollama ignores it.

## Preflight check

```bash
vox doctor
```

```text
VOX SYSTEM CHECK  -  v0.1.0  -  Linux
=====================================
[ OK ] PYTHON   3.12.5
[ OK ] DEPS     openai 2.41.1 | textual 8.2.8 | rich 15.0.0
[ OK ] CONFIG   /home/you/.vox
[ OK ] EDITOR   nano (EDITOR)
[FAIL] LINK     http://localhost:11434/v1 - cannot reach provider
[ -- ] MODEL    qwen2.5-coder:3b - not verified
```

Exit code `0` means everything is ready, `1` means VOX works but the provider
is unreachable, `2` means something blocking is wrong.

## Using it

```bash
vox                        # start in the current directory
vox -w ~/code/project      # set the agent workspace
vox -m qwen2.5-coder:1.5b  # override the model for this run (add --save to keep it)
vox --no-splash            # skip the boot banner
python -m vox_chat         # same app, from a checkout
```

### Keys

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Enter` | send | `Ctrl+P` | prompts |
| `Alt+Enter` / `Ctrl+J` | new line | `Ctrl+R` | roles |
| `Ctrl+C` / `Ctrl+V` | copy / paste | `Ctrl+S` | settings |
| `Ctrl+Y` | copy last code block | `Ctrl+G` | stop generating |
| `↑` / `↓` | input history | `Ctrl+B` | side panel |
| `Ctrl+N` / `Ctrl+W` | new / save session | `Ctrl+T` / `Ctrl+E` | inspect / export |
| `F3` | join / leave the mesh | `F4` / `F5` | the universe / the round |
| | | `Ctrl+Q` | quit |

`Enter` sends because `Ctrl+Enter` is not delivered by most terminals; it stays
bound for the ones that do. The arrows only reach the history at the first and
last line of the input, so they still move the cursor inside a multiline draft.
History lives in `~/.vox/history.json` (last 200 entries) and survives
restarts. A key legend is always visible on the bottom row.

### Commands

`/help` lists everything. The essentials:

| Command | Effect |
| --- | --- |
| `/provider [name]`, `/model [name]` | switch provider or model, no restart |
| `/role [name]`, `/roles` | choose the persona driving the system prompt |
| `/prompts`, `/prompt <name>` | load a saved prompt into the input, unsent |
| `/prompt-save <name>` | store the current input as a reusable prompt |
| `/sessions`, `/session-save [name]`, `/session-load <name>` | manage sessions |
| `/config`, `/settings` | edit configuration in your editor or in the TUI |
| `/agent on\|off`, `/workspace <path>` | control coding-agent mode |
| `/stats` | token usage, context fill and speed for the session |
| `/warm` | preload the active model on the server |
| `/mesh [on\|off\|new-ca\|sample-ca]`, `/universe` | join the mesh, see who is on it, swap the authority |
| `/consensus [on\|off]`, `/round` | ask the other agents, watch them answer |
| `/connect`, `/stop`, `/new`, `/clear`, `/exit` | connection and session control |

Send a message that really starts with a slash by prefixing it with `//`.

### Roles and prompts

Roles live in `~/.vox/roles.json` and decide the system prompt and temperature:
`general-assistant`, `python-developer`, `code-reviewer`, `embedded-developer`,
`debugging-assistant`. Prompts live in `~/.vox/prompts.json` and support
`{{workspace}}`, `{{file}}` and `{{selection}}` placeholders; unresolved
placeholders are reported before you send. Substitution is plain text
replacement — never `eval`.

Sessions are JSON files in `~/.vox/sessions/`, written atomically, carrying the
full history including tool calls.

### Usage and speed

The status bar keeps a live readout while the answer streams and after it
lands:

```text
LINK ONLINE  ·  GENERATING…  ·  MODE CHAT  ·  WS /home/you/code
local-ollama/qwen2.5-coder:3b [python-developer]  ·  CTX 1.9k/8.2k 23%  ·  412 tok  27.4 tok/s  ·  avg 25.8 tok/s
```

While a turn is in flight a braille spinner sits in the transcript — `WAITING
FOR MODEL… 2.4s`, or `RUNNING read_file…` during a tool call — and another
spins next to `GENERATING` in the status bar. The transcript spinner is
replaced by the answer as soon as the first token lands.

`/stats` prints the full breakdown: turns, prompt and completion tokens,
tokens per turn, wall time against time actually generating, average and peak
speed, the last turn's first-token latency, and how much of the context window
the last request filled.

Speed is measured from the first token onwards, so a cold model that takes
half a minute to load is not reported as generating at 0.1 tok/s — the wait
shows up as first-token latency instead.

VOX asks the provider for exact counts through
`stream_options.include_usage`; providers that reject the option are retried
without it and the figures fall back to a character-based estimate, marked
`~est` in the status bar. Two settings control this:

```json
"generation": { "context_window": 8192, "include_usage": true },
"ui":         { "show_usage": true }
```

`context_window` is what the percentage is measured against — set it to the
window your model actually serves, since the API does not report it.

### Appearance

The default theme, `nasa`, is deliberately low-contrast and low-saturation:
bone white text, muted amber for labels, sage for system notes, terracotta for
errors, on a warm charcoal panel. `dark` and `light` are neutral alternatives.

```json
"ui": { "theme": "nasa", "logo": "frame", "show_timestamps": true }
```

`logo` picks the header style: `frame` (thin box), `bar` (two plain rows) or
`none` (a single compact line). The header measures itself against the real
terminal width, truncating the least important field first and dropping the
box below 40 columns, so it never wraps. The older `wopr` and `norad` values
from earlier configs are still accepted.

### Reasoning

Models that think out loud get their own dim `THINK` block above the answer,
whether the thinking arrives in a dedicated field (`reasoning_content`,
`reasoning`, `thinking`) or inlined as `<think>…</think>` in the content —
including when a tag is split across two streaming chunks. Reasoning is saved
with the session but never replayed to the model. Turn it off with
`"ui": { "show_reasoning": false }`.

### Slow first token

A local model that is not resident in memory has to be loaded before it can
answer, and on modest hardware that can take minutes. VOX handles it in three
ways:

- **Preloading.** After connecting, and after a model change, it asks the
  server for a single token in the background, so the load happens while you
  are still typing. `/warm` does it on demand; `"generation": {"preload":
  false}` turns it off. While it waits, a spinner names the model *and the
  endpoint* and counts the seconds, the status bar reads PRELOADING rather
  than IDLE, and `Ctrl+G` stops waiting. It gives up after
  `generation.preload_timeout_seconds` (180 by default) and says so, instead
  of sitting on the ten-minute provider timeout with nothing on screen.
- **Keeping the model resident.** Anything in a provider's `extra_body` is
  sent with every request, so for Ollama you can stop it unloading between
  messages:

  ```json
  "extra_body": { "keep_alive": "30m" }
  ```

- **Saying so.** The spinner shows the elapsed seconds and, past ten, adds
  that the server may still be loading the model. `/stats` separates the wait
  from the actual generation time.

### Leaving

`Ctrl+Q` asks for confirmation if the session has unsaved messages, then
cancels whatever is running, closes the connection pool, stops the timers and
exits.

One case needs more than that. Textual runs its thread workers on a pool whose
threads are joined when the interpreter exits, so a request the provider never
answers — a cold model that takes minutes to load, say — would keep the process
alive long after the screen is gone: the terminal comes back but the shell does
not. VOX waits a second and a half for such a worker to finish and then leaves
anyway. Nothing is lost, because configuration, sessions, prompts, roles and
history are written as they change, not at exit.

### Copy and paste

`Ctrl+C` copies and `Ctrl+V` pastes, against the **system** clipboard, exactly
as everywhere else. With nothing selected, `Ctrl+C` copies the last answer.
`Ctrl+Shift+C` and `Ctrl+Shift+V` do the same, for terminals that reserve the
plain combinations. Both follow the focus, so they work inside the settings
modal too. Quitting is `Ctrl+Q`, stopping a generation is `Ctrl+G`, settings are `Ctrl+S`
(`Ctrl+,` still works) and a session is saved with `Ctrl+W`.

VOX talks to the real clipboard through whatever helper the platform provides
— PowerShell `Set-Clipboard` / `Get-Clipboard` on Windows, `pbcopy` / `pbpaste`
on macOS, `wl-copy`, `xclip` or `xsel` on Linux, `termux-clipboard-*` on
Termux. It never goes through a shell, gives up after five seconds, and says
which helper it used. `vox doctor` reports what is available, so on a bare
Linux box a missing `xclip` shows up as a warning rather than as a key that
silently does nothing.

Your terminal's own shortcuts keep working too: if it handles `Ctrl+Shift+V`
itself it will paste before VOX ever sees the key, which is fine — the text
arrives as a paste event either way. To select text with the mouse for the
terminal's own copy, hold `Shift` while dragging, since the app itself is
listening for mouse events.

### The code panel

Every fenced block in the latest answer is extracted — ` ``` ` and `~~~`
fences, with or without a language tag, indented or not, and the one still
being streamed is shown too, marked incomplete. The panel opens by itself the
first time an answer contains code (`"ui": {"code_panel": false}` stops that)
and widens to make room. It follows the answer as it streams, so on a slow
model you watch the new code arrive instead of staring at the previous
answer's; a block whose closing fence has not arrived yet is shown and marked
incomplete. An answer with no code empties the panel rather than leaving stale
code behind.

The code is rendered flush left with no prefix and no fences, so a terminal
selection over it yields the code and nothing else. `Ctrl+Y` copies the last
block, `/code` lists what is available, `/code <n>` copies that one, and
`/panel index` / `/panel code` switch the panel between the code and the
sessions-prompts-roles index.

### Getting the model to create files

Three things have to be true before a model can touch your disk:

1. agent mode is on — `/agent on`;
2. the workspace is the directory you mean — `/workspace ~/code/project`,
   shown in the status bar;
3. the model supports tool calling. Small models often ignore the tools and
   just print code; VOX says so and disables agent mode for that turn.

Then ask in plain language, but **name the file and say it should be written**.
Measured against a local Ollama, "fammi un hello world" left the smallest model
chatting about it, while this phrasing made every model call `write_file`:

> Crea il file hello.py con una funzione main() che stampa 'hello, world'.
> Scrivilo su disco.

Useful patterns:

- create: *"Create src/config.py with a load_config function that reads
  config.json. Write it to disk."*
- modify: *"Read src/main.py, then apply a patch that adds a --verbose flag."*
  Reading first matters: a patch built on a guess will not apply.
- several files: ask for them one at a time; you approve each write
  separately anyway.

Each write, patch or command opens the confirmation dialog with the diff, so
you decide what actually happens.

### Inspecting the tokens

`Ctrl+T` — or `F2`, or `/inspect` with no argument — turns the measurement on
if it was off and opens a full-screen table
that fills while the answer streams and stays open afterwards. Pressing the
same key again closes it.

It is `Ctrl+T` rather than the more obvious `Ctrl+I` because terminals send the
same byte for `Ctrl+I` as for `Tab`: Textual reports it as `tab`, so a binding
on `ctrl+i` never fires. `Esc` goes back,
`f` shows everything, `d` only the decision points, `t` the thinking and `a`
the answer.

Per token the table shows the probability the model gave the token it chose,
the entropy of the returned top-k in bits, the margin to the runner-up, and the
alternatives it passed over.

```text
INSPECT · qwen2.5:3b · top-k 5 · 40 tokens · 5 decision points
mean p 0.80   mean top-k entropy 0.72 bit
entropy is over the returned top-k, not the full vocabulary

   #  TOKEN                  P      H  MARGIN   ALTERNATIVES
  28  ' a'                0.36   2.03    0.18   ' known' 0.18  ' based' 0.16
  29  ' simplified'       0.46   1.72    0.23   ' well' 0.22  ' fundamental' 0.12  ◄ DECISION
  30  ' explanation'      0.86   0.76    0.80   ' scientific' 0.06  ' physical' 0.03
```

`Ctrl+L` opens a legend over the table explaining every column in the units it
is actually in — what `P` is computed from, what `H` is measured over, what
`MARGIN` compares — and quoting the decision-point criteria currently in force,
not the defaults. The same key closes it.

**What this is not.** These are measurements of the output distribution and
nothing else: no attention, no activations, no model internals. A flat
distribution is reported as a flat distribution — it is not evidence that the
model was "hesitating" or "thinking", and neither the view nor the report ever
says so. Entropy is computed over the returned top-k because the API does not
return the tail of the vocabulary, which is why it is called top-k entropy
everywhere it appears.

**Decision points** are positions where the distribution was both spread and
close: entropy at or above `entropy_threshold`, margin at or below
`margin_threshold`, not pure punctuation, and at least `min_distance` tokens
after the previous one. All four are configuration, printed in the report
alongside the figures they produced:

```json
"inspect": {
  "enabled": false,
  "top_k": 5,
  "entropy_threshold": 1.0,
  "margin_threshold": 0.35,
  "min_distance": 3,
  "skip_punctuation": true
}
```

`top_k` accepts 1 to 20; the endpoint refuses anything above 20 with HTTP 400,
so the configuration refuses it first.

**Thinking against answer.** When a model reasons, its thinking tokens carry
logprobs too, so the view and the report give the mean probability of each
phase separately. The phase comes from the delta each logprob arrived with — a
chunk carrying a logprob but no text, about a quarter of them on a reasoning
model, continues the phase already open rather than being guessed at.

**Cost.** Off by default, because `top_logprobs` makes each response several
times heavier and not every provider supports it. A provider that rejects the
option is retried once without it, told about once, and the answer is
unaffected — inspection never changes the outcome of a chat.

### Saving a session

`/export`, or `Ctrl+E`, writes the session **into the directory you started VOX
in** as `vox-<timestamp>.html`, `.json`, `.md` and `.toon`. Ask for one with
`/export html`.

Saved sessions go to the same place, as `vox-session-<name>.json`, and
`/sessions` lists the ones belonging to the directory you are in — not a pile
shared by every project. `/workspace <path>` moves both, so exports and
sessions follow wherever you point it.

Everything written this way is prefixed `vox-`, which makes it one line to
ignore in version control, and means a project's own `package.json` or
`tsconfig.json` is never mistaken for a session. What remains in `~/.vox` is
what belongs to you rather than to a project: configuration, roles, prompts,
input history and logs.

Each report opens with the question, the model, the provider and endpoint, the
role, and the parameters actually sent — temperature, max tokens, context
window, agent mode, timeout, any `extra_body`, and the inspection settings when
they were on. Then the exchange, with thinking kept separate from answers. Then
the statistics: tokens, wall time against time actually generating, tokens per
second, mean and median probability, mean top-k entropy, the thinking-versus-
answer comparison, and the decision point table. It closes with the VOX version,
the timestamp and a reminder of what the entropy figure is measured over.

The HTML is a single self-contained file that carries no JavaScript at all, so
it reads correctly with scripting disabled. The JSON is one documented schema
(`vox.report/1`) meant to be re-analysed by other tools. The Markdown lists only
the decision points, because a full token table is unreadable there. The TOON
(Token-Oriented Object Notation) export is a line-oriented, indentation-based
rendering of the same schema — the most token-efficient format for an LLM to
read the figures back from.

A session with inspection off still exports: you get the exchange, the timings
and a line saying there are no token figures.

### Reviewing a change before it happens

A write or a patch is never authorised blind: the dialog shows the unified
diff of what would change, context lines included, removals in terracotta and
additions in sage. A new file is announced as such and shown in full; a
rewrite that would change nothing says so; a file that is not UTF-8 text is
reported rather than diffed; a very long diff is clipped with a count of what
was left out.

The patch preview is produced by applying the diff in memory, so a patch that
does not fit the current file is reported before you are asked to approve it,
and nothing is written until you do. Building the preview reads the workspace,
so it refuses a path that escapes it exactly as the tool would.

### Confirmation dialogs

Writes, patches, commands and deletions open a two-button dialog. The cancel
side is focused first, so a stray `Enter` can never approve anything. `←`/`→`
(or `Tab`) move between the buttons, `Enter` activates the focused one, `Y`
approves, `Esc` cancels, and a legend under the buttons spells all of that out
for the dialog you are actually looking at.

`Esc` cancels any dialog or picker. `Ctrl+C` decides nothing there: it is the
copy key, so a dialog keeps waiting for a real answer.

### The agent mesh

VOX can join a peer-to-peer mesh of agents on the local network. It announces
nothing until you ask: `F3`, or `/mesh on`.

```text
> /mesh on
SYS ▸ JOINING THE MESH…
SYS ▸ MESH ONLINE - vox-b6ffa342e0d3 · PROCESSOR · announcing on
      239.17.42.1:45177 every 60s
SYS ▸ a second machine joins with a certificate of its own issued by
      ~/.vox/pki/ca.crt — copy that authority, not a shared secret: every
      agent signs with its own key
```

The border turns red for as long as VOX is announcing, the header switches
from `Universe: LOCAL` to `Universe: ON-LINE`, and the status bar counts what
it sees: `MESH ONLINE · 3 agents, 2 active`. `F3` again,
or `/mesh off`, takes it back offline; the choice is written to the
configuration, but going online is never automatic.

`F4` (or `/universe`) opens the universe:

```text
UNIVERSE · vox-b6ffa342e0d3 · PROCESSOR · 3 agents · 2 active

AGENT               CATEGORY      STATE      ADDRESS                   SEEN   VERBS
legacy              —             PROBATION  10.0.0.2:41000            0.9s   —
ingestor            SOURCE        ACTIVE     10.0.0.1:41000            2.1s   ingest
watcher             OBSERVER      SUSPECT    10.0.0.9:41000           91.4s   observe
```

It refreshes on its own once a second. `Ctrl+L` explains the states and the
categories; `Esc` closes it.

**On the keys.** The mesh is on `F3` and `F4` and on no ctrl combination at
all. `Ctrl+Shift+O` and `Ctrl+Shift+U` were tried first — most terminals do
not tell `Ctrl+Shift+<letter>` from `Ctrl+<letter>`, and only those speaking
the Kitty keyboard protocol, or Windows Terminal in win32-input mode, deliver
them — and `Ctrl+O` fared no better. Function keys reach the application
everywhere. `/mesh` and `/universe` remain the same code path.

#### How it works

The whole protocol is in
[vox_chat/discovery/README.md](../vox_chat/discovery/README.md), and the README
carries a diagram of it. The short version:

1. every agent multicasts a signed announcement to `239.17.42.1:45177` with a
   TTL of 1, so it never leaves the local segment;
2. a listener checks the HMAC signature, the timestamp and the nonce;
3. a peer that is new or has restarted is interrogated over mTLS — the WHOIS —
   and the answer carries the verbs it declares;
4. the category is derived from those verbs, identically on every node;
5. the announcement then serves as the heartbeat: three intervals of silence
   make a peer SUSPECT, five make it DEAD.

#### Configuration

```json
"mesh": {
  "enabled": false,
  "agent_id": "",
  "name": "vox",
  "verbs": ["infer"],
  "announce_interval": 60.0,
  "group": "239.17.42.1",
  "port": 45177,
  "pki_dir": "",
  "auto_provision": true,
  "demo_ca": true
}
```

`agent_id` is a *label*, not the whole name: whatever you write is cleaned up
into a DNS label and a fingerprint of this machine is appended, so
`"agent_id": "workstation"` becomes `workstation-b6ffa342e0d3`. Leave it empty
and the name is just `vox-<fingerprint>`.

The fingerprint is the first 12 hex characters of the SHA-256 of the network
card's MAC address. It is hashed, never announced, so the address itself never
goes on the wire. This is deliberate: **the identity must not be dictated by
the configuration file**. Copy your `config.json` to a second machine and it
still gets its own name, its own certificate and its own SAN — two machines
announcing the same agent id would each invalidate the other's handshake. On a
machine with no hardware MAC (some containers) the fingerprint falls back to a
hash of user and host; it is stable per run, and still not read from the
configuration.

Changing `verbs` changes the category VOX announces — `infer` makes it a
PROCESSOR.

#### Keys and certificates

#### The sample authority

VOX ships with a certificate authority in `vox_chat/demo_pki/`. The first `F3`
copies it into `~/.vox/pki/` and issues this agent a 24-hour certificate
against it, so two fresh installations on the same segment see each other with
no setup at all.

**Its private key is public** — it is in the package and in the repository — so
anybody with a copy of VOX can issue themselves an identity for it. A sample
mesh is open to whoever is on your network segment. VOX never hides this:

- the header reads `Universe: ON-LINE (SAMPLE CERT)`;
- the status bar appends `DEMO CERT`;
- going online writes the warning into the transcript;
- `vox doctor` reports `[WARN] MESH … SAMPLE CA, private key public`.

`mesh.demo_ca` is `true` by default, and it is a statement about which
authority to be on, not only about what to do when there is none: a machine
that already holds a private authority is moved onto the sample one when it
goes online. That is what makes a fresh install and an old one meet.

When you want a mesh of your own:

```text
/mesh new-ca
```

This sets `mesh.demo_ca` to `false` and keeps it there, generates an authority
that exists only on this machine, moves the sample `ca.crt`/`ca.key` aside as
`.replaced-<timestamp>`, deletes the certificates issued under it, and reissues
this agent. `/mesh sample-ca` goes back the other way, which is how you meet a
fresh installation again. The mesh restarts on the new anchor if
it was online. From then on every other machine needs a certificate from *your*
authority — the label disappears from the header once nobody is on the sample
one.

There is **no shared secret**. An agent signs every announcement with its own
private key — the same key as its certificate, mode 0600, never leaving the
machine — and attaches its certificate to the packet. A receiver checks that
certificate against its own `ca.crt`, checks that the announced agent id is one
the certificate names, and only then checks the signature.

After `/mesh new-ca`, a second machine needs two things, neither of them secret
between peers:

- `ca.crt`, to judge everyone else's certificates;
- a certificate of its own, issued by that same authority.

The quick way, on a machine you trust, is to copy the whole `~/.vox/pki`
directory — `ca.crt` and `ca.key` — and let VOX issue itself a certificate on
its first `F3`. (Copying it over a sample authority is enough; VOX only seeds
the sample one when there is no authority at all.) The careful way is to keep `ca.key` on one machine, run

```bash
python3 -c "from vox_chat.discovery.identity import issue_agent_cert; \
    issue_agent_cert('pki', 'vox-<their-fingerprint>', 'pki/ca.crt', 'pki/ca.key')"
```

there, and hand the other machine only its `<agent-id>.crt`, `<agent-id>.key`
and `ca.crt`. An intruder with none of those can still send packets; they are
rejected before they reach the registry, because the certificate they carry was
not issued by your authority.

Certificates are short-lived on purpose: a day is the most practical form of
revocation when there is no CRL. VOX reissues automatically once one is past
half its life.

To see a real second agent, the vendored runner starts one:

```bash
python3 -m vox_chat.discovery.run_agent --name ingestor \
    --agent-id ingestor-01 --pki ~/.vox/pki --verbs ingest --interval 5
```

It needs `ingestor-01.crt` and `ingestor-01.key` in that directory, issued by
the same CA; without them it cannot sign anything anyone will accept.

#### Limits worth knowing

- **Multicast does not cross routers**, and cloud VPCs disable it. Beyond one
  L2 segment this needs a seed list or a registry, which is not built yet.
- **The CA is the whole of the trust.** Whoever holds `ca.key` can issue an
  identity for any name, so it deserves the care you would give any signing
  key — and the sample one is held by everybody, which is the point of the
  label in the header. Individual agents only ever hold their own private key, and losing one
  compromises exactly one agent — for at most the 24 hours its certificate has
  left to live.
- **The WHOIS authorizer is permissive by default**: every mesh member may ask
  VOX to describe itself. The descriptor holds the agent id, the name and the
  declared verbs — nothing about your conversation, your model or your files.
- Discovery answers *who is there*; it carries no work. What agents do with
  each other after they have found each other is not part of this.

### CONSENSUS

`[CNS] … [/CNS]` marks the part of a message that may be sent to the other
agents on the mesh:

```text
Here is the stack trace from our staging box, with customer ids in it.
[CNS]Is a lock-free ring buffer safe when a reader can stall indefinitely?[/CNS]
```

Everything outside the tag stays on this machine. The local model still sees
the whole message with the markers removed; the peers see only the span, in a
fresh conversation with no context of yours attached.

```text
SYS  ▸ CONSENSUS - sending 68 characters to 2 agents: node-b, node-c
PEER node-b ▸ No, a stalled reader blocks reclamation.
              [qwen2.5-coder:3b · 14.0s]
PEER node-c ▸ Safe as long as readers never stall.
              [llama3.1:8b · 21.4s]
SYS  ▸ CONSENSUS - 2/2 answered · slowest 21.4s
SYS  ▸ CONSENSUS - the agents differ; reconciling locally
VOX  ▸ …
```

**Watching them think.** `F5` (or `/round`) opens the round as it happens.
Peers stream what they are writing over the same mTLS channel — no websocket,
because that channel is already an authenticated two-way stream — and each
fragment is shown with the time it arrived:

```text
THE ROUND · node-b  node-c
asked: Is a lock-free ring buffer safe when a reader can stall?
conversation 7f3c1a9b2e04

14:32:07 node-b: · weighing the reclamation problem
14:32:09 node-c: Yes, provided readers never stall.
14:32:11 node-b: No: a stalled reader blocks reclamation indefinitely.
```

One colour per agent, italics for its thinking and upright for its answer. This
is a separate view from the transcript on purpose: the transcript shows each
peer's answer as one block, which is right for reading afterwards and loses who
was writing at the same time as whom.

A peer that does not stream simply appears when it is done. Measured on one
machine with `qwen2.5-coder:3b`: the first fragment 4.1s in, then roughly ten
tokens a second until the answer completed.

**How the answers become one answer.** If the replies agree once normalised —
whitespace, case and trailing punctuation ignored — that is the result, and the
tally is written into the transcript. A vote needs at least two agents and a
strict majority: two out of five agreeing is a coincidence, not a decision.
Anything else goes to your local model, which is told to answer and to say
plainly where the agents agreed and where they did not.

Every reply is kept: in the transcript as `PEER` lines, in the side panel
(`/panel consensus`), in the saved session and in `/export`. A verdict you
cannot check is not worth having.

**What stops a round.** VOX refuses, and says which it is:

- consensus is off — `/consensus on`;
- the mesh is offline — `F3`;
- the marked text is over `max_question_chars`;
- an unclosed `[CNS]` — nothing is sent at all, rather than guessing where the
  span ends.

With no peers to ask it says so and answers locally instead of failing.

**One round at a time.** While the peers are out the status bar reads `ASKING
THE MESH` and another message is refused with `STILL ASKING THE MESH - CTRL+G
TO STOP`, because a round that returns after you have moved on would attach its
answers to the wrong question. `Ctrl+G` abandons it: the peers may still answer,
and those answers are dropped.

**On the sample authority** a round works, and warns before every one of them:

```text
ERR ▸ SAMPLE CERTIFICATE - this text is readable by anyone on the segment
      running VOX. /mesh new-ca for a mesh only yours
```

That is not a formality. The shipped authority's private key is in the package,
so anyone on your segment with a copy of VOX can issue themselves a certificate,
join, and be one of the agents you ask. The warning repeats every round because
the peer list you are shown is not the same thing as who can read the text.

`consensus.allow_sample_ca: false` turns the warning back into a refusal, for
asking and for answering alike.

**Answering other agents.** With `answer_requests` on, this node answers peers
that passed mTLS, one at a time, capped at `answer_max_tokens`. Each request is
written into the transcript — who asked, which conversation it belongs to, how
long the question was, how long the answer took — followed by the answer given.

`F5` works on this side too, and shows the same exchange from the other end:

```text
ANSWERING · vox-b6ffa342e0d3
asked by vox-1a2b3c4d5e6f: Is a lock-free ring buffer safe when a reader stalls?
conversation 7f3c1a9b2e04

14:32:07 vox-b6ffa342e0d3: · weighing the reclamation problem
14:32:09 vox-b6ffa342e0d3: No: a stalled reader blocks reclamation.
```

The conversation id is the asker's, so both machines' records of the round can
be lined up afterwards. What was answered is shown but never stored in this
machine's session: it is their conversation, and it must not become context for
your own model. A round you started yourself takes precedence — an incoming
question will not overwrite the view of one you are waiting on. The question is answered in a fresh two-message conversation:
your conversation, role, workspace and files are not part of it.

```json
"consensus": {
  "enabled": true,
  "verb": "infer",
  "max_peers": 5,
  "ask_timeout_seconds": 90.0,
  "max_question_chars": 4000,
  "answer_requests": true,
  "answer_max_tokens": 512,
  "quorum": 2,
  "allow_sample_ca": true
}
```

`verb` decides who is asked: `infer` means the PROCESSORs. OBSERVERs are never
asked — an agent that declared itself passive is not given work.

**Honest limits.** This is aggregation, not consensus in the distributed-systems
sense. mTLS proves who a peer is, not that it is truthful, and there is no
Byzantine tolerance: a member that lies is believed. Free text rarely produces
identical answers, so most rounds end with one model judging the others — which
is why the raw replies are always kept. A round is as slow as its slowest peer,
bounded by `ask_timeout_seconds`. Every agent asked sees the marked text in
full.

Measured on one machine with two nodes and `qwen2.5-coder:3b` answering: 14.0s
from asking to the answer, effectively all of it the model.

### Coding-agent mode

Off by default. `/agent on` exposes six tools to the model:

| Tool | Confirmation |
| --- | --- |
| `list_files`, `read_file`, `search_text` | none — read-only, workspace-only |
| `write_file`, `apply_patch` | required (`agent.confirm_writes`) |
| `run_command` | required (`agent.confirm_commands`) |

Every path is resolved and confined to the workspace: `..` traversal, absolute
paths outside it and symlinks pointing out of it are refused. Commands run
without a shell (so `&&`, pipes and redirections are inert arguments), under
`agent.command_timeout_seconds`, with stdout, stderr and the exit code captured
and truncated to `agent.max_output_bytes` before going back to the model. A
turn stops after `agent.max_tool_cycles` tool rounds. There is no flag that
skips a confirmation.

If the model does not support tool calling, agent mode switches itself off for
that turn and says so, instead of failing.

## Development

```bash
pip install -e ".[dev]"
python -m compileall vox_chat
pytest
```

Tests never need a running inference server: streaming and the agent loop are
driven by fake clients.

Layout: `config.py` and `storage.py` own persistence, `llm_client.py` is the
only place an OpenAI client is constructed, `tools.py` owns workspace
confinement, `agent.py` owns the tool loop, and everything visual lives in
`app.py` plus `ui/`.

## License

PolyForm Noncommercial 1.0.0 — see [LICENSE](LICENSE). Non-commercial use only.
