# Contributing

## Running it

```bash
git clone https://github.com/atagliente/vox
cd vox
pip install -e ".[dev]"
pre-commit install
```

Four checks, the same ones CI runs, so a red build is a surprise rather than
the norm:

```bash
ruff check . && ruff format --check .
mypy
pytest -q
```

The suite runs across every core and takes under two minutes. `pytest -m "not
slow"` drops the handful that wait on real sockets and timers. `pytest -n0`
puts it back in one process when a traceback is easier to read that way, and
`nox` runs the lot on every interpreter you have installed.

Nothing in the suite needs a model, a network or a running server. If a change
you are making seems to require one, that is worth a conversation before it is
worth a pull request.

**If `pytest` fails with a `PermissionError` about `pytest-of-<you>`**, your
temporary directory is not writable. It says so once, at the top, with the two
ways out.

## How it is laid out

`app.py` draws the screen and owns the widgets. Everything that is not drawing
lives beside it:

| Module | What it holds |
| --- | --- |
| `commands/` | the slash commands: `spec` the vocabulary, `handlers` what each does, `dispatch` the table joining them |
| `generation.py` | one turn, from the send key to the last token |
| `consensus_flow.py` | a mesh round, and answering when another agent asks |
| `agent.py`, `tools.py` | the tool loop, and the tools it may call |
| `mcp/` | talking to Model Context Protocol servers, and `server.py` for being one |
| `hosting.py` | what a command may ask of the thing running it |
| `turn.py` | what gets sent, for whichever host is asking |
| `webui/` | `vox ui`: the browser host, the server and the page |
| `sandbox.py` | running a confirmed command somewhere it can do less harm |
| `http.py` | the only place VOX makes an HTTP request |
| `threads.py` | the only place VOX starts a thread of its own |
| `discovery/` | the mesh: multicast announcements, mTLS, the whois server |

The controllers hold the application rather than inheriting from it. Textual's
`@work` decorators stay on `VoxApp`, because putting something on a thread is
the framework's business and belongs with the widget.

A command is written against `hosting.Host`, not against `VoxApp`. If you need
something from the application that the protocol does not have, add it there
and answer it in both hosts — a command that only works in the terminal is a
command that quietly does nothing in the browser.

## What a pull request is expected to carry

**A test that fails without the change.** Not coverage for its own sake — a
test that would have caught the thing. If the change is a fix, the test is the
reproduction.

**A reason, in the code.** The comments here explain *why*, not *what*: why a
wide `except` is right at that boundary, why a default is what it is, why the
obvious approach was not taken. A comment restating the line above it is worse
than none. If you find yourself writing "this is deliberate", say what the
alternative was and why it lost.

**Honesty about what it does not do.** A limit named in a docstring is worth
more than one discovered later. `SECURITY.md` is written this way on purpose:
what VOX defends, and — at equal length — what it does not.

**A commit message that says what changed for the user.** Present tense,
imperative, describing the effect rather than the diff: *Make the threads say
who they are* rather than *Add threads.py*. The body is for the reasoning, and
it is worth writing at length when there is reasoning to record.

## Things that will be asked about

- **A new dependency.** VOX uses four, and each earns its place. The standard
  library has done the HTTP, the HTML parsing, the JSON-RPC and the TLS so
  far. A fifth would need to be doing something those cannot.
- **A default that acts.** Nothing that talks to the network, spawns a
  process, or writes to somebody's disk is on by default. `mcp`, `index`,
  `web` and compaction are all off until asked for, and that is a pattern
  rather than an accident.
- **A promise the code cannot keep.** The mesh has no Byzantine tolerance and
  says so. An "improvement" whose real effect is to make an existing limit
  harder to see is a regression.

## Reporting a vulnerability

Not here. See [SECURITY.md](SECURITY.md) — privately, through GitHub's
security advisories.
