"""Host-owned transport to a separate native inference container, never a shell."""

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
import uuid

from emuru.projects.coder_native import MAX_LINE, validate_call, validate_request
from emuru.projects.coder_sandbox import bounded_process


def verify_native_completion(image, name, final, sequence, code, actual):
    """Bind the trusted native final channel to daemon-observed termination only."""
    if not isinstance(final, dict) or type(final.get("success")) is not bool:
        raise ValueError("Native completion frame is missing or invalid.")
    fields = {"type", "success", "tool_calls"}
    if not final["success"]:
        fields.add("error")
    state = actual.get("State") if isinstance(actual, dict) else None
    config = actual.get("Config") if isinstance(actual, dict) else None
    if (
        set(final) != fields
        or final["type"] != "final"
        or type(final["tool_calls"]) is not int
        or final["tool_calls"] != sequence
        or (
            not final["success"]
            and not re.fullmatch(r"coder_[a-z_]+", str(final.get("error", "")))
        )
        or type(code) is not int
        or code != (0 if final["success"] else 1)
        or not isinstance(state, dict)
        or not isinstance(config, dict)
        or config.get("Image") != image
        or actual.get("Name") != "/" + name
        or not re.fullmatch(r"[a-f0-9]{64}", str(actual.get("Id", "")))
        or state.get("Status") != "exited"
        or state.get("Running") is not False
        or state.get("Dead") is not False
        or state.get("OOMKilled") is not False
        or state.get("Error") != ""
        or state.get("ExitCode") != code
    ):
        raise ValueError("Native completion lacks consistent lifecycle evidence.")
    return {
        "container_id": actual["Id"],
        "image": image,
        "exit_code": code,
        "tool_calls": sequence,
        "final_sha256": hashlib.sha256(
            json.dumps(final, sort_keys=True).encode()
        ).hexdigest(),
    }


def run_inference(
    image, network, gateway_key, request, dispatch, *, name=None, process_event=None
):
    validate_request(request)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Native inference requires an immutable image identity.")
    if not gateway_key:
        raise ValueError("Coding inference requires model access.")
    name = name or "emuru-coder-inference-" + uuid.uuid4().hex
    if not re.fullmatch(r"emuru-coder-[a-z0-9-]{8,80}", name):
        raise ValueError("Native inference process identity is invalid.")
    seconds = request.get("deadline_seconds", 600)
    command = [
        "docker",
        "run",
        "-i",
        "--name",
        name,
        "--network",
        network,
        "--read-only",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "64",
        "--cpus",
        "1",
        "--log-driver",
        "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,nr_inodes=1024,mode=1777",
        "--tmpfs",
        "/dev/shm:ro,noexec,nosuid,nodev,size=1m",
        "--env",
        "EMURU_GATEWAY_KEY",
        "--env",
        "HOME=/tmp",
        "--env",
        "HERMES_DISABLE_LAZY_INSTALLS=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "/usr/bin/timeout",
        image,
        "--signal=KILL",
        str(seconds),
        "/opt/hermes/.venv/bin/python",
        "-I",
        "-B",
        "-c",
        (
            "import sys; sys.path.insert(0,'/opt/emuru/src'); "
            "from emuru.projects.coder_native import main; raise SystemExit(main())"
        ),
    ]
    docker_environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    if process_event:
        process_event(name, "starting")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={
            **docker_environment,
            "EMURU_GATEWAY_KEY": gateway_key,
        },
    )
    deadline, buffer, sequence, final = time.monotonic() + seconds, bytearray(), 0, None
    try:
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as poller:
            poller.register(process.stdout, selectors.EVENT_READ)
            while poller.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native coding inference exceeded its deadline.")
                for key, _ in poller.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        poller.unregister(key.fileobj)
                        break
                    buffer.extend(chunk)
                    if len(buffer) > MAX_LINE:
                        raise ValueError("Native coding protocol exceeded its bound.")
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        event = json.loads(line)
                        if final is not None or not isinstance(event, dict):
                            raise ValueError(
                                "Native coding protocol ended or is invalid."
                            )
                        if event.get("type") == "final":
                            if type(event.get("success")) is not bool:
                                raise ValueError(
                                    "Native coding final outcome is invalid."
                                )
                            final = event
                            continue
                        sequence += 1
                        if (
                            set(event) != {"type", "id", "name", "arguments"}
                            or event["type"] != "tool"
                            or event["id"] != sequence
                        ):
                            raise ValueError(
                                "Native coding request correlation failed."
                            )
                        validate_call(event["name"], event["arguments"])
                        result = dispatch(event["name"], event["arguments"])
                        reply = (
                            json.dumps({"id": sequence, "result": result}).encode()
                            + b"\n"
                        )
                        if len(reply) > MAX_LINE or time.monotonic() >= deadline:
                            raise ValueError("Native coding reply exceeded its bounds.")
                        process.stdin.write(reply)
                        process.stdin.flush()
        if buffer or final is None:
            raise ValueError("Native coding final outcome is missing.")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        inspected = bounded_process(
            ["docker", "inspect", name], b"", 5, 65536, env=docker_environment
        )
        if inspected["exit_code"]:
            raise ValueError("Native coding lifecycle inspection unavailable.")
        actual = json.loads(inspected["stdout"])
        if not isinstance(actual, list) or len(actual) != 1:
            raise ValueError("Native coding lifecycle inspection is ambiguous.")
        lifecycle = verify_native_completion(
            image, name, final, sequence, code, actual[0]
        )
        outcome = {
            "success": final["success"],
            "tool_calls": sequence,
            "container": name,
            "exit_code": code,
            "model_led": True,
            "error": final.get("error")
            if re.fullmatch(r"coder_[a-z_]+", str(final.get("error")))
            else None,
            "native_lifecycle": lifecycle,
        }
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        removed = bounded_process(
            ["docker", "rm", "--force", name], b"", 10, 4096, env=docker_environment
        )
        absent = bounded_process(
            ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
            b"",
            5,
            4096,
            env=docker_environment,
        )
        if removed["exit_code"] or absent["exit_code"] or absent["stdout"].strip():
            raise RuntimeError("Native coding inference termination is uncertain.")
        if process_event:
            process_event(name, "stopped")
    outcome["native_lifecycle"]["removed"] = True
    return outcome
