"""The CI workflow, checked here rather than by pushing and looking.

Every run of `ci.yml` from the day it was added failed, and the API said
`"conclusion": "failure"` with an empty job list — which reads like the tests
broke and is not that at all. GitHub had refused to start the run:

    (Line: 168, Col: 16): Unrecognized named-value: 'matrix'.
    Located at position 1 within expression: matrix.shell

`shell:` on a step is one of the few keys GitHub evaluates before the matrix
exists, so `shell: ${{ matrix.shell }}` is not a thing that can work. Nothing
on this machine said so: the file is valid YAML, it reads sensibly, and the
error only exists inside GitHub's own expression checker.

So these tests encode the part of that checker that bit us. They are not a
general workflow validator — actionlint is that, and it is a Go binary this
project does not otherwise need. They answer one question: does a workflow
here use a context in a key that cannot see it?
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = sorted(
    (Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml")
)

# Keys evaluated before a job's matrix is expanded. A `${{ matrix.… }}` in one
# of these is rejected when the workflow is parsed, not when the job runs.
BEFORE_THE_MATRIX = frozenset({"shell", "if", "continue-on-error"})

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
CONTEXT = re.compile(r"\b([a-z-]+)\s*\.")


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_there_is_a_workflow_to_check() -> None:
    # A glob that silently matches nothing would make every test below pass.
    assert WORKFLOWS, "no workflows found under .github/workflows"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_workflow_is_yaml_with_jobs_that_have_steps(path: Path) -> None:
    document = load(path)
    assert isinstance(document, dict)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} defines no jobs"
    for name, job in jobs.items():
        assert "runs-on" in job or "uses" in job, f"{name} runs nowhere"
        if "uses" not in job:
            assert job.get("steps"), f"{name} has no steps"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_step_takes_its_shell_from_the_matrix(path: Path) -> None:
    """The exact mistake that made twelve runs fail before one job started."""
    for job_name, job in load(path).get("jobs", {}).items():
        for step in job.get("steps") or []:
            for key in BEFORE_THE_MATRIX:
                value = step.get(key)
                if not isinstance(value, str):
                    continue
                for expression in EXPRESSION.findall(value):
                    contexts = set(CONTEXT.findall(expression))
                    assert "matrix" not in contexts, (
                        f"{path.name}: job {job_name!r} uses matrix in "
                        f"{key!r}, which GitHub evaluates before the matrix "
                        "exists. The run will not start."
                    )
