"""
The path a newcomer takes: install, add one line to a client, run locally.

From a visitor's feedback (2026-11-26): the home page's main button led to a
setup page that asked for a token nobody could get, for a hosted server that
answered 401 — the deployment behind the site is private. The connect page
now leads with running DocsForge on your own machine, says plainly that
there is no public server, and keeps the hosted snippets for a server you
run yourself.

Found on the way: `pipx install docsforge` — the command the site, the FAQ,
llms.txt and the README all gave — installs an unrelated PyPI project of the
same name. And an installed copy kept its store inside site-packages and its
logs in whatever folder the client launched it from.
"""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import docsforge
from docsforge.server import mcp_server
from docsforge.store import kb_store

ROOT = Path(__file__).resolve().parent.parent
INSTALL = "pipx install git+https://github.com/R204570/DocsForge"
#: The bare name, as a command: what installs somebody else's package.
WRONG = re.compile(r"(pipx|pip|uv tool) install docsforge(?![\w\-\[])|uvx --from docsforge\b")
#: The same, given as a line to run — the README and pyproject.toml mention it
#: in prose only to warn against it.
WRONG_LINE = re.compile(r"^[#\s]*((pipx|pip|uv tool) install docsforge(?![\w\-\[])|uvx --from docsforge\b)",
                        re.M)


@pytest.fixture
def site(monkeypatch):
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    with TestClient(mcp_server.build_http_app(token="t"), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_nothing_tells_anyone_to_install_the_other_docsforge(site):
    for path in ("/", "/tools", "/connect", "/contributors", "/llms.txt"):
        found = WRONG.search(site.get(path).text)
        assert not found, (path, found.group(0))
    for name in ("README.md", "pyproject.toml"):
        found = WRONG_LINE.search((ROOT / name).read_text(encoding="utf-8"))
        assert not found, (name, found.group(0))
    assert INSTALL in site.get("/connect").text and INSTALL in site.get("/llms.txt").text
    assert INSTALL in (ROOT / "README.md").read_text(encoding="utf-8")


def test_connect_leads_with_running_it_locally(site):
    html = site.get("/connect").text
    install, clients, host = (html.index(f'id="{i}"') for i in ("install", "clients", "host"))
    assert install < clients < host
    # the snippet a reader sees first, script or not, needs no URL and no token
    assert '<pre id="snippet">claude mcp add docsforge -- docsforge-mcp</pre>' in html
    assert 'data-mode="local" class="tab is-active" aria-pressed="true"' in html
    # the server and token rows exist for a hosted server, and start hidden
    for row in re.findall(r'<div class="tokenrow hosted-only"[^>]*>', html):
        assert " hidden" in row
    assert len(re.findall(r'class="tokenrow hosted-only"', html)) == 2


def test_connect_says_there_is_no_public_server(site):
    html = site.get("/connect").text
    assert "There is no public server to connect to." in html
    assert "private development deployment" in html


def test_every_client_has_a_local_line(site):
    html = site.get("/connect").text
    local = html[html.index("var LOCAL = {"):html.index("var HOSTED = {")]
    for cmd in ('"claude mcp add docsforge -- " + CMD', '"codex mcp add docsforge -- " + CMD',
                '"gemini mcp add docsforge " + CMD', '\\"command\\":\\"" + CMD'):
        assert cmd in local, cmd
    assert local.count("text: stdioJson") == 5            # the JSON-config clients
    assert "Authorization" not in local and "__TOKEN__" not in local
    assert 'var CMD = "docsforge-mcp"' in html


def test_the_public_sites_own_mcp_is_never_offered_as_a_server(site):
    html = site.get("/connect").text
    assert f'var PUBLIC = "{docsforge.SITE_URL}"' in html and "__SITE_URL__" not in html
    assert 'serverEl.value = MCP.indexOf(PUBLIC + "/") === 0 ? "" : MCP;' in html


def test_the_faq_answers_whether_there_is_a_hosted_server(site):
    assert "Is there a hosted DocsForge I can connect to?" in site.get("/").text


def test_the_hero_shows_an_unpinned_call_storing_the_current_docs(site):
    html = site.get("/").text
    hero = html[html.index('class="hero"'):html.index('id="try"')]
    assert 'learn_technology(name="pydantic")</text>' in hero
    assert "stored as <tspan class=\"nt\">pydantic latest</tspan>" in hero
    assert "pydantic 1.10" not in hero and "214 times" not in hero
    assert "names 'pydantic' 80 times" in hero and hero.count('class="cell d-fill"') == 93 * 2


# ── the setup guide ─────────────────────────────────────
STEPS = ("before", "pipx", "install", "check", "client", "first", "files", "update",
         "extras", "clone", "trouble")


def test_the_setup_guide_walks_every_step_in_order(site):
    html = site.get("/setup").text
    at = [html.index(f'<section class="section" id="{s}">') for s in STEPS]
    assert at == sorted(at)
    # its contents list names every step, and every entry lands on one
    toc = html[html.index('class="toc"'):html.index("</nav>", html.index('class="toc"'))]
    assert re.findall(r'href="/setup#([a-z]+)"', toc) == list(STEPS)


def test_the_setup_guide_gives_the_commands_that_work(site):
    html = site.get("/setup").text
    assert INSTALL in html
    for line in ("py -m pip install --user pipx", "brew install pipx", "sudo apt install pipx",
                 "docsforge-mcp --list", "claude mcp add docsforge --scope user -- docsforge-mcp",
                 '"command": "docsforge-mcp"', "pipx reinstall docsforge",
                 "pipx uninstall docsforge",
                 "pipx install https://github.com/R204570/DocsForge/archive/refs/heads/main.zip",
                 "~/.docsforge/knowledge_base/"):
        assert line in html, line


def test_the_setup_guide_is_in_the_nav_and_linked_from_where_people_start(site):
    for path in ("/", "/tools", "/connect", "/contributors"):
        assert '<a href="/setup">Setup</a>' in site.get(path).text, path
    assert '<a href="/setup" aria-current="page">Setup</a>' in site.get("/setup").text
    assert 'href="/setup">Read the setup guide' in site.get("/").text
    assert 'href="/setup">setup guide</a>' in site.get("/connect").text
    assert 'guide.href = "/setup"' in site.get("/").text      # after a try-box answer
    assert f"[Setup]({docsforge.SITE_URL}/setup): the step-by-step" in site.get("/llms.txt").text


def _blocks(html):
    """The text of every code block on a page, notes included."""
    import html as htmllib
    return [htmllib.unescape(re.sub(r"<[^>]+>", "", b))
            for b in re.findall(r'<div class="codeblock[^"]*"[^>]*>.*?<pre>(.*?)</pre>', html, re.S)]


def test_every_code_block_gets_a_copy_button_that_copies_only_what_to_run(site):
    html = site.get("/setup").text
    # added by the page's script to every .codeblock, in its bar or its corner
    assert 'document.querySelectorAll(".codeblock").forEach' in html
    assert 'button.className = "copy"' in html
    # the `# …` notes are for reading and are left out of what is copied
    assert 'clone.querySelectorAll(".c").forEach(function (note) { note.remove(); })' in html
    assert len(_blocks(html)) >= 12


def test_a_copied_block_never_does_something_the_reader_did_not_mean(site):
    """Copy-paste runs a whole block. One that updated and then uninstalled,
    or that mixed Windows and macOS lines, would do the wrong thing whole."""
    for path in ("/setup", "/connect"):
        for block in _blocks(site.get(path).text):
            if "uninstall" in block:
                assert block.strip() == "pipx uninstall docsforge", block
            assert not ("Scripts\\activate" in block and "source .venv" in block), block
            assert not ("py -m" in block and ("brew " in block or "apt " in block)), block


def test_every_documented_flag_exists():
    """The guide tells people to run `docsforge-mcp --list`, `--stdio` and
    `--http`; the server's own parser is the authority on whether they do."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert mcp_server.main(["--list"]) == 0
    assert out.getvalue().count("\n    ") == 13
    with pytest.raises(SystemExit):
        mcp_server.main(["--stdio", "--http"])          # they exclude each other, so both exist


# ── where an installed copy writes ──────────────────────
def test_this_checkout_is_recognised_as_one():
    assert docsforge.CHECKOUT is True
    assert docsforge.home() == Path.home() / ".docsforge"


def test_a_clone_keeps_its_store_beside_main_py(monkeypatch):
    monkeypatch.setattr(docsforge, "CHECKOUT", True)
    store = kb_store.build_store()
    assert Path(store.location) == (docsforge.ROOT / "knowledge_base").resolve()


def test_an_installed_copy_keeps_its_store_in_the_users_home(monkeypatch, tmp_path):
    monkeypatch.setattr(docsforge, "CHECKOUT", False)
    monkeypatch.setattr(docsforge, "home", lambda: tmp_path / ".docsforge")
    store = kb_store.build_store()
    assert Path(store.location) == (tmp_path / ".docsforge" / "knowledge_base").resolve()


def test_the_store_root_variable_still_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(docsforge, "CHECKOUT", False)
    monkeypatch.setenv("DOCSFORGE_KB_ROOT", str(tmp_path / "kb"))
    assert Path(kb_store.build_store().location) == (tmp_path / "kb").resolve()
