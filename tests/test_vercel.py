"""
The server as a Vercel Function: stateless `/mcp`, the site, and the two
misconfigurations it must refuse to run in.

Driven through Starlette's TestClient *without* its context manager, on
purpose: used that way it sends no lifespan events, which is exactly the
host the on-demand startup exists for.
"""

import json
import os
import sys
import time

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.server import mcp_server, vercel
from docsforge.tools import forge_tools as ft
from docsforge.tools import harvest_jobs

TOKEN = "vercel-token"
# What Vercel's environment holds: the store, the token, and the production
# domain the platform itself provides — which is what OAuth discovery names.
ENV_OK = {"DOCSFORGE_DB": "postgresql://x:y@db.example/z", mcp_server.TOKEN_VAR: TOKEN,
          "VERCEL_PROJECT_PRODUCTION_URL": "docsforge.vercel.app"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client():
    # No `with`: no lifespan events. The deployment hostname as Host.
    return TestClient(vercel.build(ENV_OK), base_url="https://docsforge.vercel.app")


def test_site_and_health_serve_without_lifespan_events(client, monkeypatch):
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    assert client.get("/").status_code == 200
    assert "Twelve tools" in client.get("/tools").text
    assert client.get("/health").json()["status"] == "ok"
    # The Connect page names this deployment, not a placeholder.
    assert "https://docsforge.vercel.app/mcp" in client.get("/connect").text


def test_mcp_is_gated(client):
    assert client.post("/mcp", json=INIT, headers=HDR).status_code == 401


def test_mcp_answers_plain_json_and_needs_no_session(client):
    h = {**HDR, "Authorization": f"Bearer {TOKEN}"}
    r = client.post("/mcp", json=INIT, headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json"), \
        "stateless mode answers JSON, not an event stream"
    assert r.json()["result"]["serverInfo"]["name"] == "docsforge"
    # A second request with no session id at all — a different instance, as
    # far as a serverless host is concerned — still works.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"learn_technology", "search_knowledge_base", "read_knowledge_base"} <= names


def test_the_deployment_hostname_is_accepted(client):
    # Not loopback: the SDK's localhost-only Host check must be off.
    r = client.post("/mcp", json=INIT, headers={**HDR, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code != 421


@pytest.mark.parametrize("env,reason", [
    ({mcp_server.TOKEN_VAR: TOKEN}, "DOCSFORGE_DB"),
    ({"DOCSFORGE_DB": "postgresql://x:y@db.example/z"}, mcp_server.TOKEN_VAR),
])
def test_a_misconfigured_deployment_serves_the_site_but_refuses_mcp(env, reason):
    c = TestClient(vercel.build(env), base_url="https://x.vercel.app")
    assert c.get("/").status_code == 200
    r = c.post("/mcp", json=INIT, headers={**HDR, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503
    assert reason in r.json()["reason"]


def test_importing_the_entrypoint_marks_the_process_ephemeral():
    # On Vercel the import is the process, so the flag is set at import —
    # the suite resets it between tests (conftest).
    import importlib
    importlib.reload(vercel)
    assert harvest_jobs.EPHEMERAL is True


def test_an_unfinished_harvest_promises_nothing_it_cannot_keep(monkeypatch):
    """It used to say "discarded — nothing partial was stored", which reads as
    a verdict and is not one: the thread runs on while the instance lives, and
    `harvest_status` answered "1 harvest running … let it finish" one call
    later. A caller shown both in consecutive calls cannot act on either.

    So the message promises only what the shared record delivers — that the
    answer is knowable — and still promises no continuation."""
    monkeypatch.setattr(harvest_jobs, "EPHEMERAL", True)
    job = harvest_jobs.Job(id="mojo-1", label="mojo", started=time.time() - 30)
    message = ft._still_harvesting(job)

    assert "continues in the background" not in message
    assert "may keep running" in message and "cut off at any moment" in message
    assert "mojo-1" in message, "the id is what makes it followable at all"
    assert "harvest_status" in message
    assert "stopped reporting" in message, "how to tell it died"
    assert "python main.py" in message
    # and the load-bearing line survives: do not call again here
    assert "Do not call learn_technology" in message


def test_the_unfinished_message_names_the_tool_that_was_called(monkeypatch):
    """Telling a `harvest_docs` caller not to call `learn_technology` again is
    advice about a tool they did not use."""
    monkeypatch.setattr(harvest_jobs, "EPHEMERAL", True)
    job = harvest_jobs.Job(id="mojo-1", label="mojo", started=time.time() - 30)
    assert "Do not call harvest_docs" in ft._still_harvesting(job, "harvest_docs")

    monkeypatch.setattr(harvest_jobs, "EPHEMERAL", False)
    assert "Do not call harvest_docs" in ft._still_harvesting(job, "harvest_docs")


def test_on_vercel_writable_paths_go_to_tmp(monkeypatch):
    # Re-import with VERCEL set: the caches must land in /tmp, which is the
    # only writable place, and must not override an operator's own choice.
    import importlib
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DOCSFORGE_LOG_DIR", raising=False)
    monkeypatch.setenv("DOCSFORGE_HARVEST_STATE", "/srv/keep-mine")
    importlib.reload(vercel)
    assert os.environ["DOCSFORGE_LOG_DIR"] == "/tmp/docsforge/logs"
    assert os.environ["DOCSFORGE_HARVEST_STATE"] == "/srv/keep-mine"


def test_a_host_that_sends_lifespan_is_not_started_twice():
    # Used *with* the context manager the client drives lifespan like a real
    # server; the on-demand path must step aside and requests must work.
    with TestClient(vercel.build(ENV_OK), base_url="https://docsforge.vercel.app") as c:
        h = {**HDR, "Authorization": f"Bearer {TOKEN}"}
        assert c.post("/mcp", json=INIT, headers=h).status_code == 200
        assert c.get("/health").status_code == 200


def test_oauth_discovery_names_the_production_domain(client):
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["authorization_endpoint"] == "https://docsforge.vercel.app/authorize"
    r = client.post("/mcp", json=INIT, headers=HDR)
    assert 'resource_metadata="https://docsforge.vercel.app/.well-known/oauth-protected-resource/mcp"' \
        in r.headers["www-authenticate"]


def test_without_a_production_domain_the_token_still_works_and_oauth_is_off(capsys):
    env = {k: v for k, v in ENV_OK.items() if k != "VERCEL_PROJECT_PRODUCTION_URL"}
    c = TestClient(vercel.build(env), base_url="https://x.vercel.app")
    assert c.post("/mcp", json=INIT, headers={**HDR, "Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404
    assert "OAuth not offered" in capsys.readouterr().err
