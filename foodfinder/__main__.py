"""Command line entry points for the local app."""

from __future__ import annotations

import argparse
import sys

from foodfinder.database import initialize
from foodfinder.scanner import refresh_directory, run_scan
from foodfinder.settings import HOST, PORT, database_path


def main() -> int:
    parser = argparse.ArgumentParser(prog="foodfinder", description="U of T complimentary-food event finder")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="start the local dashboard")
    login = subcommands.add_parser("login", help="log in to your own Instagram account")
    login.add_argument(
        "--browser-cookie",
        choices=("brave", "chrome", "chromium", "edge", "firefox", "librewolf", "opera", "opera_gx", "vivaldi"),
        help="import your signed-in session from this browser instead of password login",
    )
    discover = subcommands.add_parser("discover", help="refresh the U of T club directory")
    discover.add_argument("--names-only", action="store_true", help="skip visiting individual club profiles")
    scan = subcommands.add_parser("scan", help="run a background-free scan now")
    scan.add_argument("--discover", action="store_true", help="also refresh club-directory data")
    args = parser.parse_args()
    initialize(database_path())

    if args.command == "serve":
        import uvicorn

        uvicorn.run("foodfinder.web:app", host=HOST, port=PORT, reload=False, access_log=False)
        return 0
    if args.command == "login":
        from foodfinder.instagram import interactive_login

        try:
            username = interactive_login(browser_cookie=args.browser_cookie or "")
        except Exception as exc:
            print(f"Instagram login was not completed: {exc}", file=sys.stderr)
            return 1
        print(f"Instagram session saved for @{username}.")
        return 0
    if args.command == "discover":
        stats = refresh_directory(enrich_profiles=not args.names_only)
        print(f"Found {stats['clubs']} St. George club listings and {stats['accounts']} Instagram accounts.")
        return 0
    if args.command == "scan":
        run_id = run_scan(force_discovery=args.discover)
        print(f"Scan run: {run_id}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
