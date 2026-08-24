"""`slack-runtests-runner` — start a test server."""

from __future__ import annotations

import logging
import os

from stonedog_logs import configure


def main() -> int:
    from .agent import run_forever

    # One line, one format, one service tag across the edge, the API and the
    # test servers — which is the point of adopting a logging package rather
    # than repeating `basicConfig` in three entry points with three chances to
    # drift. STONEDOG_LOGS_JSON=1 switches the whole fleet to JSON without a
    # code change.
    configure(service_name='slack-runtests-runner')
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
