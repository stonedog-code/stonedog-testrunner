"""The job-definition API — the third door, and the one an admin UI uses.

It is DEFAULT DENY and answers 404, not 401 or 403. An unauthenticated caller
learns only that there is nothing here; a job list names products, servers and
repositories, and confirming those exist is exactly the reconnaissance a public
endpoint should not do.

The tab is a CLIENT of this (PRD A2.4), never a second reader of the tables —
so what these tests pin is the contract that app depends on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from edge_server import app as edge_app
from edge_server.config import EdgeConfig
from slack_runtests import identity
from slack_runtests.store import open_store

pytestmark = pytest.mark.unit

ADMIN = "admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN}"}

#: Fictional, like every other fixture here: a real product name in this file
#: is a real product name shipped in this repository.
PRODUCTS = frozenset({"alpha", "beta"})
SERVERS = frozenset({"sandbox", "staging"})
SCOPES = frozenset({"smoke", "full"})


def a_job(**over) -> dict:
    body = {
        "name": "alpha smoke",
        "description": "",
        "product": "alpha",
        "test_scope": "smoke",
        "server": "sandbox",
        "action_kind": "test-server",
        "action_target": "any-runner",
        "language": "python",
    }
    body.update(over)
    return body


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = edge_app.app
    app.state.config = EdgeConfig(
        signing_secret="unused-here",
        db_path=str(tmp_path / "edge.db"),
        admin_token=ADMIN,
        allowed_products=PRODUCTS,
        allowed_servers=SERVERS,
        allowed_test_scopes=SCOPES,
        poll_timeout=0,
        github_token="",          # so the workflow check reports SKIPPED
    )
    app.state.store = open_store(str(tmp_path / "edge.db"))
    app.state.edge_key = identity.generate()
    return TestClient(app)


@pytest.fixture
def closed(tmp_path) -> TestClient:
    """The same app with NO admin token — the production default today."""
    app = edge_app.app
    app.state.config = EdgeConfig(
        signing_secret="unused-here",
        db_path=str(tmp_path / "closed.db"),
        admin_token="",
        allowed_products=PRODUCTS,
        allowed_servers=SERVERS,
        allowed_test_scopes=SCOPES,
        poll_timeout=0,
    )
    app.state.store = open_store(str(tmp_path / "closed.db"))
    app.state.edge_key = identity.generate()
    return TestClient(app)


# ── default deny, on EVERY endpoint ─────────────────────────────────────────

ENDPOINTS = [
    ("get", "/admin/jobs"),
    ("get", "/admin/jobs/some-id"),
    ("put", "/admin/jobs/some-id"),
    ("delete", "/admin/jobs/some-id"),
]


def call(client: TestClient, method: str, path: str, **kw):
    """`get` and `delete` take no body, so a shared `json=` is a TypeError."""
    if method == "put":
        kw["json"] = a_job()
    return getattr(client, method)(path, **kw)


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_no_token_is_a_404_on_every_endpoint(client: TestClient, method, path) -> None:
    """Enumerated, not spot-checked. A new endpoint that forgets the guard is
    the way this leaks, and a test naming only two of four would not see it."""
    response = call(client, method, path)
    assert response.status_code == 404, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_wrong_token_is_a_404_too(client: TestClient, method, path) -> None:
    response = call(client, method, path, headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 404


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_an_unset_admin_token_closes_the_door_rather_than_opening_it(
    closed: TestClient, method, path
) -> None:
    """The empty-allowlist rule, applied to a credential.

    With no EDGE_ADMIN_TOKEN configured, presenting no token must NOT match.
    `"" == ""` is the shape of that bug, and it would make the whole API public
    on every deployment that never set the variable — which is production today.
    """
    response = call(closed, method, path)
    assert response.status_code == 404
    # And presenting a token does not open it either.
    response = call(closed, method, path, headers={"Authorization": "Bearer "})
    assert response.status_code == 404


def test_the_positive_control_a_real_token_is_accepted(client: TestClient) -> None:
    """Without this every assertion above is satisfied by an API that 404s
    unconditionally."""
    assert client.get("/admin/jobs", headers=AUTH).status_code == 200


# ── the list ────────────────────────────────────────────────────────────────

def test_an_empty_list_reports_a_count_of_zero(client: TestClient) -> None:
    """`0 jobs` and a body the caller failed to parse are otherwise the same
    answer, and only the count distinguishes them."""
    body = client.get("/admin/jobs", headers=AUTH).json()
    assert body == {"count": 0, "jobs": []}


def test_the_count_follows_the_list(client: TestClient) -> None:
    # THREE DISTINCT TRIGGERS. The first version of this cycled two products
    # against two scopes over three jobs, so the third repeated the first and
    # was refused as a duplicate — the count came back 2 and the constraint was
    # doing its job on a fixture that was wrong.
    triggers = [("alpha", "smoke", "sandbox"),
                ("beta", "smoke", "sandbox"),
                ("alpha", "full", "sandbox")]
    for n, (product, scope, server) in enumerate(triggers):
        created = client.put(
            f"/admin/jobs/j{n}",
            json=a_job(name=f"job {n}", product=product, test_scope=scope, server=server),
            headers=AUTH,
        )
        assert created.status_code == 201, created.text

    body = client.get("/admin/jobs", headers=AUTH).json()
    assert body["count"] == len(body["jobs"]) == 3


# ── create and replace ──────────────────────────────────────────────────────

def test_a_new_job_is_created_and_readable_back(client: TestClient) -> None:
    created = client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    assert created.status_code == 201, created.text
    assert created.json()["result"] == "created"

    got = client.get("/admin/jobs/j1", headers=AUTH).json()
    assert got["name"] == "alpha smoke"
    # The canonical trigger is returned, so the tab does not have to build it
    # and get it subtly different from what the parser accepts.
    assert got["trigger"] == "runtests --product alpha --test_scope smoke --server sandbox"


def test_saving_the_same_id_again_replaces_rather_than_duplicating(client: TestClient) -> None:
    client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    again = client.put("/admin/jobs/j1", json=a_job(name="renamed"), headers=AUTH)
    assert again.status_code == 200
    assert again.json()["result"] == "updated"
    assert client.get("/admin/jobs", headers=AUTH).json()["count"] == 1


def test_a_second_job_claiming_one_trigger_is_refused_with_409(client: TestClient) -> None:
    """A2.2.2, surfaced. The store refuses it with a unique constraint; this
    reports that refusal rather than re-implementing the rule."""
    client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    clash = client.put("/admin/jobs/j2", json=a_job(name="different name"), headers=AUTH)
    assert clash.status_code == 409, clash.text
    assert "trigger" in clash.json()


# ── the allowlist is the boundary, and the ROUTE is where it is applied ─────

@pytest.mark.parametrize("field,value", [
    ("product", "not-allowed"),
    ("server", "prod"),
    ("test_scope", "everything"),
])
def test_a_value_outside_the_allowlist_is_refused(client: TestClient, field, value) -> None:
    """A2.10: the allowlist is the security boundary, a job is a routing
    decision on top of it. The STORE deliberately does not check this — a
    stored row is a request, never an authorisation — so if this route did not,
    nothing would, and adding a job would widen the boundary.
    """
    refused = client.put("/admin/jobs/j1", json=a_job(**{field: value}), headers=AUTH)
    assert refused.status_code == 422, refused.text
    assert field in refused.json()["fields"]
    # And nothing was written.
    assert client.get("/admin/jobs", headers=AUTH).json()["count"] == 0


def test_the_refusal_NAMES_what_is_allowed(client: TestClient) -> None:
    """"not allowed" alone makes an admin guess, and the guess is usually a
    typo they cannot see in their own input."""
    refused = client.put("/admin/jobs/j1", json=a_job(product="alfa"), headers=AUTH)
    allowed = refused.json()["fields"]["product"]["allowed"]
    assert allowed == ["alpha", "beta"]
    assert refused.json()["fields"]["product"]["got"] == "alfa"


def test_a_malformed_job_is_refused_by_the_stores_own_validator(client: TestClient) -> None:
    """Shape errors come from `validate_job_def`, reported here rather than
    duplicated — two copies of a rule is one copy that drifts."""
    refused = client.put("/admin/jobs/j1", json=a_job(action_kind="curl"), headers=AUTH)
    assert refused.status_code == 422
    assert "gh-action" in refused.json()["detail"]


def test_a_body_that_is_not_an_object_is_a_400_not_a_500(client: TestClient) -> None:
    for body in ("[]", '"a string"', "null", "not json at all"):
        response = client.put("/admin/jobs/j1", content=body,
                              headers={**AUTH, "Content-Type": "application/json"})
        assert response.status_code == 400, f"{body!r} answered {response.status_code}"


# ── the workflow check must never silently pass (A2.3.1) ────────────────────

def test_a_gh_action_save_reports_that_the_check_was_SKIPPED(client: TestClient) -> None:
    """With no GITHUB_TOKEN nothing can be checked, and saying nothing would be
    the green-over-an-empty-set failure with a form around it."""
    created = client.put(
        "/admin/jobs/j1",
        json=a_job(action_kind="gh-action", action_target="runtests.yml"),
        headers=AUTH,
    )
    assert created.status_code == 201
    workflow = created.json()["workflow"]
    assert workflow["status"] == "skipped"
    assert workflow["reason"], "a skip must say why"


def test_a_test_server_save_does_not_claim_to_have_checked_a_workflow(client: TestClient) -> None:
    """There is no workflow to check, so there must be no `workflow` key at all
    — an absent check and a passed one must not look the same."""
    created = client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    assert "workflow" not in created.json()


# ── delete ──────────────────────────────────────────────────────────────────

def test_delete_says_whether_anything_was_deleted(client: TestClient) -> None:
    client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    assert client.delete("/admin/jobs/j1", headers=AUTH).json() == {"deleted": True}
    assert client.delete("/admin/jobs/j1", headers=AUTH).json() == {"deleted": False}


def test_a_deleted_trigger_is_free_again(client: TestClient) -> None:
    """Otherwise a deleted job blocks its own replacement forever."""
    client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    client.delete("/admin/jobs/j1", headers=AUTH)
    assert client.put("/admin/jobs/j2", json=a_job(), headers=AUTH).status_code == 201


def test_an_unknown_id_reads_as_404(client: TestClient) -> None:
    assert client.get("/admin/jobs/nope", headers=AUTH).status_code == 404


# ── history (NEH-1167) ──────────────────────────────────────────────────────

def test_history_needs_the_token_like_everything_else(client: TestClient) -> None:
    assert client.get("/admin/jobs/j1/runs").status_code == 404


def test_an_unknown_definition_404s_rather_than_returning_an_empty_history(
    client: TestClient,
) -> None:
    """A definition that does not exist and one that has never run are
    different facts. An empty history is an ordinary state for a new job, so
    answering `[]` for a typo would read as "this job has never run"."""
    assert client.get("/admin/jobs/nope/runs", headers=AUTH).status_code == 404


def test_a_new_definition_has_an_empty_history_with_a_count(client: TestClient) -> None:
    client.put("/admin/jobs/j1", json=a_job(), headers=AUTH)
    body = client.get("/admin/jobs/j1/runs", headers=AUTH).json()
    assert body == {"count": 0, "runs": []}


# ── language (NEH-1192) ──────────────────────────────────────────────────────
#
# `language` chooses the workflow file. It is stored and NEVER dispatched --
# workflow_dispatch caps at ten inputs and eight are already spent.


def test_a_definition_records_the_language_it_was_given(client: TestClient) -> None:
    """The positive control. Every refusal below is trivially true of an API
    that rejects everything, so this has to pass first."""
    created = client.put("/admin/jobs/j1", json=a_job(language="node"), headers=AUTH)
    assert created.status_code == 201

    read_back = client.get("/admin/jobs/j1", headers=AUTH)
    assert read_back.json()["language"] == "node"


def test_an_unknown_language_is_refused_and_the_message_names_the_real_ones(
    client: TestClient,
) -> None:
    """Refused at SAVE, not at dispatch.

    A bad language reaching dispatch surfaces as GitHub answering 422 for a
    workflow that does not exist, which reads as a GitHub fault rather than a
    definition one token wrong.
    """
    refused = client.put("/admin/jobs/j1", json=a_job(language="rust"), headers=AUTH)
    assert refused.status_code == 422

    detail = refused.text.lower()
    assert "python" in detail and "node" in detail, (
        "the refusal must name the allowed values -- an operator who cannot see "
        "them guesses again"
    )


def test_an_absent_language_is_refused_rather_than_defaulted(client: TestClient) -> None:
    """No default, deliberately.

    A default is a guess about somebody else's repository, and it fails
    silently: a Node product defaulted to python dispatches a workflow that is
    simply absent, reported as a missing workflow rather than a wrong language.
    """
    body = a_job()
    del body["language"]
    assert client.put("/admin/jobs/j1", json=body, headers=AUTH).status_code == 422


def test_the_workflow_is_derived_from_the_language_when_none_is_given(
    client: TestClient,
) -> None:
    """The ordinary case: an operator picks a language, not a filename."""
    body = a_job(action_kind="gh-action", language="node")
    del body["action_target"]

    assert client.put("/admin/jobs/j1", json=body, headers=AUTH).status_code == 201
    assert client.get("/admin/jobs/j1", headers=AUTH).json()["action_target"] == (
        "runtests-node.yml"
    )


def test_an_explicit_workflow_still_wins_over_the_derived_one(client: TestClient) -> None:
    """A repo whose workflow is named something else must not be locked out."""
    created = client.put(
        "/admin/jobs/j1",
        json=a_job(action_kind="gh-action", language="node", action_target="ci-tests.yml"),
        headers=AUTH,
    )
    assert created.status_code == 201
    assert client.get("/admin/jobs/j1", headers=AUTH).json()["action_target"] == "ci-tests.yml"
