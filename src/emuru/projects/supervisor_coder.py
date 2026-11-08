"""One enrolled coder stage; shared durable accounting and owner controls."""

import hashlib
import json
import os
import re
import signal
import time
import uuid
from pathlib import Path

from emuru.projects import coder_board, supervisor
from emuru.projects.coder import validate_grant
from emuru.projects.scout import digest, encoded
from emuru.projects.supervisor_controls import control_state
from emuru.projects.supervisor_scout import (
    PROJECT_LIMITS,
    launcher_identity,
    reserved,
    unresolved,
)

MAX_EVENTS = 384
STOP_EVENTS = 96
# Covers one unwind of three active containers/two networks; removed IDs stay removed.
EVENT_BYTES = 1024
METADATA_RESERVATION = MAX_EVENTS * EVENT_BYTES
DEADLINE_EXEC = (
    "import os,sys,time; "
    "remaining=(int(sys.argv[1])-time.monotonic_ns())/1e9; "
    "sys.exit(124) if remaining<=0 else None; "
    "os.execv('/usr/bin/timeout',['timeout','--signal=KILL',str(remaining),*sys.argv[2:]])"
)


def allowance(grant):
    limits = validate_grant(grant)["limits"]
    return {
        **dict.fromkeys(supervisor.COUNTERS, 0),
        "coder_runs": 1,
        "active_seconds": limits["attempt_seconds"],
        "coder_iterations": limits["model_iterations"],
        "checks": limits["check_executions"],
        "output_bytes": limits["total_output_bytes"],
        "artifact_bytes": limits["artifact_bytes"],
    }


def validate_record(value, records):
    common = {
        "schema",
        "generation",
        "previous",
        "kind",
        "project",
        "task",
        "enrollment",
        "phase",
    }
    if len(encoded(value)) > EVENT_BYTES:
        raise ValueError("Coder lifecycle receipt capacity exhausted.")
    if value.get("phase") != "admitted":
        return validate_event(value, records, common)
    if set(value) != common | {
        "grant_sha256",
        "reserved",
        "started_ns",
        "deadline_ns",
        "launcher",
        "image",
    }:
        raise ValueError("Unsupported coder lifecycle record.")
    from emuru.projects.supervisor_scout import _valid_process

    enrollment = next(
        (
            r
            for r in records
            if r["kind"] == "enrollment" and r["generation"] == value["enrollment"]
        ),
        None,
    )
    if (
        enrollment is None
        or enrollment["stage"] != "coder"
        or any(enrollment[k] != value[k] for k in ("project", "task", "grant_sha256"))
        or value["image"] != enrollment["identity"]["image"]
        or not _valid_process(value["launcher"])
        or any(type(value[k]) is not int for k in ("started_ns", "deadline_ns"))
        or not 0 < value["deadline_ns"] - value["started_ns"] <= 600 * 10**9
        or not isinstance(value["reserved"], dict)
        or set(value["reserved"]) != set(supervisor.COUNTERS)
        or any(
            type(v) is not int or not 0 <= v <= PROJECT_LIMITS[k]
            for k, v in value["reserved"].items()
        )
        or value["reserved"]["coder_runs"] != 1
        or any(
            value["reserved"][k]
            for k in ("reviewer_runs", "scout_runs", "reviewer_iterations")
        )
        or value["reserved"]["active_seconds"] * 10**9
        != value["deadline_ns"] - value["started_ns"]
    ):
        raise ValueError("Coder reservation or enrollment binding invalid.")
    from emuru.projects.coder import LIMITS

    for counter, limit in (
        ("coder_iterations", "model_iterations"),
        ("checks", "check_executions"),
        ("output_bytes", "total_output_bytes"),
        ("artifact_bytes", "artifact_bytes"),
    ):
        if not 1 <= value["reserved"][counter] <= LIMITS[limit]:
            raise ValueError("Coder per-stage reservation exceeds approved ceiling.")


def validate_event(value, records, common):
    from emuru.projects.supervisor_scout import _valid_process

    extensions = {
        "claim-started": set(),
        "claimed": {"native_task", "run"},
        "planned": {"name", "service", "image"},
        "created": {"name", "container_id"},
        "container-stopped": {"name", "container_id"},
        "client-planned": {"name", "command_sha256", "container_id", "maintenance"},
        "client-bound": {"name", "process"},
        "client-stopped": {"name", "process"},
        "client-unstarted": {"name"},
        "network-planned": {"name"},
        "network-created": {"name", "network_id"},
        "network-stopped": {"name", "network_id"},
        "model-admitted": {"count"},
        "check-admitted": {"count"},
        "stop-observed": {"observed_ns"},
        "completion-started": {"native_task", "run", "evidence_sha256"},
        "verified": {"native_task", "run", "evidence_sha256"},
        "awaiting-review": {"native_task", "run", "evidence_sha256"},
        "stopped": {"native_task", "run", "observer"},
    }
    phase = value.get("phase")
    if (
        phase not in extensions
        or set(value) != common | {"admission"} | extensions[phase]
    ):
        raise ValueError("Invalid coder lifecycle record.")
    stage = next(
        (
            r
            for r in records
            if r["kind"] == "coder-stage"
            and r["phase"] == "admitted"
            and r["generation"] == value["admission"]
        ),
        None,
    )
    if stage is None or any(
        value[k] != stage[k] for k in ("project", "task", "enrollment")
    ):
        raise ValueError("Coder lifecycle admission binding changed.")
    prior = [r for r in records if r.get("admission") == stage["generation"]]
    if len(prior) + 1 >= MAX_EVENTS or any(
        r["phase"] in {"verified", "stopped", "awaiting-review"} for r in prior
    ):
        raise ValueError("Coder lifecycle terminal or capacity exhausted.")
    if "name" in value and (
        not isinstance(value["name"], str)
        or not re.fullmatch("emuru-coder-[a-z0-9-]{8,100}", value["name"])
    ):
        raise ValueError("Invalid coder resource name.")
    if phase == "planned" and (
        value["service"] not in {"gateway", "inference", "check"}
        or not re.fullmatch("sha256:[0-9a-f]{64}", value["image"])
        or len(prior) + 16 >= MAX_EVENTS - STOP_EVENTS
    ):
        raise ValueError("Coder resource or remaining stop capacity invalid.")
    if (
        "container_id" in value
        and not (phase == "client-planned" and value["container_id"] is None)
        and (
            not isinstance(value["container_id"], str)
            or not re.fullmatch("[0-9a-f]{64}", value["container_id"])
        )
    ):
        raise ValueError("Invalid exact coder container identity.")
    if phase == "client-planned" and (
        not isinstance(value["command_sha256"], str)
        or not re.fullmatch("[0-9a-f]{64}", value["command_sha256"])
    ):
        raise ValueError("Invalid coder child command binding.")
    if phase == "client-planned":
        if type(value["maintenance"]) is not bool:
            raise ValueError("Coder maintenance intent invalid.")
        ceiling = MAX_EVENTS - 4 if value["maintenance"] else MAX_EVENTS - STOP_EVENTS
        if len(prior) + 3 >= ceiling:
            raise ValueError(
                "Coder lifecycle capacity exhausted; stop/cleanup reserve retained."
            )
    if "network_id" in value and (
        not isinstance(value["network_id"], str)
        or not re.fullmatch("[0-9a-f]{64}", value["network_id"])
    ):
        raise ValueError("Invalid exact coder network identity.")
    predecessor = {
        "created": "planned",
        "container-stopped": "created",
        "client-bound": "client-planned",
        "client-stopped": "client-bound",
        "network-created": "network-planned",
        "network-stopped": "network-created",
        "client-unstarted": "client-planned",
    }.get(phase)
    if predecessor and not any(
        r["phase"] == predecessor
        and r["name"] == value["name"]
        and all(
            r.get(k) == value[k]
            for k in ("container_id", "network_id", "process")
            if k in value and k in r
        )
        for r in prior
    ):
        raise ValueError("Coder resource lacks exact prior identity.")
    if phase in {"client-bound", "client-stopped"} and not _valid_process(
        value["process"]
    ):
        raise ValueError("Invalid coder client identity.")
    if phase in {"claimed", "verified", "awaiting-review", "completion-started"} and (
        value["native_task"] != stage["task"]
        or type(value["run"]) is not int
        or value["run"] <= 0
    ):
        raise ValueError("Invalid coder native run.")
    if phase == "claimed" and not any(r["phase"] == "claim-started" for r in prior):
        raise ValueError("Coder claim lacks durable intent.")
    if phase in {"model-admitted", "check-admitted"}:
        if len(prior) + 1 >= MAX_EVENTS - STOP_EVENTS:
            raise ValueError(
                "Coder lifecycle capacity exhausted; stop/cleanup reserve retained."
            )
        counter = "coder_iterations" if phase == "model-admitted" else "checks"
        count = sum(r["phase"] == phase for r in prior)
        if (
            type(value["count"]) is not int
            or value["count"] != count + 1
            or value["count"] > stage["reserved"][counter]
        ):
            raise ValueError("Coder mandatory call/check counter invalid or exhausted.")
    if phase == "stop-observed" and (
        type(value["observed_ns"]) is not int
        or value["observed_ns"] < stage["started_ns"]
    ):
        raise ValueError("Invalid coder cancellation observation.")
    if phase in {"verified", "awaiting-review"}:
        if not re.fullmatch(
            "[0-9a-f]{64}", value["evidence_sha256"]
        ) or not resources_stopped(prior):
            raise ValueError("Coder completion lacks exact resource stop proof.")
        if not any(r["phase"] == "claimed" and r["run"] == value["run"] for r in prior):
            raise ValueError("Coder completion native run changed.")
    if phase == "stopped" and (
        not _valid_process(value["observer"])
        or value["observer"] == stage["launcher"]
        or not coder_board.launcher_stopped(stage["launcher"])
        or not resources_stopped(prior)
        or value["native_task"] != stage["task"]
        or (
            value["run"] is not None
            and (
                type(value["run"]) is not int
                or value["run"] <= 0
                or not any(
                    r["phase"] == "claimed" and r["run"] == value["run"] for r in prior
                )
            )
        )
    ):
        raise ValueError(
            "Independent coder launcher/client/container stop proof required."
        )
    if phase not in {"model-admitted", "check-admitted"} and any(
        r["phase"] == phase and r.get("name") == value.get("name") for r in prior
    ):
        raise ValueError("Coder lifecycle operation already attempted.")


def resources_stopped(events):
    return (
        all(
            any(
                r["phase"] == "container-stopped" and r["name"] == p["name"]
                for r in events
            )
            for p in events
            if p["phase"] == "planned"
        )
        and all(
            any(
                r["phase"] == "network-stopped" and r["name"] == p["name"]
                for r in events
            )
            for p in events
            if p["phase"] == "network-planned"
        )
        and all(
            any(
                r["phase"] in {"client-stopped", "client-unstarted"}
                and r["name"] == p["name"]
                and (
                    r["phase"] == "client-unstarted"
                    or coder_board.launcher_stopped(r["process"])
                )
                for r in events
            )
            for p in events
            if p["phase"] == "client-planned"
        )
    )


def event(home, stage, phase, **fields):
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        original = next(
            (r for r in records if r["generation"] == stage["generation"]), None
        )
        if original != {k: v for k, v in stage.items() if k != "admission"}:
            raise ValueError("Coder admitted binding changed.")
        return supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "coder-stage",
                "phase": phase,
                "project": stage["project"],
                "task": stage["task"],
                "enrollment": stage["enrollment"],
                "admission": stage["generation"],
                **fields,
            },
        )[0]


def reserve(home, vault, root, grant, image):
    """Full stage charge before intent/claim; never refunded or replayed."""
    validate_grant(grant)
    if grant["schema"] not in {1, 2}:
        raise ValueError("Supervised rework unavailable until Ticket 04.")
    coder_board.authorize(home, vault, root, grant["task"], grant, readonly=True)
    coder_board.ensure_idle(home, root, grant["task"])
    identity = supervisor.identity(root, image)
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        enrollment = next(
            (
                r
                for r in reversed(records)
                if r["kind"] == "enrollment" and r["task"] == grant["task"]
            ),
            None,
        )
        if (
            enrollment is None
            or enrollment["stage"] != "coder"
            or enrollment["grant_sha256"] != digest(grant)
            or enrollment["identity"] != identity
            or enrollment["boot_id"] != supervisor.boot_id()
            or supervisor.time.monotonic_ns() >= enrollment["deadline_ns"]
            or control_state(records, enrollment) != "unrestricted"
            or enrollment.get("authority", {}).get("vault")
            != str(Path(vault).absolute())
        ):
            raise ValueError(
                "Current unrestricted, unchanged coder enrollment required."
            )
        if unresolved(records):
            raise ValueError("Prior stage ownership uncertain; no new admission.")
        if any(
            r["kind"] == "coder-stage"
            and r["phase"] == "admitted"
            and r["task"] == grant["task"]
            for r in records
        ):
            raise ValueError("Coder stage already attempted; no replay.")
        charge = allowance(grant)
        used = reserved(records, grant["project"])
        if used["coder_runs"]:
            raise ValueError(
                "Project initial coder already attempted; only separately authorized rework may follow."
            )
        if any(used[k] + charge[k] > PROJECT_LIMITS[k] for k in charge):
            raise ValueError("Project coder budget exhausted; no refund.")
        started = supervisor.time.monotonic_ns()
        return supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "coder-stage",
                "phase": "admitted",
                "project": grant["project"],
                "task": grant["task"],
                "enrollment": enrollment["generation"],
                "grant_sha256": digest(grant),
                "reserved": charge,
                "image": image,
                "started_ns": started,
                "deadline_ns": started + charge["active_seconds"] * 10**9,
                "launcher": launcher_identity(),
            },
        )[0]


def load(home, grant):
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stage = next(
            (
                r
                for r in records
                if r["kind"] == "coder-stage"
                and r["phase"] == "admitted"
                and r["task"] == grant["task"]
            ),
            None,
        )
        if (
            stage is None
            or stage["grant_sha256"] != digest(grant)
            or stage["reserved"] != allowance(grant)
        ):
            raise ValueError("Exact coder admission/grant/reservation required.")
        folder = coder_board.attempt_path(home, grant)
        if grant["schema"] == 2 and folder.exists():
            from emuru.projects.reviewer import check_loop

            check_loop(folder, grant)
            if coder_board._read_json(folder / "coder-stage.json") != {
                "schema": 1,
                "boot_id": stage["launcher"]["boot_id"],
                "started_ns": stage["started_ns"],
                "deadline_ns": stage["deadline_ns"],
            }:
                raise ValueError(
                    "Coder M5 reservation deadline conflicts with supervisor."
                )
        return {**stage, "admission": digest(stage)}


class CoderStage:
    """Host-only capability for this admitted stage, never a worker credential."""

    def __init__(self, home, vault, root, grant):
        self.home, self.vault, self.root, self.grant = home, vault, root, grant
        self.stage = load(home, grant)
        self.deadline = self.stage["deadline_ns"] / 10**9

    def records(self):
        with supervisor._locked(self.home) as (parent, size, marker):
            records, _ = supervisor._records(parent, size, marker)
        if not any(
            r == {k: v for k, v in self.stage.items() if k != "admission"}
            for r in records
        ):
            raise ValueError("Coder admission missing or changed.")
        return records

    def check(self):
        records = self.records()
        enrollment = next(
            r for r in records if r["generation"] == self.stage["enrollment"]
        )
        if control_state(records, enrollment) == "revoked":
            if not any(
                r.get("admission") == self.stage["generation"]
                and r.get("phase") == "stop-observed"
                for r in records
            ):
                self.event("stop-observed", observed_ns=supervisor.time.monotonic_ns())
            raise InterruptedError("Coder stop requested; effects remain uncertain.")
        if (
            self.stage["launcher"]["boot_id"] != supervisor.boot_id()
            or supervisor.time.monotonic_ns() >= self.stage["deadline_ns"]
        ):
            raise TimeoutError("Coder original stage deadline exhausted.")
        if any(
            r.get("admission") == self.stage["generation"]
            and r.get("phase") in {"verified", "stopped", "awaiting-review"}
            for r in records
        ):
            raise ValueError("Coder stage already ended; no replay.")

    def stopped(self):
        self.check()
        return False

    def event(self, phase, **fields):
        return event(self.home, self.stage, phase, **fields)

    def admit(self, kind):
        """Revalidate native/approval authority before each model/tool/check effect."""
        records = self.records()
        claim = next(
            (
                r
                for r in records
                if r.get("admission") == self.stage["generation"]
                and r.get("phase") == "claimed"
            ),
            None,
        )
        if claim is None:
            raise ValueError("Coder native claim binding unavailable.")
        enrollment = next(
            r for r in records if r["generation"] == self.stage["enrollment"]
        )
        if enrollment["identity"] != supervisor.identity(
            self.root, self.stage["image"]
        ):
            raise ValueError("Coder source/image/pin/schema incompatible.")
        coder_board.authorize(
            self.home,
            self.vault,
            self.root,
            self.grant["task"],
            self.grant,
            run_id=claim["run"],
            readonly=True,
        )
        self.check()
        if kind in {"model", "check"}:
            phase = kind + "-admitted"
            count = sum(
                r.get("admission") == self.stage["generation"]
                and r.get("phase") == phase
                for r in records
            )
            self.event(phase, count=count + 1)
        elif kind != "tool":
            raise ValueError("Unknown coder admission kind.")

    def claim(self, folder):
        if self.stage["launcher"] != launcher_identity():
            raise ValueError("Coder original launcher required; no restart replay.")
        # Native claim and owner restrictions serialize; no external waits under this lock.
        with supervisor._locked(self.home, write=True) as (parent, size, marker):
            records, size = supervisor._records(parent, size, marker)
            enrollment = next(
                r for r in records if r["generation"] == self.stage["enrollment"]
            )
            if (
                control_state(records, enrollment) == "revoked"
                or supervisor.time.monotonic_ns() >= self.stage["deadline_ns"]
            ):
                raise ValueError(
                    "Coder stopped or original deadline exhausted before claim."
                )
            if any(
                r.get("admission") == self.stage["generation"]
                and r.get("phase") == "claim-started"
                for r in records
            ):
                raise ValueError("Coder claim already attempted; no replay.")
            value = {
                "kind": "coder-stage",
                "project": self.stage["project"],
                "task": self.stage["task"],
                "enrollment": self.stage["enrollment"],
                "admission": self.stage["generation"],
            }
            _, size = supervisor._append(
                Path(self.home) / "emuru-projects/supervisor",
                records,
                size,
                {**value, "phase": "claim-started"},
            )
            run = coder_board._claim_native(
                self.home, self.root, self.grant["task"], self.grant, folder
            )
            supervisor._append(
                Path(self.home) / "emuru-projects/supervisor",
                records,
                size,
                {
                    **value,
                    "phase": "claimed",
                    "native_task": run["task"],
                    "run": run["run"],
                },
            )
            return run

    def client(self, command, *, maintenance=False):
        name = "emuru-coder-client-" + uuid.uuid4().hex
        self.event(
            "client-planned",
            name=name,
            command_sha256=digest(command),
            maintenance=maintenance,
            container_id=command[-1] if command[:2] == ["docker", "start"] else None,
        )

        def observe(process, phase):
            if phase == "unstarted":
                self.event("client-unstarted", name=name)
                return
            if phase == "bound":
                identity = {
                    "pid": process.pid,
                    "start_time": Path(f"/proc/{process.pid}/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()[19],
                    "boot_id": supervisor.boot_id(),
                }
                observe.identity = identity
            self.event("client-" + phase, name=name, process=observe.identity)

        observe.identity = None
        observe.name = name
        return observe

    def command(
        self,
        command,
        *,
        payload=b"",
        seconds=10,
        output_bytes=65536,
        env=None,
        maintenance=False,
    ):
        from emuru.projects.coder_sandbox import bounded_process

        if not maintenance:
            self.check()
            seconds = min(seconds, self.deadline - time.monotonic())
        observer = self.client(command, maintenance=maintenance)
        return bounded_process(
            command,
            payload,
            seconds,
            output_bytes,
            env=env,
            stopped=None if maintenance else self.stopped,
            process_event=observer,
        )

    def inspect(self, identity, *, maintenance=False):
        observed = self.command(
            ["docker", "inspect", "--format", "{{json .}}", identity],
            maintenance=maintenance,
        )
        if observed["exit_code"]:
            prefix, _, missing = observed["stderr"].strip().rpartition(": ")
            errors = {
                f"{intro}no such {kind}"
                for intro in ("", "error: ", "error response from daemon: ")
                for kind in ("object", "container")
            }
            if missing == identity and prefix.casefold() in errors:
                return None
            raise ValueError(
                "Coder Docker inspection unavailable; ownership uncertain."
            )
        return json.loads(observed["stdout"])

    def bind(self, name, actual):
        plan = next(
            r
            for r in self.records()
            if r.get("admission") == self.stage["generation"]
            and r.get("phase") == "planned"
            and r["name"] == name
        )
        if (
            actual is None
            or actual.get("Image") != plan["image"]
            or actual.get("Name") != "/" + name
            or actual.get("Config", {}).get("Labels", {}).get("io.emuru.coder.stage")
            != self.stage["admission"]
        ):
            raise ValueError(
                "Coder container exact image/name/admission identity changed."
            )
        created = next(
            (
                r
                for r in self.records()
                if r.get("admission") == self.stage["generation"]
                and r.get("phase") == "created"
                and r["name"] == name
            ),
            None,
        )
        if created is None:
            self.event("created", name=name, container_id=actual["Id"])
        elif created["container_id"] != actual["Id"]:
            raise ValueError("Coder container identity replaced; retain ownership.")
        return actual["Id"]

    def create(self, command, name, service, image, *, env=None, deadline_ns=None):
        self.check()
        self.event("planned", name=name, service=service, image=image)
        # A stopped, exactly bound container is recorded before any entrypoint runs.
        command = [*command]
        if "--entrypoint" in command:
            index = command.index("--entrypoint")
            if command[index + 1] == "/usr/bin/timeout":
                tail = command[index + 3 :]
                if tail[:1] == ["--signal=KILL"]:
                    tail = tail[2:]
                elif tail[:2] == ["-s", "KILL"]:
                    tail = tail[3:]
                else:
                    raise ValueError("Coder absolute timeout wrapper changed.")
                absolute = (
                    self.stage["deadline_ns"] if deadline_ns is None else deadline_ns
                )
                if (
                    type(absolute) is not int
                    or not 0 < absolute <= self.stage["deadline_ns"]
                ):
                    raise ValueError("Coder absolute deadline cannot expand.")
                python = (
                    "python" if service == "gateway" else "/opt/emuru/.venv/bin/python"
                )
                command = [
                    *command[:index],
                    "--entrypoint",
                    python,
                    command[index + 2],
                    "-I",
                    "-B",
                    "-c",
                    DEADLINE_EXEC,
                    str(absolute),
                    *tail,
                ]
        command[1] = "create"
        command = [arg for arg in command if arg != "-d"]
        command[2:2] = ["--label", "io.emuru.coder.stage=" + self.stage["admission"]]
        result = self.command(command, env=env)
        if result["exit_code"]:
            raise ValueError("Coder container creation failed; no retry.")
        return self.bind(name, self.inspect(name))

    def remove(self, name):
        if not any(
            r.get("admission") == self.stage["generation"]
            and r.get("phase") == "planned"
            and r["name"] == name
            for r in self.records()
        ):
            return
        records = self.records()
        if any(
            r.get("admission") == self.stage["generation"]
            and r.get("phase") == "container-stopped"
            and r["name"] == name
            for r in records
        ):
            return
        created = next(
            (
                r
                for r in records
                if r.get("admission") == self.stage["generation"]
                and r.get("phase") == "created"
                and r["name"] == name
            ),
            None,
        )
        actual = self.inspect(
            created["container_id"] if created is not None else name, maintenance=True
        )
        if actual is not None:
            identity = self.bind(name, actual)
            result = self.command(
                ["docker", "rm", "--force", identity], maintenance=True
            )
            if (
                result["exit_code"]
                or self.inspect(identity, maintenance=True) is not None
            ):
                raise ValueError("Coder exact container stop unproven.")
        elif created is not None:
            identity = created["container_id"]
        else:
            raise ValueError("Coder container creation/identity uncertain.")
        if not any(
            r.get("admission") == self.stage["generation"]
            and r.get("phase") == "container-stopped"
            and r["name"] == name
            for r in records
        ):
            self.event("container-stopped", name=name, container_id=identity)

    def create_network(self, name, *, internal=False):
        self.event("network-planned", name=name)
        result = self.command(
            [
                "docker",
                "network",
                "create",
                "--label",
                "io.emuru.coder.stage=" + self.stage["admission"],
                *(["--internal"] if internal else []),
                name,
            ],
            seconds=30,
        )
        if result["exit_code"]:
            raise ValueError("Coder network creation failed; inspect intent.")
        self.event("network-created", name=name, network_id=result["stdout"].strip())

    def remove_network(self, name):
        events = [
            r for r in self.records() if r.get("admission") == self.stage["generation"]
        ]
        if any(r["phase"] == "network-stopped" and r["name"] == name for r in events):
            return
        created = next(
            (
                r
                for r in events
                if r["phase"] == "network-created" and r["name"] == name
            ),
            None,
        )
        query = created["network_id"] if created else name

        def inspect(identity):
            result = self.command(
                ["docker", "network", "inspect", identity], maintenance=True
            )
            if result["exit_code"]:
                if result["stderr"].strip().casefold() not in {
                    "error response from daemon: network " + identity + " not found",
                    "error: no such network: " + identity,
                }:
                    raise ValueError("Coder network inspection unavailable.")
                return None
            actual = json.loads(result["stdout"])
            if not isinstance(actual, list) or len(actual) != 1:
                raise ValueError("Coder network inspection ambiguous.")
            return actual[0]

        actual = inspect(query)
        if actual is None and created is None:
            raise ValueError("Coder network creation binding uncertain.")
        if actual is not None:
            if (
                actual.get("Name") != name
                or actual.get("Labels", {}).get("io.emuru.coder.stage")
                != self.stage["admission"]
                or (created and actual["Id"] != query)
            ):
                raise ValueError("Coder exact network identity changed.")
            query = actual["Id"]
            if created is None:
                self.event("network-created", name=name, network_id=query)
            removed = self.command(["docker", "network", "rm", query], maintenance=True)
            if removed["exit_code"] or inspect(query) is not None:
                raise ValueError("Coder network absence unproven.")
        self.event("network-stopped", name=name, network_id=query)


def projection(home, root, records, enrollment, *, inspect=True):
    stage = next(
        (
            r
            for r in records
            if r["kind"] == "coder-stage"
            and r["phase"] == "admitted"
            and r["enrollment"] == enrollment["generation"]
        ),
        None,
    )
    if stage is None:
        return {}
    result = {
        "adapter": "coder",
        "execution": "uncertain",
        "stage_deadline_ns": stage["deadline_ns"],
        "stop_requested": control_state(records, enrollment) == "revoked",
    }
    events = [r for r in records if r.get("admission") == stage["generation"]]
    result["actual"] = {
        "model_calls": sum(r["phase"] == "model-admitted" for r in events),
        "checks": sum(r["phase"] == "check-admitted" for r in events),
    }
    claimed = next((r for r in events if r["phase"] == "claimed"), None)
    if claimed:
        from emuru.projects import board

        folder = coder_board._find_attempt(home, stage["task"])
        if folder is None:
            raise ValueError("Supervised coder attempt missing.")
        grant = coder_board.load_grant(folder / "grant.json")
        with board.project_board_readonly(home, root) as (kb, db):
            card = kb.get_task(db, stage["task"])
            observed = native_status(home, root, grant, card, kb, db, inspect=inspect)
        if observed["status"] == "tested local result / awaiting owner review":
            result.update(execution="finished", verified_outcome=observed["status"])
        elif observed["status"] == "awaiting_review":
            result.update(
                execution="awaiting_review",
                verified_outcome="checked coder evidence; independent review unavailable",
            )
        elif observed["status"] == "running":
            result["execution"] = "running"
    if any(r["phase"] in {"planned", "stopped"} for r in events) and resources_stopped(
        events
    ):
        result["stop_proof"] = "recorded exact client/container stop"
    return result


def evidence(stage, folder, run):
    from emuru.projects.coder_runtime import _read_private, verify_evidence

    data = _read_private(
        Path(folder) / "evidence.json", stage.grant["limits"]["artifact_bytes"]
    )
    sha = hashlib.sha256(data).hexdigest()
    report = verify_evidence(folder, sha, task_run=run)
    if report["image"] != stage.stage["image"]:
        raise ValueError("Coder evidence image differs from admission.")
    records = stage.records()
    events = [r for r in records if r.get("admission") == stage.stage["generation"]]
    checks = sum(r["phase"] == "check-admitted" for r in events)
    calls = sum(r["phase"] == "model-admitted" for r in events)
    if (
        len(report["checks"]) > checks
        or (report["success"] and len(report["checks"]) != checks)
        or (
            "model_calls" in report["native"]
            and report["native"]["model_calls"] != calls
        )
    ):
        raise ValueError(
            "Coder mandatory call/check accounting conflicts with evidence."
        )
    return report, sha, events


def finish(stage, folder, run, *, reconciling=False):
    """Verify existing coder artifacts; schema 2 never claims a reviewer here."""
    report, sha, events = evidence(stage, folder, run)
    if not resources_stopped(events):
        raise ValueError("Coder exact client/container stop unproven.")
    legacy = coder_board._processes(folder)
    if report["success"] and (
        not {"gateway", "inference", "check"} <= {r["kind"] for r in legacy}
        or any(r["phase"] != "stopped" for r in {r["name"]: r for r in legacy}.values())
    ):
        raise ValueError("Coder completion lacks predecessor process observations.")
    if not reconciling:
        stage.check()
    if not report["success"]:
        return {
            "status": "uncertain",
            "task": stage.grant["task"],
            "run": run["run"],
            "detail": "Stopped worker; original launcher stop requires explicit reconciliation; effects uncertain.",
        }
    coder_board.authorize(
        stage.home,
        stage.vault,
        stage.root,
        stage.grant["task"],
        stage.grant,
        run_id=run["run"],
        readonly=True,
    )
    intent = next((r for r in events if r["phase"] == "completion-started"), None)
    if intent is None:
        stage.event(
            "completion-started",
            native_task=run["task"],
            run=run["run"],
            evidence_sha256=sha,
        )
    elif (intent["native_task"], intent["run"], intent["evidence_sha256"]) != (
        run["task"],
        run["run"],
        sha,
    ):
        raise ValueError("Coder completion intent conflicts with evidence.")
    with supervisor._locked(stage.home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        enrollment = next(
            r for r in records if r["generation"] == stage.stage["enrollment"]
        )
        if control_state(records, enrollment) == "revoked":
            raise InterruptedError(
                "Coder stop requested before completion; effects uncertain."
            )
        phase = "awaiting-review" if stage.grant["schema"] == 2 else "verified"
        if phase == "verified":
            result = coder_board._complete_success(
                stage.home,
                stage.root,
                run["task"],
                stage.grant,
                folder,
                run,
                report,
                sha,
            )
        else:
            result = {
                "status": "awaiting_review",
                "task": run["task"],
                "run": run["run"],
                "detail": "Checked coder evidence; independent review not admitted.",
            }
        supervisor._append(
            Path(stage.home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "coder-stage",
                "phase": phase,
                "project": stage.stage["project"],
                "task": stage.stage["task"],
                "enrollment": stage.stage["enrollment"],
                "admission": stage.stage["generation"],
                "native_task": run["task"],
                "run": run["run"],
                "evidence_sha256": sha,
            },
        )
    return result


def recover(home, vault, root, task_id, grant, *, cleanup=False):
    """Explicit exact-resource cleanup or metadata-only reconciliation; no execution."""
    from emuru.projects import board

    stage = CoderStage(home, vault, root, grant)
    records = stage.records()
    enrollment = next(
        r for r in records if r["generation"] == stage.stage["enrollment"]
    )
    if enrollment["identity"] != supervisor.identity(root, stage.stage["image"]):
        raise ValueError("Coder recovery source/image/pin/schema incompatible.")
    if not coder_board.launcher_stopped(stage.stage["launcher"]):
        raise ValueError("Coder launcher may still execute; preserve ownership.")
    events = [r for r in records if r.get("admission") == stage.stage["generation"]]
    final = next(
        (r for r in events if r["phase"] in {"verified", "awaiting-review", "stopped"}),
        None,
    )
    if final is not None:
        with board.project_board_readonly(home, root) as (kb, db):
            return native_status(home, root, grant, kb.get_task(db, task_id), kb, db)
    for plan in (r for r in events if r["phase"] == "client-planned"):
        if any(
            r["phase"] == "client-unstarted" and r["name"] == plan["name"]
            for r in events
        ):
            continue
        bound = next(
            (
                r
                for r in events
                if r["phase"] == "client-bound" and r["name"] == plan["name"]
            ),
            None,
        )
        if bound is None:
            raise ValueError(
                "Coder child creation outcome uncertain; no process-stop proof."
            )
        process = bound["process"]
        if not coder_board.launcher_stopped(process):
            if not cleanup:
                raise ValueError(
                    "Coder recorded child still active; explicit cleanup required."
                )
            fd = os.pidfd_open(process["pid"])
            try:
                if not coder_board.launcher_stopped(process):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
            deadline = time.monotonic() + 1
            while (
                not coder_board.launcher_stopped(process)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            if not coder_board.launcher_stopped(process):
                raise ValueError("Coder exact child stop unproven.")
        if not any(
            r["phase"] == "client-stopped" and r["name"] == plan["name"] for r in events
        ):
            stage.event("client-stopped", name=plan["name"], process=process)
    for plan in (r for r in events if r["phase"] == "planned"):
        if any(
            r["phase"] == "container-stopped" and r["name"] == plan["name"]
            for r in events
        ):
            continue
        if cleanup:
            stage.remove(plan["name"])
        else:
            created = next(
                (
                    r
                    for r in events
                    if r["phase"] == "created" and r["name"] == plan["name"]
                ),
                None,
            )
            if (
                created is None
                or stage.inspect(created["container_id"], maintenance=True) is not None
            ):
                raise ValueError(
                    "Coder container stop unproven; explicit cleanup required."
                )
            if not any(
                r["phase"] == "container-stopped" and r["name"] == plan["name"]
                for r in events
            ):
                stage.event(
                    "container-stopped",
                    name=plan["name"],
                    container_id=created["container_id"],
                )
    for plan in (r for r in events if r["phase"] == "network-planned"):
        if cleanup:
            stage.remove_network(plan["name"])
        elif not any(
            r["phase"] == "network-stopped" and r["name"] == plan["name"]
            for r in events
        ):
            raise ValueError(
                "Coder gateway network cleanup unproven; explicit cleanup required."
            )
    folder = coder_board.attempt_path(home, grant)
    if folder.exists():
        for event in {r["name"]: r for r in coder_board._processes(folder)}.values():
            if (
                not any(
                    r["phase"] == "planned" and r["name"] == event["name"]
                    for r in events
                )
                and stage.inspect(event["name"], maintenance=True) is not None
            ):
                raise ValueError("Coder unbound resource present; preserve ownership.")
            if event["phase"] != "stopped":
                coder_board.process_event(
                    folder, event["name"], event["kind"], "stopped"
                )
        try:
            private = coder_board._read_json(folder / "gateway.json")
        except FileNotFoundError:
            pass
        else:
            coder_board._cleanup_gateway_secrets(root, private)
    if cleanup:
        return {
            "status": "uncertain",
            "detail": "Exact recorded resources stopped; effects uncertain; explicit reconciliation required.",
        }
    with board.project_board_readonly(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        if (
            card is None
            or card.tenant != grant["project"]
            or card.created_by != "emuru-owner"
        ):
            raise ValueError("Coder recovery native project binding changed.")
        binding = json.loads(card.body)
        if (
            any(
                binding.get(k) != grant[k] for k in ("proposal", "revision", "approval")
            )
            or digest(binding.get("task")) != grant["task_digest"]
        ):
            raise ValueError("Coder recovery native task binding changed.")
        latest = kb.latest_run(db, task_id)
        run = None if latest is None else {"task": task_id, "run": latest.id}
        if latest is not None and (
            latest.task_id != task_id or latest.profile != coder_board.CODER
        ):
            raise ValueError("Coder recovery native run incompatible.")
        claim = next((r for r in events if r["phase"] == "claimed"), None)
        if claim and (run is None or claim["run"] != run["run"]):
            raise ValueError("Coder native run superseded; no recovery mutation.")
        if (
            latest is not None
            and claim is None
            and (
                not any(r["phase"] == "claim-started" for r in events)
                or len(kb.list_runs(db, task_id)) != 1
            )
        ):
            raise ValueError("Coder claim outcome cannot be uniquely associated.")
    if run is not None:
        if claim is None:
            stage.event("claimed", native_task=task_id, run=run["run"])
            if not (folder / "run.json").exists():
                coder_board._write_new(folder, "run.json", encoded(run))
        if card.status == "done":
            with board.project_board_readonly(home, root) as (kb, db):
                prior = native_status(
                    home, root, grant, kb.get_task(db, task_id), kb, db
                )
            report, sha, _ = evidence(stage, folder, run)
            stage.event(
                "verified", native_task=task_id, run=run["run"], evidence_sha256=sha
            )
            return prior
        try:
            report, _, _ = evidence(stage, folder, run)
        except FileNotFoundError:
            report = None
        if (
            report
            and report["success"]
            and control_state(stage.records(), enrollment) != "revoked"
        ):
            return finish(stage, folder, run, reconciling=True)
        with board.project_board(home, root) as (kb, db):
            current = kb.get_task(db, task_id)
            if current.status == "running":
                coder_board._require_run(current, task_id, run["run"])
                if not kb.block_task(
                    db,
                    task_id,
                    reason="Exact process stop proven; effects uncertain; no automatic retry.",
                    expected_run_id=run["run"],
                ):
                    raise ValueError("Coder exact-run stop transition refused.")
            elif (
                current.status != "blocked"
                or latest.ended_at is None
                or latest.outcome != "blocked"
            ):
                raise ValueError("Coder recovery native ownership changed.")
    stage.event(
        "stopped",
        native_task=task_id,
        run=None if run is None else run["run"],
        observer=launcher_identity(),
    )
    return {
        "status": "uncertain",
        "task": task_id,
        "run": None if run is None else run["run"],
        "stop": "proven",
        "detail": "Stopped processes; effects uncertain; reservation retained.",
    }


def native_status(home, root, grant, card, kb, db, *, inspect=True):
    """Observational only; a native done flag alone proves no completion."""
    folder = coder_board.attempt_path(home, grant)
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    if not any(
        r["kind"] == "coder-stage" and r["phase"] == "admitted" and r["task"] == card.id
        for r in records
    ):
        return coder_board._status(kb, db, card, home, root, folder, grant)
    stage = CoderStage(home, None, root, grant)
    enrollment = next(
        r for r in records if r["generation"] == stage.stage["enrollment"]
    )
    if enrollment["identity"] != supervisor.identity(root, stage.stage["image"]):
        raise ValueError("Coder source/image/pin/schema incompatible.")
    events = [r for r in records if r.get("admission") == stage.stage["generation"]]
    claim = next((r for r in events if r["phase"] == "claimed"), None)
    if claim is None:
        return {
            "status": "uncertain",
            "detail": "Coder reservation charged; native claim outcome unproven; no replay.",
        }
    run = {"task": card.id, "run": claim["run"]}
    latest = kb.latest_run(db, card.id)
    actual = kb.get_run(db, run["run"])
    if (
        latest is None
        or actual is None
        or latest.id != run["run"]
        or actual.task_id != card.id
        or actual.profile != coder_board.CODER
    ):
        raise ValueError("Coder native run superseded or incompatible.")
    final = next(
        (r for r in events if r["phase"] in {"verified", "awaiting-review", "stopped"}),
        None,
    )
    if grant["schema"] == 1 and card.status == "done":
        report, sha, _ = evidence(stage, folder, run)
        if (
            not report["success"]
            or not resources_stopped(events)
            or (final and final.get("evidence_sha256") != sha)
        ):
            raise ValueError("Coder final evidence changed.")
        return coder_board._status(kb, db, card, home, root, folder, grant)
    if final and final["phase"] == "awaiting-review":
        report, sha, _ = evidence(stage, folder, run)
        if (
            not report["success"]
            or sha != final["evidence_sha256"]
            or card.current_run_id != run["run"]
            or card.status != "running"
        ):
            raise ValueError("Coder awaiting-review evidence/native binding changed.")
        return {
            "status": "awaiting_review",
            "task": card.id,
            "run": run["run"],
            "detail": "Checked coder evidence; independent review not admitted.",
        }
    if (
        inspect
        and card.status == "running"
        and card.current_run_id == run["run"]
        and not coder_board.launcher_stopped(stage.stage["launcher"])
    ):
        from emuru.projects.coder_sandbox import bounded_process

        for created in (r for r in events if r["phase"] == "created"):
            if any(
                r["phase"] == "container-stopped" and r["name"] == created["name"]
                for r in events
            ):
                continue
            inspected = bounded_process(
                ["docker", "inspect", created["container_id"]], b"", 1, 65536
            )
            if inspected["exit_code"]:
                continue
            rows = json.loads(inspected["stdout"])
            plan = next(
                r
                for r in events
                if r["phase"] == "planned" and r["name"] == created["name"]
            )
            if (
                len(rows) == 1
                and rows[0].get("Id") == created["container_id"]
                and rows[0].get("Image") == plan["image"]
                and rows[0]
                .get("Config", {})
                .get("Labels", {})
                .get("io.emuru.coder.stage")
                == stage.stage["admission"]
                and rows[0].get("State", {}).get("Running") is True
            ):
                return {"status": "running", "task": card.id, "run": run["run"]}
    return {
        "status": "uncertain",
        "task": card.id,
        "run": run["run"],
        "detail": "Coder effects uncertain; inspect exact processes; no replay.",
    }
