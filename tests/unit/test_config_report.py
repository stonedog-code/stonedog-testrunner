"""`GET /admin/config` — the settings an operator can finally see (NEH-1191).

These five values decide every refusal the trigger surface can produce, and
until this endpoint they appeared in NO surface. A `/testauto` refused as
"product not allowed" could be told that it was refused and never what would
have been accepted.

Two things are load-bearing and both are asserted here rather than commented:

* **The token's value never leaves the process.** Present or absent, nothing
  else. Not masked, not prefixed, not measured — a masked secret is still a
  secret rendered into HTML, and a length is a real hint about which kind of
  token it is.
* **Empty and absent are different answers.** A variable nobody set and a
  variable deliberately set to nothing both refuse every command, and the
  operator needs to know which one they are looking at: the first is a mistake,
  the second is a decision.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from edge_server import app as edge_app
from edge_server.config import REPORTED_SETTINGS, SECRET_SETTINGS, EdgeConfig

pytestmark = pytest.mark.unit

ADMIN = "admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN}"}

#: Fictional, like every other fixture here: a real product name in this file
#: would be a real product name shipped in this repository.
PRODUCTS = frozenset({"alpha", "beta"})
SERVERS = frozenset({"sandbox", "staging"})
SCOPES = frozenset({"smoke", "full"})

#: Distinctive enough that a substring search for it cannot match by accident.
TOKEN = "ghp-SEKRIT-do-not-render-6a0314e7"


def a_config(tmp_path, **over) -> EdgeConfig:
    base = dict(
        signing_secret="unused-here",
        db_path=str(tmp_path / "edge.db"),
        admin_token=ADMIN,
        allowed_products=PRODUCTS,
        allowed_servers=SERVERS,
        allowed_test_scopes=SCOPES,
        product_repos={"alpha": "an-org/alpha-repo", "beta": "an-org/beta-repo"},
        github_token=TOKEN,
        poll_timeout=0,
        declared=frozenset(REPORTED_SETTINGS),
    )
    base.update(over)
    return EdgeConfig(**base)


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = edge_app.app
    app.state.config = a_config(tmp_path)
    app.state.store = None
    return TestClient(app)


def by_name(body: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in body["settings"]}


class TestDefaultDeny:
    """Same treatment as every other `/admin` route, for the same reason."""

    def test_404s_without_a_token(self, client: TestClient) -> None:
        assert client.get("/admin/config").status_code == 404

    def test_404s_with_the_wrong_token(self, client: TestClient) -> None:
        assert client.get(
            "/admin/config", headers={"Authorization": "Bearer not-the-token"}
        ).status_code == 404

    def test_404s_rather_than_401_or_403(self, client: TestClient) -> None:
        # 404, deliberately. Which products and repositories exist is exactly
        # what an unauthenticated caller should not have confirmed, and a 403
        # confirms the route.
        assert client.get("/admin/config").json() == {"detail": "Not Found"}


class TestTheTokenNeverLeaves:
    """The assertion that has to survive somebody adding a helpful preview."""

    def test_the_token_value_appears_nowhere_in_the_body(self, client: TestClient) -> None:
        raw = client.get("/admin/config", headers=AUTH).text
        assert TOKEN not in raw
        # Not a prefix either. A leak that only shows the first eight characters
        # is still a leak, and it is the shape a "masked" field takes.
        assert TOKEN[:8] not in raw

    def test_the_token_is_reported_as_present_and_nothing_else(
        self, client: TestClient
    ) -> None:
        entry = by_name(client.get("/admin/config", headers=AUTH).json())["GITHUB_TOKEN"]
        assert entry == {
            "name": "GITHUB_TOKEN",
            "configured": True,
            "kind": "secret",
            "present": True,
        }
        # No length, which is a real hint about which kind of token it is.
        assert "length" not in entry
        assert "value" not in entry

    def test_an_absent_token_is_absent_rather_than_blank(self, tmp_path) -> None:
        app = edge_app.app
        app.state.config = a_config(tmp_path, github_token="", declared=frozenset())
        app.state.store = None
        entry = by_name(TestClient(app).get("/admin/config", headers=AUTH).json())[
            "GITHUB_TOKEN"
        ]
        assert entry["configured"] is False
        assert entry["present"] is False

    def test_a_token_set_to_empty_is_configured_but_not_present(self, tmp_path) -> None:
        # The deployment that "has" a token which authorises nothing. It is a
        # different fault from never having set one, and it is diagnosed
        # differently.
        app = edge_app.app
        app.state.config = a_config(
            tmp_path, github_token="", declared=frozenset({"GITHUB_TOKEN"})
        )
        app.state.store = None
        entry = by_name(TestClient(app).get("/admin/config", headers=AUTH).json())[
            "GITHUB_TOKEN"
        ]
        assert entry["configured"] is True
        assert entry["present"] is False


class TestWhatItReports:
    def test_reports_every_setting_the_module_declares(self, client: TestClient) -> None:
        body = client.get("/admin/config", headers=AUTH).json()
        # Walks the tuple rather than a list retyped here, so a setting added to
        # the config without a reader fails this instead of being silently
        # unreported. THE COUNT, beside the list, as everywhere else.
        assert body["count"] == len(REPORTED_SETTINGS)
        assert [entry["name"] for entry in body["settings"]] == list(REPORTED_SETTINGS)

    def test_reports_the_allowlists_the_process_actually_loaded(
        self, client: TestClient
    ) -> None:
        entries = by_name(client.get("/admin/config", headers=AUTH).json())
        assert entries["RUNTESTS_PRODUCTS"]["values"] == ["alpha", "beta"]
        assert entries["RUNTESTS_SERVERS"]["values"] == ["sandbox", "staging"]
        assert entries["RUNTESTS_TEST_SCOPES"]["values"] == ["full", "smoke"]
        assert entries["RUNTESTS_PRODUCTS"]["count"] == 2

    def test_reports_the_dispatch_map_that_bounds_the_token(
        self, client: TestClient
    ) -> None:
        # This value, not the token, is what bounds the token. It is the whole
        # reason the surface is operator-only.
        entry = by_name(client.get("/admin/config", headers=AUTH).json())[
            "RUNTESTS_PRODUCT_REPOS"
        ]
        assert entry["kind"] == "map"
        assert entry["entries"] == {
            "alpha": "an-org/alpha-repo",
            "beta": "an-org/beta-repo",
        }
        assert entry["count"] == 2

    def test_an_unset_allowlist_reads_as_not_configured(self, tmp_path) -> None:
        app = edge_app.app
        app.state.config = a_config(
            tmp_path, allowed_products=frozenset(), declared=frozenset()
        )
        app.state.store = None
        entry = by_name(TestClient(app).get("/admin/config", headers=AUTH).json())[
            "RUNTESTS_PRODUCTS"
        ]
        assert entry["configured"] is False
        assert entry["values"] == []

    def test_a_deliberately_empty_allowlist_is_NOT_the_same_answer(
        self, tmp_path
    ) -> None:
        # Both refuse every command. One is a deployment that forgot a setting,
        # the other is "allow nothing", and only `configured` tells them apart.
        app = edge_app.app
        app.state.config = a_config(
            tmp_path,
            allowed_products=frozenset(),
            declared=frozenset({"RUNTESTS_PRODUCTS"}),
        )
        app.state.store = None
        entry = by_name(TestClient(app).get("/admin/config", headers=AUTH).json())[
            "RUNTESTS_PRODUCTS"
        ]
        assert entry["configured"] is True
        assert entry["values"] == []

    def test_is_byte_stable_over_an_unchanged_edge(self, client: TestClient) -> None:
        # Sorted throughout, so two reads of an unchanged process are identical
        # and a diff between them means something moved.
        first = client.get("/admin/config", headers=AUTH).text
        assert first == client.get("/admin/config", headers=AUTH).text


class TestTheReportItself:
    """`settings_report` without the HTTP layer, so the rule is pinned where it lives."""

    def test_no_secret_setting_carries_a_value_key(self, tmp_path) -> None:
        report = a_config(tmp_path).settings_report()
        for entry in report:
            if entry["name"] in SECRET_SETTINGS:
                assert set(entry) == {"name", "configured", "kind", "present"}

    def test_the_token_is_not_in_the_serialised_report(self, tmp_path) -> None:
        # Serialised, because that is the form that reaches a browser. A dict
        # comparison would pass while a nested object still carried it.
        assert TOKEN not in json.dumps(a_config(tmp_path).settings_report())

    def test_every_reported_setting_is_covered_by_exactly_one_reader(
        self, tmp_path
    ) -> None:
        # The guard against the config and the report drifting: a new entry in
        # REPORTED_SETTINGS with no branch to read it would arrive here with
        # neither `values` nor `entries` nor `present`.
        for entry in a_config(tmp_path).settings_report():
            shapes = [k for k in ("values", "entries", "present") if k in entry]
            assert len(shapes) == 1, f"{entry['name']} reported as {shapes}"
