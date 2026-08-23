# TWO IMAGES, ONE FILE. Build with --target edge or --target runner.
#
# They were one image until the deployment work, and the reason to split them is
# not size. The edge is the process that answers the internet and NEVER runs a
# test; the test server is the process that runs tests and opens no inbound
# port. Shipping one image meant the public-facing process carried pytest and a
# sample suite it had no business being able to execute, and lacked the Postgres
# driver its own durable store needs.
#
# The separation is real rather than cosmetic, because a stage chooses its own
# COPY. Measured on the built images rather than assumed — `docker/smoke.sh`
# asserts it, and would fail if a future edit reunited them:
#
#     edge   : edge_server ✓  slack_runtests ✓  psycopg ✓  test_server ✗  pytest ✗
#     runner : test_server ✓  slack_runtests ✓  pytest   ✓  edge_server ✗
#
# (`psycopg` is in BOTH, which is not an oversight: the store conformance suite
# needs it, so it lives in the dev group the runner image installs.)
#
# Debian 12 "bookworm" via python:3.12-slim-bookworm — the house choice for
# every machine in this system, and the reason is that they should be the same.
# An earlier sketch of this harness used three different distributions to prove
# the code was portable. That is a different question from the one this harness
# asks: it exists to show that three IDENTICAL test servers share a queue
# correctly, and mixed base images would mean a scheduling bug and a musl
# packaging bug produce the same red result.
#
# Debian rather than Alpine specifically: glibc, which is what almost every
# production host and CI runner uses, so a wheel that installs here installs
# there. Slim rather than full: ~80 MB smaller and nothing in this stack needs
# a compiler at runtime.

# ── shared base ──────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS base

# uv manages the interpreter and the lockfile, exactly as it does on a laptop —
# pinned, because a moving tag makes two builds of the "same" image different.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv-docker \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    # Install exactly what the lockfile says, never re-resolve. Without this a
    # container can quietly drift from the image it was built as.
    UV_FROZEN=1 \
    PATH="/app/.venv-docker/bin:$PATH"

WORKDIR /app

# A non-root user, created once and inherited by both targets. It owns the key
# directory: the private key is written 0600 by identity.load_or_create, and
# that mode means nothing if the process is root, because root ignores it.
RUN useradd --create-home --uid 10001 runner \
    && mkdir -p /app/keys /app/data \
    && chown -R runner:runner /app

# Dependency manifests first, so editing source does not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/


# ── the edge: the public process. No test runner, no suite ───────────────────
FROM base AS edge

COPY edge/ ./edge/

# --no-dev is the point, not an economy: pytest lives in the dev group, and the
# process answering the internet must not be able to run a suite.
#
# --extra postgres because the embedded deployment's store is the host's
# Postgres. A container service with no persistent volume deletes a SQLite file
# on every redeploy, taking the queue and the whole run history with it — and
# the driver is not installed by default, so an image without it would discover
# that at the first Slack command.
RUN uv sync --frozen --no-dev --extra postgres \
    && chown -R runner:runner /app

USER runner

# The console script directly, NOT `uv run`. `uv run` performs a sync check on
# every start, which on this image would REINSTALL THE DEV GROUP and put pytest
# back into the very process this stage exists to keep it out of. It also means
# a container start needs no network and cannot drift from what was built.
# `docker/smoke.sh` checks pytest is absent from the RUNNING container, which is
# the only check that can see this.
CMD ["slack-runtests-edge"]


# ── the test server: runs suites, opens no port ──────────────────────────────
FROM base AS runner

COPY test-server/ ./test-server/
COPY tests/ ./tests/

# WITH the dev group, and that is not laziness: pytest lives there, and a test
# server whose whole job is to run pytest needs it. `--no-dev` here builds an
# image that starts cleanly, enrols, claims a job, and then fails to run it.
RUN uv sync --frozen \
    && chown -R runner:runner /app

USER runner

CMD ["slack-runtests-runner"]
