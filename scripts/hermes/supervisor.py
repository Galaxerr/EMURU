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
    parser.add_argument("--task")
    parser.add_argument("--stage", choices=("coder", "scout"))
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument(
        "--owner", type=int, help="Required for Scout policy enrollment"
    )
    parser.add_argument("--bot", type=int, help="Required for Scout policy enrollment")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--batch-preview", type=Path)
    mode.add_argument("--batch-enroll", type=Path)
    parser.add_argument("--expect")
    args = parser.parse_args()
    if args.batch_preview or args.batch_enroll:
        if (
            args.task
            or args.stage
            or (args.batch_enroll is not None) != (args.expect is not None)
        ):
            parser.error(
                "Batch modes exclude --task/--stage; enrollment requires --expect only."
            )
    elif not args.task or not args.stage or args.expect:
        parser.error("Single-task enrollment requires --task and --stage.")
    os.umask(0o077)
    if os.getuid() <= 0 or os.getgid() <= 0:
        raise ValueError("Supervisor enrollment must run as an unprivileged owner.")
    launcher = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    _, image = launcher["ensure_image"](build=False)
    if args.batch_preview or args.batch_enroll:
        from emuru.projects import continuation_batch
        from emuru.projects.coder import load_grant

        manifest = continuation_batch.load_manifest(
            args.batch_preview or args.batch_enroll
        )
        policy = load_grant(args.policy)
        if any(
            v is not None and v != policy[k]
            for k, v in (("owner", args.owner), ("bot", args.bot))
        ):
            raise ValueError("Batch operator identity does not match policy.")
        if args.batch_preview:
            result = continuation_batch.preview(
                args.home, args.vault, ROOT, manifest, policy, image
            )
        else:
            result = continuation_batch.enroll(
                args.home,
                args.vault,
                ROOT,
                manifest,
                policy,
                image,
                args.expect,
                seconds=args.seconds,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
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
