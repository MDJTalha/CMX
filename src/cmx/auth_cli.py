"""Trusted local operator commands for CMX service credentials.

Do not expose this command as an HTTP endpoint. Access to the auth database and this
operator command is administrative authority to grant or revoke tenant permissions.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from cmx.service_auth import SQLiteCredentialStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmx-auth",
        description="Issue or revoke CMX server-side service credentials.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue = subparsers.add_parser("issue", help="Create a service key and display it once.")
    issue.add_argument("--subject-id", required=True, help="Trusted service identity ID.")
    issue.add_argument("--tenant-id", required=True, help="Tenant to bind the key to.")
    issue.add_argument(
        "--permission",
        action="append",
        required=True,
        dest="permissions",
        help="Permission grant; repeat this argument for multiple permissions.",
    )
    issue.add_argument(
        "--expires-in-days", type=int, default=30, choices=range(1, 366),
        metavar="{1..365}", help="Lifetime in days; defaults to 30.",
    )
    issue.add_argument("--issuer", default="urn:cmx:service-key")

    revoke = subparsers.add_parser("revoke", help="Revoke a credential by its credential ID.")
    revoke.add_argument("--credential-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    database_path = os.environ.get("CMX_AUTH_DB_PATH", "data/cmx-auth.sqlite3")

    try:
        store = SQLiteCredentialStore(database_path)
        now = int(time.time())
        if args.command == "issue":
            issued = store.issue(
                subject_id=args.subject_id,
                tenant_id=args.tenant_id,
                permissions=args.permissions,
                expires_in_seconds=args.expires_in_days * 24 * 60 * 60,
                now_epoch=now,
                issuer=args.issuer,
            )
            print("CMX service credential created. Copy the bearer token now; it is not stored in plaintext.")
            print(f"Credential ID: {issued.credential_id}")
            print(f"Subject: {issued.subject_id}")
            print(f"Tenant: {issued.tenant_id}")
            print(f"Permissions: {', '.join(sorted(issued.permissions))}")
            print(f"Expires at (UTC epoch): {issued.expires_at}")
            print(f"Bearer token: {issued.token}")
            return 0

        if args.command == "revoke":
            if not store.revoke(args.credential_id, now_epoch=now):
                print("Credential was not found or was already revoked.", file=sys.stderr)
                return 1
            print(f"Credential revoked: {args.credential_id}")
            return 0

        parser.error("Unsupported command.")
    except (OSError, ValueError, RuntimeError) as exc:
        # Never print bearer tokens or database contents in error messages.
        print(f"CMX credential operation failed: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
