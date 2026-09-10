"""The V1 workflow templates, checked against the code that dispatches them.

NEH-1193: no project in the fleet had a `runtests.yml` a v1 dispatch could
target, so every real `/testauto` resolved to a workflow that does not exist.
The templates in `examples/workflows/` are the fix for that; this file is what
stops them rotting.

## Why these assertions and not others

Three things about a dispatch target fail SILENTLY or MISLEADINGLY, and each
has already cost this fleet something:

1. **An undeclared input is a 422**, which reads as a GitHub fault rather than
   as a workflow one line out of date (NEH-1152). So the declared inputs are
   compared against `runners/github.py`'s own dict — READ FROM THE CODE, not
   restated here. A test that restates the list agrees with itself.

2. **The wrong `runs-on` produces a job that never STARTS**, not one that
   fails. The V2 file parks work for `[self-hosted, linux, testlab]`; a V1
   template carrying that label queues forever against a runner nothing answers
   to. NEH-1193 calls this the load-bearing point, so it is asserted in both
   directions: the templates must be `ubuntu-latest` AND the V2 file must still
   be self-hosted, so nobody "fixes" it into uselessness.

3. **`${{ }}` inside a `run:` block is remote code execution.** GitHub
   substitutes textually, before any shell parses it. The V2 file documents
   this at length and the templates copy the control; this asserts they kept
   it.

Plus the filenames, which are not decoration: `DEFAULT_WORKFLOW` maps a
language to one of these exact names, so a rename here breaks dispatch with no
local symptom at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from slack_runtests.store.base import DEFAULT_WORKFLOW

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO / "examples" / "workflows"
V2 = REPO / ".github" / "workflows" / "runtests.yml"

#: The workflow filenames the edge actually dispatches to, taken from the
#: mapping rather than typed out. If a language is added, this test starts
#: demanding a template for it — which is the intent.
EXPECTED = sorted(DEFAULT_WORKFLOW.values())


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on_block(doc: dict) -> dict:
    # PyYAML resolves the bare key `on` to the BOOLEAN True (YAML 1.1), so
    # `doc["on"]` is a KeyError on a workflow that is perfectly valid. This has
    # caught out enough people to be worth naming.
    return doc.get("on") or doc.get(True)


def _dispatched_inputs() -> set[str]:
    """The input names `runners/github.py` sends, read from the source.

    Parsed rather than imported because building them means calling the
    dispatcher, which wants a repo, a token and an event loop. The regex is
    anchored to the `inputs = {` literal, and the count is asserted below so a
    regex that silently matches nothing cannot pass.
    """
    src = (REPO / "src" / "slack_runtests" / "runners" / "github.py").read_text()
    block = re.search(r"inputs = \{(.*?)\n    \}", src, re.S)
    assert block, "could not find the `inputs = {...}` literal in runners/github.py"
    return set(re.findall(r'"([a-z_]+)":', block.group(1)))


def test_the_dispatched_input_set_is_readable_and_not_empty() -> None:
    """The control for every assertion below.

    A regex that matched nothing would make `test_every_dispatched_input_is_declared`
    pass vacuously — the empty set is a subset of everything.
    """
    names = _dispatched_inputs()
    assert len(names) == 8, f"expected 8 dispatched inputs, got {sorted(names)}"
    assert "correlation_id" in names and "slack_user" in names


@pytest.mark.parametrize("name", EXPECTED)
def test_a_template_exists_for_every_language_the_edge_can_dispatch(name: str) -> None:
    assert (TEMPLATE_DIR / name).is_file(), (
        f"DEFAULT_WORKFLOW dispatches to {name!r} and examples/workflows/{name} "
        "does not exist, so that language resolves to nothing"
    )


@pytest.mark.parametrize("name", EXPECTED)
def test_every_dispatched_input_is_declared(name: str) -> None:
    declared = set(_on_block(_load(TEMPLATE_DIR / name))["workflow_dispatch"]["inputs"])
    missing = _dispatched_inputs() - declared
    assert not missing, (
        f"{name} does not declare {sorted(missing)}; workflow_dispatch answers 422, "
        "which reads as a GitHub fault rather than a stale workflow"
    )


@pytest.mark.parametrize("name", EXPECTED)
def test_a_v1_template_runs_on_a_hosted_runner(name: str) -> None:
    runs_on = _load(TEMPLATE_DIR / name)["jobs"]["run"]["runs-on"]
    assert runs_on == "ubuntu-latest", (
        f"{name} declares runs-on {runs_on!r}. A v1 dispatch WAITS for the run; a "
        "self-hosted label nothing answers to queues forever, and the symptom is a "
        "job that never starts rather than one that fails"
    )


def test_the_v2_workflow_is_still_self_hosted() -> None:
    """The other direction, and the reason it is here.

    NEH-1193's warning was that the existing file is the WRONG template to
    copy. The mirror risk is somebody reading that as "this file is wrong" and
    changing it. It is correct for what it is: V2 parks work for an enrolled
    lab runner.
    """
    runs_on = _load(V2)["jobs"]["run"]["runs-on"]
    assert runs_on == ["self-hosted", "linux", "testlab"], runs_on


@pytest.mark.parametrize("name", EXPECTED + ["../../.github/workflows/runtests.yml"])
def test_no_input_is_interpolated_into_a_run_block(name: str) -> None:
    """`${{ }}` in a `run:` is substituted TEXTUALLY, before any shell parses it.

    Checked on the parsed document rather than by grepping the file, so the
    long comment blocks that EXPLAIN the hazard — and necessarily contain the
    dangerous form — cannot make this pass or fail for the wrong reason. That
    is the same trap this fleet hit with a leak guard whose plant landed in a
    comment the stripper removed.
    """
    doc = _load(TEMPLATE_DIR / name)
    for step in doc["jobs"]["run"]["steps"]:
        script = step.get("run")
        if not script:
            continue
        assert "${{" not in script, (
            f"{name}: step {step.get('name') or step.get('uses')!r} interpolates "
            f"an expression into its script:\n{script}"
        )


@pytest.mark.parametrize("name", EXPECTED)
def test_every_dispatched_input_reaches_the_job_through_env(name: str) -> None:
    """The positive half of the control above.

    Banning `${{ }}` from `run:` blocks is satisfied trivially by a workflow
    that ignores its inputs entirely. This asserts the values actually arrive —
    through `env:`, which is the mechanism that makes them data rather than
    script.
    """
    env = _load(TEMPLATE_DIR / name)["jobs"]["run"]["env"]
    referenced = {
        m for v in env.values() if isinstance(v, str)
        for m in re.findall(r"inputs\.([a-z_]+)", v)
    }
    missing = _dispatched_inputs() - referenced
    assert not missing, f"{name}: {sorted(missing)} never reach the job through env:"
