"""Render the private, primary-only LiteLLM configuration while the agent is stopped."""

import argparse
import os

from emuru.models.gateway import read_route, render, write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", default=os.environ.get("EMURU_CONTAINER_ROUTE"))
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--empty",
        action="store_true",
        help="Render authenticated infrastructure with no inference route",
    )
    args = parser.parse_args()
    if args.empty and args.route:
        parser.error("--empty cannot be combined with a route")
    if not args.route and not args.empty:
        parser.error("--route or EMURU_CONTAINER_ROUTE is required")
    try:
        write_private(
            args.output, render(None if args.empty else read_route(args.route))
        )
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    print("Primary-only gateway configuration rendered; restart explicitly to apply")


if __name__ == "__main__":
    main()
