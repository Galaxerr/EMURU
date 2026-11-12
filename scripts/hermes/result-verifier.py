"""Read-only completion verification in the operator-selected pinned Hermes runtime."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(sys.prefix).parent))
sys.path.insert(0, str(ROOT / "src"))

from emuru.projects.results import verify_completion
from emuru.projects.scout import encoded


def main():
    try:
        if sys.argv[1:] == ["--check-runtime"]:
            from emuru.projects.board import native_api

            native_api(ROOT)
            print('{"pinned_native_runtime":true}')
            return 0
        if sys.argv[1:]:
            raise ValueError("Unexpected verifier arguments.")
        request = json.loads(sys.stdin.buffer.read(4097))
        if (
            not isinstance(request, dict)
            or set(request) != {"home", "vault", "spec", "task", "run", "owner", "bot"}
            or not all(isinstance(request[k], str) for k in request if k != "run")
            or type(request["run"]) is not int
        ):
            raise ValueError("Invalid verifier request.")
        binding, _, _, _ = verify_completion(
            Path(request["home"]),
            Path(request["vault"]),
            ROOT,
            request["spec"],
            request["task"],
            request["run"],
            request["owner"],
            request["bot"],
        )
        payload = encoded(binding)
        if len(payload) > 4096:
            raise ValueError("Verifier binding exceeds limit.")
        sys.stdout.buffer.write(payload)
    except Exception:  # noqa: BLE001 -- Never expose private evidence in IPC diagnostics.
        print("Capture evidence verification refused.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
