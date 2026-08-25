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
| `Ctrl+N` / `Ctrl+W` | new / save session | `Ctrl+I` / `Ctrl+E` | inspect / export |
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

`/inspect on` asks the provider for the distribution behind every token it
emits; `Ctrl+I`, or `/inspect` with no argument, opens a full-screen table that
fills while the answer streams and stays open afterwards. `Esc` goes back,
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

`/export`, or `Ctrl+E`, writes the session to `~/.vox/reports/` as
`vox-<timestamp>.html`, `.json` and `.md`. Ask for one with `/export html`.

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
the decision points, because a full token table is unreadable there.

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
