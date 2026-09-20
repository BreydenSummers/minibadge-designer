"""Failure logging: the server-side record of every refused or failed request.

Before this existed, a refusal reached the user as JSON and reached the
server as a status code in the access log and nothing else. The operator's
question -- "why did this person's export fail?" -- had no answer on the box.
These tests hold the shape of the answer: one line per failed response, on the
app's logger, saying who, what, which status, what the user was told, and
(when the handler swallowed an exception) what actually went wrong.

Value Bar, stated honestly: a broken line here damages no badge. It damages
the *next* badge -- a fab-breaking refusal nobody can diagnose is one nobody
fixes. That is one step removed from the artifact, and it is the reason this
file holds two tests rather than one per code path. Where the config that
carries these lines can take the whole container down (gunicorn's
logconfig_dict), the proof lives at the deploy tier in test_deploy.py.
"""

import json
import logging

import pytest

from minibadge_designer import webapp

APP_LOGGER = "minibadge_designer.webapp"

# A client the reverse proxy would report, not the loopback the test client
# uses by default: the line's first field is the *attributable* address (the
# whole point of forwarded_allow_ips in gunicorn.conf.py), so the test states
# one and checks it is the one that lands.
CLIENT = "203.0.113.9"


@pytest.mark.webapp
def test_a_refused_request_leaves_one_warning_line_and_a_bot_404_leaves_none(
        client, params, caplog):
    """The refusal a user sees is the refusal the server records, once.

    Moving off the defaults: the LED that trips the refusal is a 3mm
    through-hole part on the back with a rotation, not the 0805 every example
    uses, and the client address is a forwarded one. Contrast case in the same
    test: an HTML 404 -- what a scanner produces all day -- must NOT add a
    line, or the file this feeds fills with noise and the refusals drown.
    """
    bad_led = {"x": "seven", "y": 6.0, "color": "green", "size": "3mm",
               "side": "back", "rot": 90}
    with caplog.at_level(logging.INFO, logger=APP_LOGGER):
        resp = client.post("/generate",
                           data={"params": json.dumps(params(leds=[bad_led]))},
                           environ_base={"REMOTE_ADDR": CLIENT})
        assert resp.status_code == 400, resp.data[:200]
        told = resp.get_json()["error"]

        lines = [r for r in caplog.records if r.name == APP_LOGGER]
        assert len(lines) == 1, (
            f"one failed response must be one log line, got {len(lines)}: "
            f"{[r.getMessage() for r in lines]}")
        line = lines[0]
        assert line.levelno == logging.WARNING, (
            f"a 4xx is a refusal (WARNING), logged at {line.levelname}")
        msg = line.getMessage()
        for piece in (CLIENT, "POST", "/generate", "400", told):
            assert piece in msg, f"log line lacks {piece!r}: {msg}"

        caplog.clear()
        resp = client.get("/wp-login.php", environ_base={"REMOTE_ADDR": CLIENT})
        assert resp.status_code == 404
        assert not [r for r in caplog.records if r.name == APP_LOGGER], (
            "an HTML 404 must not be logged; on a public host that is bot "
            "noise, and the access log already has it")


@pytest.mark.webapp
def test_a_generic_refusal_carries_the_swallowed_exception_as_its_cause(
        flask_app, caplog):
    """"Could not process the board shape" is the message the user gets on
    purpose; the exception behind it is what the operator needs. Every geometry
    backstop in the handlers is `except _GEOMETRY_ERRORS: return _refuse(...)`,
    and no payload in the hostile corpus reaches one (the parsers ahead of them
    absorb everything), so this drives the exact pair a handler runs -- a real
    swallowed exception, the real refusal, the real after_request chain -- in a
    request context instead of through a route.

    Rotated: a KeyError (the dict-shaped failures), a 422 rather than the
    default 400, and a path with a query string, which must survive intact.
    """
    with caplog.at_level(logging.INFO, logger=APP_LOGGER), \
            flask_app.test_request_context(
                "/outline?side=back", method="POST",
                environ_base={"REMOTE_ADDR": CLIENT}):
        try:
            {}["window_ring"]
        except KeyError:
            rv = webapp._refuse("could not process the board shape", 422)
        resp = flask_app.process_response(flask_app.make_response(rv))

    assert resp.status_code == 422
    assert resp.get_json() == {"error": "could not process the board shape"}, (
        "the user must still get the generic message, never the exception")
    lines = [r for r in caplog.records if r.name == APP_LOGGER]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    msg = lines[0].getMessage()
    for piece in (CLIENT, "/outline?side=back", "422",
                  "could not process the board shape",
                  "KeyError", "window_ring"):
        assert piece in msg, f"log line lacks {piece!r}: {msg}"


@pytest.mark.webapp
def test_a_slow_request_is_logged_even_when_it_succeeds(
        client, monkeypatch, caplog):
    """An address that keeps sending the heaviest possible boards gets 200s,
    so the failure hook never sees it; the slow-request line is what puts it
    on the operator's watch list. The threshold is dropped to zero so the
    cheapest request in the app (the index page) crosses it -- the rule under
    test is "over the threshold means a WARNING", not the threshold's value.
    """
    monkeypatch.setattr(webapp, "SLOW_REQUEST_S", 0.0)
    with caplog.at_level(logging.INFO, logger=APP_LOGGER):
        resp = client.get("/", environ_base={"REMOTE_ADDR": CLIENT})
    assert resp.status_code == 200
    slow = [r for r in caplog.records
            if r.name == APP_LOGGER and "slow request" in r.getMessage()]
    assert len(slow) == 1, [r.getMessage() for r in caplog.records]
    assert slow[0].levelno == logging.WARNING
    for piece in (CLIENT, "GET", "/", "200"):
        assert piece in slow[0].getMessage(), slow[0].getMessage()


@pytest.mark.webapp
def test_the_request_in_flight_is_named_while_it_runs_and_gone_afterwards(
        flask_app):
    """When gunicorn kills a worker on its timeout, the request being served
    gets no access line and no failure line; the worker_abort hook asks the
    app what was in flight. So the record has to exist *during* the request
    and be cleared by teardown, or a later timeout would blame the previous
    request. Driven through Flask's own before/teardown machinery, the same
    path a real request takes, with an upload size and a query string so both
    show up in the report."""
    with flask_app.test_request_context(
            "/gerbers?fmt=zip", method="POST", data=b"x" * 1234,
            content_type="application/octet-stream",
            environ_base={"REMOTE_ADDR": CLIENT},
            headers={"User-Agent": "Mozilla/5.0 (probe)"}):
        assert webapp.in_flight_report() is None, "nothing has started yet"
        flask_app.preprocess_request()
        report = webapp.in_flight_report()
        assert report is not None
        for piece in (CLIENT, "POST", "/gerbers?fmt=zip", "1234 bytes",
                      "Mozilla/5.0 (probe)"):
            assert piece in report, report
        flask_app.do_teardown_request()
    assert webapp.in_flight_report() is None, (
        "the record outlived its request; the next worker timeout would be "
        "blamed on this one")
