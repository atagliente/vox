# Discovery Agent

Peer-to-peer discovery for distributed agent systems: a signed multicast
presence announcement, a WHOIS handshake over **mTLS**, and a local registry
with failure detection.

This package is vendored into VOX as it was written; only its imports were made
relative, so VOX stays a single installable package. `vox_chat/mesh.py` is the
thin layer that VOX itself talks to.

## The flow

    1. announcer   every ~60s (with jitter) sends an HMAC-signed announcement
    2. listener    receives, checks signature and freshness, consults the registry
    3. whois       if the peer is new or restarted -> a unicast dialogue over mTLS
    4. classify    the category is derived from the declared verbs, deterministically
    5. reaper      3 intervals of silence -> SUSPECT, 5 -> DEAD

Step 3 is the only one skipped for a peer already known. The announcement
packet is **always** processed: it is the heartbeat.

## The security model

    layer         protects against                   mechanism
    -----------   --------------------------------   ---------------------------
    announcement  forged announcements, replay       HMAC + timestamp + nonce
    WHOIS         peers outside the mesh             mTLS against the internal CA
    WHOIS         impersonating a peer               cert SAN == agent_id
    WHOIS         an over-curious legitimate member  a per-caller authorizer

The third line is the binding that holds the two channels together: the client
passes `server_hostname=<the announced agent_id>`, so a node answering in place
of the legitimate peer fails the SAN check even when it holds a valid mesh
certificate.

The fourth keeps authentication and authorisation apart: `WhoisServer` takes an
`authorizer(agent_id) -> bool`, and the permissive default should be replaced as
soon as the mesh stops being uniformly trusted.

## Peer states

    PROBATION  seen, not yet interrogated  -> no work is routed to it
    ACTIVE     whois completed and classified
    SUSPECT    heartbeats missing
    DEAD       out of routing

A peer that fails the certificate check does not stay in PROBATION: it lands in
`_rejected` and is not interrogated again on every announcement. That is a
persistent condition, not a transient one, and it belongs in the logs.

## Use

VOX provisions its own CA and certificate on the first `Ctrl+Shift+O`, so
nothing here is needed for ordinary use. To start a second agent by hand — the
easiest way to see a mesh of more than one:

    export DISCOVERY_PSK="a-key-of-at-least-16-bytes"
    python3 -m vox_chat.discovery.run_agent --name ingestor \
        --agent-id ingestor-01 --pki ~/.vox/pki --verbs ingest --interval 5

The `agent_id` has to be a valid DNS label (letters, digits, hyphens): it ends
up in a DNS SAN. `Identity.load` fails immediately when the certificate does not
carry the agent_id, rather than producing opaque handshake failures later.

The suite lives in `tests/test_discovery_vendor.py` and binds real sockets, so
it runs only when asked:

    VOX_TEST_MESH=1 pytest tests/test_discovery_vendor.py

## Certificate renewal

The default lifetime is 24h. A short life is the most practical form of
revocation there is: with no CRL and no OCSP, a stolen key stops being worth
anything within a day.

The `cert-watch` thread warns past half life. The renewal itself is external —
in VOX, `mesh.ensure_identity()` reissues on the next start; then
`agent.renew(Identity.load(...))` swaps the TLS context in while running, with
no restart and no change of `incarnation`, so as far as the peers are concerned
nothing happened.

## What is still open

- **The multicast PSK is still shared.** With the CA already in place, the
  natural next step is signing announcements with the agent's own private key
  (Ed25519, the same one as the certificate) and using the fingerprint as the
  identifier. It touches only `protocol.py`.
- **Explicit revocation.** Today there is only expiry. Ejecting an agent early
  needs a CRL, or a move to SPIRE.
- **A fallback beyond the L2 segment.** Multicast does not cross routers and is
  disabled in cloud VPCs: that needs a seed list, or a registry.
- **A persistent incarnation.** It currently comes from the start clock; a
  monotonic counter on disk would survive the clock jumping backwards.

## Consistency

Every agent keeps its own registry: the views diverge and reconverge. There is
no single point of failure, but there is no agreed view of "who is out there"
either. If a quorum is ever needed, it has to be added as a layer above.
