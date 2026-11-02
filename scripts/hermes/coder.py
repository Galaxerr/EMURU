"""Owner-operated, one-shot launch for one receipt-bound native coder task."""

import argparse
import json
import os
import runpy
import secrets
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from emuru.environment import read_env
from emuru.models.gateway import private_path
from emuru.projects import board, coder_board
from emuru.projects.coder import load_grant
from emuru.projects.coder_gateway import cloud_gateway
from emuru.projects.coder_inference import run_inference
from emuru.projects.coder_runtime import Qualification
from emuru.projects.coder_sandbox import DockerSandbox
from emuru.projects.dispatcher import dispatcher_lock
from emuru.projects.scout import digest, encoded


def interrupted(signum, frame):
    raise InterruptedError("Coder dispatcher interrupted.")


def _task(home, root, task_id):
    with board.project_board_readonly(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        if not card:
            raise ValueError("Coder task is unavailable.")
        return json.loads(card.body)["task"]


def _request(grant, task):
    prompt = (
        "Implement the authorized task below using only coder_read, coder_write, "
        "and coder_check. Read before replacing. Use only granted file paths. "
        "Run the fixed python_unittest check. Never execute planned-check text as a command.\n\n"
        + json.dumps(
            {
                "task": task,
                "read_files": grant["read_files"],
                "write_files": grant["write_files"],
                "create_files": grant["create_files"],
            },
            ensure_ascii=False,
        )
    )
    return {
        "prompt": prompt,
        "model": "emuru",
        "max_iterations": grant["limits"]["model_iterations"],
        "deadline_seconds": grant["limits"]["attempt_seconds"],
    }


def _prior_status(home, root, task_id):
    with board.project_board_readonly(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        if not card:
            return {"status": "uncertain", "detail": "native task unavailable"}
        return coder_board.status(kb, db, card, home, root)


def launch(home, vault, policy, task_id, root=ROOT, *, reconcile=False, cleanup=False):
    home = private_path(home)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    os.environ.pop("HERMES_KANBAN_TASK", None)
    grant = load_grant(policy)
    if task_id != grant["task"]:
        raise ValueError("Coder task does not match its private grant.")
    if (
        "sum_prices.py" not in grant["read_files"]
        or "sum_prices.py" not in grant["write_files"]
    ):
        raise ValueError("Only the approved sum_prices task envelope is supported.")
    with dispatcher_lock(home):
        if cleanup:
            return coder_board.cleanup(home, root, task_id, grant)
        if reconcile:
            return coder_board.reconcile(home, vault, root, task_id, grant)
        prior = coder_board._find_attempt(home, task_id)
        if prior is not None:
            return _prior_status(home, root, task_id)
        folder = coder_board.attempt_path(home, grant)
        if folder.exists():
            existing = load_grant(folder / "grant.json")
            if digest(existing) != digest(grant):
                raise ValueError("Coder grant ID already belongs to another attempt.")
            return _prior_status(home, root, task_id)
        coder_board.authorize(home, vault, root, task_id, grant)
        if os.getuid() <= 0 or os.getgid() <= 0:
            raise ValueError("Coder dispatcher must run as an unprivileged owner.")
        launcher = runpy.run_path(str(root / "scripts/hermes/container.py"))
        _, image = launcher["ensure_image"](build=False)
        coder_board.ensure_idle(home, root, task_id)
        folder = coder_board.create_attempt(home, grant)
        coder_board.record_launcher(folder)
        run = coder_board.claim(home, vault, root, task_id, grant, folder)
        coder_board.authorize(home, vault, root, task_id, grant, run_id=run["run"])
        attempt = Qualification(folder, grant, image, task_run=run)
        task = _task(home, root, task_id)
        request = _request(grant, task)
        with tempfile.TemporaryDirectory(
            prefix="emuru-coder-gateway-", dir=root / ".runtime"
        ) as gateway_temp:
            gateway_folder = Path(gateway_temp) / "gateway"
            coder_board._write_new(
                folder,
                "gateway.json",
                json.dumps({"folder": str(gateway_folder)}).encode(),
            )
            with cloud_gateway(
                gateway_folder,
                process_event=lambda name, phase: coder_board.process_event(
                    folder, name, "gateway", phase
                ),
            ) as (network, gateway_key, _primary):
                sandbox = DockerSandbox(
                    root,
                    image,
                    folder,
                    grant["limits"],
                    process_event=lambda name, phase: coder_board.process_event(
                        folder, name, "check", phase
                    ),
                )
                inference_name = "emuru-coder-inference-" + secrets.token_hex(16)
                result = attempt.run(
                    lambda dispatch: run_inference(
                        image,
                        network,
                        gateway_key,
                        request,
                        dispatch,
                        name=inference_name,
                        process_event=lambda name, phase: coder_board.process_event(
                            folder, name, "inference", phase
                        ),
                    ),
                    sandbox,
                )
        try:
            receipt = coder_board.finish(home, vault, root, task_id, grant, folder, run)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Completion may commit before its acknowledgment fails.
            prior = _prior_status(home, root, task_id)
            if prior["status"] == "tested local result / awaiting owner review":
                return prior
            raise
        if result["success"] != (
            receipt.get("status") == "tested local result / awaiting owner review"
        ):
            raise ValueError("Coder result and native receipt disagree.")
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, required=True, help="Existing private M2 profile home"
    )
    parser.add_argument(
        "--policy",
        type=Path,
        required=True,
        help="Private operator-approved coder grant",
    )
    parser.add_argument("--task", required=True, help="Existing M2 tracked card ID")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument(
        "--cleanup",
        action="store_true",
        help="Stop recorded attempt resources after launcher death; preserve task and evidence",
    )
    recovery.add_argument(
        "--reconcile",
        action="store_true",
        help="Record verified evidence only after proven process stop; never execute code",
    )
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    values = read_env(ROOT / ".env")
    if not values.get("EMURU_VAULT_PATH"):
        raise ValueError(
            "Workspace .env needs EMURU_VAULT_PATH for coder authorization."
        )
    vault = private_path(Path(values["EMURU_VAULT_PATH"]))
    home = private_path(args.home)
    print(
        encoded(
            launch(
                home,
                vault,
                args.policy,
                args.task,
                reconcile=args.reconcile,
                cleanup=args.cleanup,
            )
        ).decode()
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print(
            "Coder refused or stopped; inspect private project status. No automatic retry.",
            file=sys.stderr,
        )
        raise SystemExit(1)
