"""Profile-wide service fence; callers hold the service lifecycle lock."""

from contextlib import contextmanager
from pathlib import Path

from emuru.projects import coder_board, supervisor
from emuru.projects.dispatcher import dispatcher_lock
from emuru.projects.supervisor_scout import unresolved

FENCE_SCHEMA = 1


def fenced(records):
    return next(
        (r["active"] for r in reversed(records) if r["kind"] == "sleep-fence"), False
    )


def require_admission(records):
    if fenced(records):
        raise ValueError("Profile sleep fence active; no new stage admitted.")


def validate_record(value):
    if (
        set(value) != {"schema", "kind", "generation", "previous", "active"}
        or type(value["active"]) is not bool
    ):
        raise ValueError("Supervisor sleep fence invalid; preserve newest state.")


def fence(home):
    if not supervisor.enabled(home):
        return
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        if not fenced(records):
            supervisor._append(
                Path(home) / "emuru-projects/supervisor",
                records,
                size,
                {"kind": "sleep-fence", "active": True},
                emergency=True,
            )


def snapshot(home):
    with supervisor._locked(home) as (parent, size, marker):
        return supervisor._records(parent, size, marker)[0]


def check_compatible(home, root):
    """Check newest facts before an executor may synchronize or mutate its profile."""
    if not supervisor.enabled(home):
        return
    snapshot(home)
    source = Path(root) / "src/emuru/projects/supervisor_lifecycle.py"
    if not source.is_file() or "FENCE_SCHEMA = 1\n" not in source.read_text():
        raise ValueError(
            "Supervisor state incompatible; preserve newest state and refuse rollback."
        )


def require_absent(kind, identity):
    from emuru.projects.coder_sandbox import bounded_process

    command = [
        "docker",
        *(["network"] if kind == "network" else []),
        "inspect",
        identity,
    ]
    result = bounded_process(command, b"", 5, 65536)
    message = result["stderr"].strip().casefold()
    nouns = ("network",) if kind == "network" else ("object", "container")
    missing = {
        text
        for noun in nouns
        for text in (
            f"error: no such {noun}: {identity}",
            f"error response from daemon: no such {noun}: {identity}",
            f"error response from daemon: {noun} {identity} not found",
        )
    }
    if not result["exit_code"] or message not in missing:
        raise ValueError("Exact project resource stop unproven; sleep fence retained.")


def inspect_idle(home, root, records):
    """A free lock alone proves nothing about a dead launcher's surviving work."""
    if unresolved(records):
        raise ValueError("Project work active or uncertain; sleep fence retained.")
    for stage in records:
        if (
            stage["kind"] not in {"stage", "coder-stage", "reviewer-stage"}
            or stage["phase"] != "admitted"
        ):
            continue
        if not coder_board.launcher_stopped(stage["launcher"]):
            raise ValueError(
                "Project launcher active or uncertain; sleep fence retained."
            )
        events = [r for r in records if r.get("admission") == stage["generation"]]
        if stage["kind"] == "stage":
            from emuru.projects.supervisor_scout import all_stopped, projection

            stopped = all_stopped(records, stage)
        else:
            from emuru.projects.supervisor_coder import projection, resources_stopped

            stopped = resources_stopped(events)
        if not stopped:
            raise ValueError("Project resources uncertain; sleep fence retained.")
        for event in events:
            if event["phase"] == "client-bound" and not coder_board.launcher_stopped(
                event["process"]
            ):
                raise ValueError("Project client active; sleep fence retained.")
            if event["phase"] == "created":
                require_absent("container", event["container_id"])
            elif event["phase"] == "network-created":
                require_absent("network", event["network_id"])
        enrollment = next(r for r in records if r["generation"] == stage["enrollment"])
        detail = projection(home, root, records, enrollment)
        if detail.get("execution") != "finished":
            raise ValueError("Project outcome uncertain; sleep fence retained.")
    coder_board.ensure_idle(home, root, None)


@contextmanager
def sleeping(home, root):
    """Fence before any inspection; never wait for the lifetime lock."""
    if not supervisor.enabled(home):
        yield
        return
    fence(home)
    with dispatcher_lock(Path(home)):
        records = snapshot(home)
        inspect_idle(home, root, records)
        yield


def wake_inspection(home, root):
    """Read only, including when diagnostics must retain uncertain ownership."""
    if not supervisor.enabled(home):
        return None
    check_compatible(home, root)
    try:
        with dispatcher_lock(Path(home)):
            inspect_idle(home, root, snapshot(home))
    except (ValueError, OSError) as error:
        return f"Project work not proven idle; sleep fence retained ({type(error).__name__})."
    return None


def clear_after_wake(home, root, *, image):
    if not supervisor.enabled(home):
        return None
    check_compatible(home, root)
    try:
        with dispatcher_lock(Path(home)):
            records = snapshot(home)
            if not fenced(records):
                return None
            # Source/pin/schema must match before clearing any restriction.
            for enrollment in (r for r in records if r["kind"] == "enrollment"):
                if enrollment["identity"] != supervisor.identity(root, image):
                    raise ValueError("Supervisor source/image/pin/schema incompatible.")
            inspect_idle(home, root, records)
            with supervisor._locked(home, write=True) as (parent, size, marker):
                current, size = supervisor._records(parent, size, marker)
                if current != records:
                    raise ValueError("Supervisor changed during wake inspection.")
                supervisor._append(
                    Path(home) / "emuru-projects/supervisor",
                    current,
                    size,
                    {"kind": "sleep-fence", "active": False},
                )
    except (ValueError, OSError) as error:
        return f"Project work not proven idle; sleep fence retained ({type(error).__name__})."
    return None
