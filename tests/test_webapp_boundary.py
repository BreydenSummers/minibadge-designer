"""Collects the hostile-input corpus (`tests/hostile.py`) against the HTTP boundary.

This module is deliberately three lines of code. Every decision (which routes a
payload visits, its wall-clock budget, its strict-xfail marker naming a live
defect, its `slow`/`kicad` tier markers) is made by `hostile.route_cases()` and
asserted by `hostile.probe()`, which returns `None` so that nothing here can grow
an assertion about a status code. Add payloads to the corpus, never to this file.
"""

import hostile
import pytest


@pytest.mark.webapp
@pytest.mark.parametrize("route,case", hostile.route_cases())
def test_hostile_input_never_crashes(client, route, case):
    hostile.probe(client, route, case)
