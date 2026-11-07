"""Operator-only grant enrollment; does not launch workers or change native cards."""

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from emuru.projects.supervisor import enroll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--stage", choices=("coder", "scout"), required=True)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument(
        "--owner", type=int, help="Required for Scout policy enrollment"
    )
    parser.add_argument("--bot", type=int, help="Required for Scout policy enrollment")
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() <= 0 or os.getgid() <= 0:
        raise ValueError("Supervisor enrollment must run as an unprivileged owner.")
    launcher = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    _, image = launcher["ensure_image"](build=False)
    result = enroll(
        args.home,
        args.vault,
        ROOT,
        args.stage,
        args.policy,
        args.task,
        image,
        seconds=args.seconds,
        owner=args.owner,
        bot=args.bot,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print(
            "Enrollment refused; inspect private state. No work launched.",
            file=sys.stderr,
        )
        raise SystemExit(1)
