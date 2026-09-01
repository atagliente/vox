#!/usr/bin/env python3
"""A command line discovery agent, handy for testing a mesh of more than one.

    DISCOVERY_PSK=a-key-of-at-least-16-bytes \
        python3 -m vox_chat.discovery.run_agent --name ingestor \
        --agent-id ingestor-01 --verbs ingest,publish --interval 5
"""

import argparse
import logging
import time
from pathlib import Path

from vox_chat.discovery.agent import DiscoveryAgent, load_psk
from vox_chat.discovery.identity import Identity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--agent-id", required=True,
                        help="must match the SAN of the certificate")
    parser.add_argument("--pki", default="./pki",
                        help="the directory holding <agent-id>.crt/.key and ca.crt")
    parser.add_argument("--verbs", default="transform",
                        help="the verbs declared, comma separated")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--run-for", type=float, default=0.0,
                        help="0 means for ever")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    identity = Identity.load(args.agent_id, Path(args.pki))
    logging.info("identity loaded, the certificate expires on %s",
                 identity.expires_at().isoformat())

    agent = DiscoveryAgent(
        identity=identity,
        name=args.name,
        capabilities={
            "verbs": [v.strip() for v in args.verbs.split(",") if v.strip()],
            "formats": ["application/json"],
            "max_concurrency": 8,
        },
        psk=load_psk(),
        announce_interval=args.interval,
    )
    agent.start()

    deadline = time.time() + args.run_for if args.run_for else None
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(args.interval)
            peers = agent.registry.snapshot()
            logging.info("[%s] registry: %s", args.name,
                         [(p.name or p.agent_id, p.state.value, p.category) for p in peers])
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
