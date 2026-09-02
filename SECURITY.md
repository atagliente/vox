# Security

## Reporting something

Report it privately, through
[GitHub's security advisories](https://github.com/atagliente/vox/security/advisories/new).
That page is private until an advisory is published, which is the point: a
public issue tells everyone at once, including the people you would rather it
did not.

Include what it is, how to reach it, and what it gets. A short reproduction is
worth more than a long description of what could happen. You will get an
acknowledgement; if the report is valid you will be credited in the advisory
unless you ask not to be.

Please do not open a public issue for a vulnerability, and please do not test
against anybody else's machine.

## What VOX defends

VOX runs on your machine and talks to a model you chose. Most of what it does
is therefore not a trust boundary. These are:

**The workspace confinement.** Agent-mode tools resolve every path inside the
configured workspace and refuse anything that leaves it — `..`, an absolute
path outside, or a symlink whose target escapes. Commands are split without a
shell, so redirection and chaining are not available to the model. Every
write, patch and command is confirmed by a human with the change in front of
them; there is no bypass parameter anywhere in `tools.py`.

**What a fetched page is.** Text from the web reaches the model labelled as
data, never as instructions, because a page saying "ignore your previous
instructions" is a page. Private and loopback addresses are refused *after*
name resolution rather than by pattern, so `127.0.0.1.nip.io` does not get
through.

**What an MCP server is.** A server is somebody else's program and its tool
descriptions are written by its author. They reach the model as text, and
every call is confirmed unless the server itself marks the tool read-only.
A tool the server marks destructive is confirmed whatever the setting says.

**The mesh.** Peers authenticate with mTLS against a certificate authority you
control. `agent_id` is bound to the certificate's SAN, so an announcement
claiming to be somebody else fails the handshake rather than being believed.
Only the text between `[CNS]` and `[/CNS]` leaves the machine, and a test
holds that boundary.

**Secrets in logs.** API keys are redacted by a filter, not by remembering to.

## What VOX does not defend

Stated plainly, because a promise nobody made cannot be broken:

- **The mesh has no Byzantine tolerance.** A peer that authenticates
  successfully and then lies is believed. mTLS answers "who is this", not
  "is this true".
- **The sample certificate authority is not a secret.** It ships in the
  repository so a fresh clone can join a mesh without provisioning. Anyone
  with a copy can mint a certificate it trusts. VOX warns every time it is in
  use; `consensus.allow_sample_ca: false` refuses instead.
- **Agent commands run as you.** They are confined to the workspace by path
  and confirmed by a human, but they are not sandboxed: a confirmed command
  can do anything your account can. The `deny` list and the resource limits
  narrow this; they do not close it.
- **A model is not trusted, but it is powerful.** VOX confirms before acting.
  A confirmation nobody reads is not a confirmation.

## Supported versions

VOX is pre-1.0. Fixes go to `main`; there are no maintained release branches.

## How the supply chain is handled

- Dependencies are constrained to majors that have actually been run against,
  not to "anything newer".
- `requirements.lock` pins exact versions with hashes, for installations that
  need to be reproducible.
- `pip-audit` runs in CI on every push, and Dependabot opens pull requests
  weekly.
- `bandit` runs in CI over the source.
