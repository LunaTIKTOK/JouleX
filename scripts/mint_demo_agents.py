#!/usr/bin/env python3
"""Mint demo agents for a JouleX deployment — DEMO ONLY, paper-only.

The deployed server trusts one demo issuer (see
``data/trust_store.demo.json``). This script re-derives that issuer
from its seed — read ONLY from the ``JOULEX_DEMO_ISSUER_SEED_HEX``
environment variable, never from a file in this repo and never printed
— mints two demo agents (Alice + Bob), and, with ``--url``, posts a
fresh signed revocation list to the target server so the minted
credentials verify.

Operating model (CF-KYA rules, not this script's choice): revocation
lists go stale after 1 hour and credentials last at most 24h. Run this
script before a demo session and trade within the hour; re-run it to
refresh. The seed is throwaway demo key material: anyone holding it can
mint credentials the deployed server accepts, so treat it like a demo
password — never use it for anything real. Rotate by generating a new
seed, re-running with ``--write-trust-store`` to replace the seed
file, and redeploying.

Usage:
  JOULEX_DEMO_ISSUER_SEED_HEX=<64 hex chars> python3 scripts/mint_demo_agents.py [--url http://127.0.0.1:8472] [--write-trust-store data/trust_store.demo.json] [--out agents.json]

Output (stdout, or --out): JSON ``{"alice": {...}, "bob": {...}}`` with
each agent's ``credential``, ``agent_seed_hex``, ``agent_id`` and
``principal_id``. Feed ``credential`` + ``bytes.fromhex(agent_seed_hex)``
to ``joulex.client.ExchangeClient`` (see demo.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import joulex  # noqa: E402  (puts joulex/vendor on sys.path)
import ed25519  # noqa: E402
from joulex.demo_issuer import DEMO_ISSUER_ID, DemoIssuer  # noqa: E402

SEED_ENV = "JOULEX_DEMO_ISSUER_SEED_HEX"


def issuer_from_seed(seed_hex: str, *, now_fn=None) -> DemoIssuer:
    """Re-derive the demo issuer from its 32-byte seed (hex).

    The seed is the only secret in this flow; it arrives from the
    environment, is never written to a file by this script, and is
    never printed."""
    try:
        seed = bytes.fromhex(seed_hex.strip())
    except ValueError as exc:
        raise ValueError(f"{SEED_ENV} must be 64 hex characters (32 bytes)") from exc
    if len(seed) != 32:
        raise ValueError(f"{SEED_ENV} must be 64 hex characters (32 bytes)")
    issuer = DemoIssuer(now_fn=now_fn)
    issuer.issuer_seed = seed
    issuer.issuer_public_key_hex = ed25519.public_key_from_seed(seed).hex()
    return issuer


def build_trust_store(issuer: DemoIssuer) -> dict[str, Any]:
    """The seed file: public keys + a signed public revocation list.

    Contains NO private material. The revocation list is fresh at
    build time and goes stale after 1 hour by design — the mint flow
    refreshes it on the target server before trading."""
    return {
        "_demo_only": True,
        "note": (
            "DEMO ONLY — paper-only demo issuer for the JouleX deployment. "
            "Throwaway keys: this file holds public keys and a signed public "
            "revocation list only. It never authorises real value. "
            "Revocation lists go stale after 1 hour; refresh via "
            "scripts/mint_demo_agents.py before a demo session."
        ),
        "trusted_issuers": issuer.trust_store,
        "revocation_lists": {issuer.issuer_id: issuer.revocation_list()},
    }


def mint_agents(issuer: DemoIssuer) -> dict[str, dict[str, Any]]:
    """Mint the two demo agents. Only the trading material is returned:
    credential + agent seed (hex) + ids. Principal seeds are dropped —
    the demo client never needs them."""

    def one(principal_id: str, display_name: str, agent_id: str) -> dict[str, Any]:
        bundle = issuer.mint_agent(
            principal_id=principal_id,
            display_name=display_name,
            agent_id=agent_id,
        )
        return {
            "credential": bundle["credential"],
            "agent_seed_hex": bundle["agent_seed"].hex(),
            "agent_id": bundle["agent_id"],
            "principal_id": bundle["principal_id"],
        }

    return {
        "alice": one(
            "principal-demo-alice", "Alice Demo Principal", "agent-demo-alice"
        ),
        "bob": one(
            "principal-demo-bob", "Bob Demo Principal", "agent-demo-bob"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint demo agents for a JouleX deployment (DEMO ONLY)."
    )
    parser.add_argument(
        "--url",
        default=None,
        help="deployed server base URL; when given, post a fresh signed "
        "revocation list there before minting (required for orders to verify).",
    )
    parser.add_argument(
        "--write-trust-store",
        default=None,
        metavar="PATH",
        help="also write the seed trust store (public keys only) to PATH.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="write the agent bundle JSON to PATH instead of stdout.",
    )
    args = parser.parse_args(argv)

    seed_hex = os.environ.get(SEED_ENV)
    if not seed_hex:
        print(f"{SEED_ENV} is not set — refusing to mint.", file=sys.stderr)
        return 2
    try:
        issuer = issuer_from_seed(seed_hex)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.write_trust_store:
        store = build_trust_store(issuer)
        Path(args.write_trust_store).write_text(
            json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"wrote demo trust store to {args.write_trust_store} "
            f"(issuer {DEMO_ISSUER_ID}, public keys only)",
            file=sys.stderr,
        )

    if args.url:
        from joulex.client import ExchangeClient

        client = ExchangeClient(args.url)
        client.refresh_revocation_list(issuer.revocation_list())
        print(
            f"posted a fresh revocation list to {args.url} "
            "(valid 1 hour; re-run before the next demo session)",
            file=sys.stderr,
        )

    bundle = mint_agents(issuer)
    text = json.dumps(bundle, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote demo agent bundle to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
