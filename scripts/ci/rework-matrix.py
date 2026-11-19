# Run via: bash scripts/ci/as-coder.sh - < scripts/ci/rework-matrix.py
# (fed on stdin so the working directory, not this file's directory, is on sys.path,
# exactly as the original inline heredoc behaved).
import os
import runpy
from functools import partialmethod
from pathlib import Path
from unittest.mock import patch

from emuru.projects.coder_sandbox import DockerSandbox

# act's copied workspace differs from the daemon's bind source.
daemon_root = runpy.run_path("scripts/hermes/container-check.py")["docker_workspace"](
    Path.cwd()
)
mapped = partialmethod(DockerSandbox.__init__, daemon_root=daemon_root)
with patch.object(DockerSandbox, "__init__", mapped):
    runpy.run_path("scripts/hermes/rework-check.py")["run"](
        os.environ["EMURU_TEST_IMAGE"], matrix=True
    )
