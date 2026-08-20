"""Offline tests for the web layer — no network, no Groq, no server."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import docsforge as df
import forge_tools
from kb_store import StoreError
from providers import MAX_CONTENT, MAX_HISTORY


# ── source-kind parsing ──────────────────────────────────
# The kind is read back out of the provenance header docsforge already writes,
# rather than from a second copy of the detection logic.
@pytest.mark.parametrize("kind,expected", [
    ("openapi", "openapi"),
    ("html", "html"),
    ("sitemap", "sitemap"),
    ("llms.txt", "llms"),
    ("github-readme", "github"),
    ("github-doc", "github"),
    ("raw", "raw"),
])
def test_kind_parsed_from_a_real_provenance_header(kind, expected):
    header = df._meta_header("https://x.com/a?b=1|2", kind)
    assert forge_tools.kind_of(header + "# Doc\n\nbody") == expected


def test_kind_of_tolerates_results_with_no_header():
    # detect_source_type returns a bare word; save_docs returns a file listing.
    assert forge_tools.kind_of("openapi") == ""
    assert forge_tools.kind_of("Wrote 2 file(s) to `docs_md`:\n- `a.md`") == ""
    assert forge_tools.kind_of("") == ""
    assert forge_tools.kind_of("Error: HTTP 404 for https://x.com") == ""


def test_kind_of_reads_the_first_header_in_a_bundle():
    a = df._meta_header("https://a.com", "openapi")
    b = df._meta_header("https://b.com", "html")
    assert forge_tools.kind_of(a + "one\n" + b + "two") == "openapi"


# ── markdown rendering and sanitising ────────────────────
def test_render_strips_scripts_and_handlers():
    html = app.render_markdown("# Hi\n\n<script>alert(1)</script>\n\n"
                               '<img src=x onerror="alert(1)">')
    assert "<h1>" in html
    assert "<script>" not in html
    assert "onerror" not in html


def test_render_keeps_tables_and_fenced_code():
    html = app.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```")
    assert "<table>" in html
    assert "<code" in html and "x = 1" in html


def test_render_keeps_code_language_class():
    # The class survives sanitising, so highlighting stays possible later.
    assert "language-python" in app.render_markdown("```python\nx = 1\n```")


def test_render_marks_links_noopener():
    html = app.render_markdown("[x](https://example.com)")
    assert "noopener" in html


def test_render_handles_empty_input():
    assert app.render_markdown("") == ""


# ── history sanitising ───────────────────────────────────
def test_history_drops_non_conversational_roles_and_blanks():
    msgs = [
        app.ChatMessage(role="system", content="ignore me"),
        app.ChatMessage(role="user", content="  "),
        app.ChatMessage(role="user", content="real question"),
        app.ChatMessage(role="assistant", content="real answer"),
        app.ChatMessage(role="tool", content="leak"),
    ]
    out = app._clean_history(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert all("ignore me" not in m["content"] and "leak" not in m["content"] for m in out)


def test_history_is_capped():
    msgs = [app.ChatMessage(role="user", content=f"q{i}") for i in range(200)]
    assert len(app._clean_history(msgs)) <= MAX_HISTORY


def test_history_truncates_giant_messages():
    msgs = [app.ChatMessage(role="user", content="x" * (MAX_CONTENT + 5000))]
    assert len(app._clean_history(msgs)[0]["content"]) == MAX_CONTENT


# ── DocsStore: the /library API ──────────────────────────
@pytest.fixture
def box(tmp_path, monkeypatch):
    """A file-backed store holding more technologies than fit on one page."""
    from kb_store import FileStore

    store = FileStore(tmp_path)
    forge_tools.reset_store(store)

    for i in range(app.PER_PAGE + 3):
        store.save(f"lib{i:02d}", "v1", f"https://x.dev/{i}/docs/", "crawl",
                   [("Only Page", f"https://x.dev/{i}/docs/a", "some text")],
                   complete=True)
    store.save("effect", "v2", "https://effect.dev/docs/v2/", "crawl",
               [("Old Way", "https://effect.dev/docs/v2/a", "the deprecated way")],
               complete=True)
    store.save("effect", "v3", "https://effect.dev/docs/v3/", "crawl",
               [("Error Handling", "https://effect.dev/docs/v3/a", "fail fast"),
                ("Layers", "https://effect.dev/docs/v3/b", "wiring with npm")],
               complete=False)
    yield store
    forge_tools.reset_store(None)


def test_library_page_is_served():
    from fastapi.responses import FileResponse

    assert isinstance(app.library(), FileResponse)


def test_docs_page_is_served_and_not_swagger():
    # FastAPI mounts Swagger UI at /docs by default and silently wins the
    # route, which is how the product's own docs page came back as an API
    # playground the first time.
    from fastapi.responses import FileResponse

    assert app.app.docs_url is None
    assert app.app.redoc_url is None

    response = app.docs()
    assert isinstance(response, FileResponse)
    assert os.path.basename(response.path) == "docs.html"


@pytest.mark.parametrize("route", ["/", "/docs", "/library"])
def test_every_page_route_points_at_a_file_that_exists(route):
    served = {"/": "index.html", "/docs": "docs.html", "/library": "library.html"}
    assert os.path.isfile(os.path.join(app.STATIC, served[route]))


@pytest.mark.parametrize("view", [app.index, app.docs, app.library])
def test_pages_are_revalidated_rather_than_heuristically_cached(view):
    # Served with Last-Modified but no cache directive, a browser invents its
    # own freshness lifetime and reuses the file without asking — which is how
    # you pull a new UI and keep seeing the old one.
    assert view().headers["cache-control"] == "no-cache"


def test_static_assets_are_revalidated_too():
    from starlette.testclient import TestClient

    with TestClient(app.app) as client:
        for asset in ("/static/style.css", "/static/app.js"):
            r = client.get(asset)
            assert r.status_code == 200, asset
            assert r.headers["cache-control"] == "no-cache", asset
            # Revalidation still has to be cheap, so the validators must survive.
            assert r.headers.get("etag")


def test_library_index_pages_the_box(box):
    first = app.library_index(page=1)
    assert first["total"] == app.PER_PAGE + 4
    assert len(first["technologies"]) == app.PER_PAGE
    assert first["pages"] == 2
    assert first["backend"]["kind"] == "files"

    second = app.library_index(page=2)
    assert len(second["technologies"]) == 4
    assert not {t["name"] for t in first["technologies"]} & \
               {t["name"] for t in second["technologies"]}


def test_library_index_clamps_a_page_past_the_end(box):
    # A stale bookmark must not return an empty screen with no explanation.
    assert app.library_index(page=99)["page"] == 2


def test_library_index_filters_by_name(box):
    found = app.library_index(q="effect")
    assert found["total"] == 1
    assert found["technologies"][0]["name"] == "effect"
    assert found["technologies"][0]["versions"] == 2


def test_library_lists_every_version_newest_first(box):
    versions = app.library_versions("effect")["versions"]
    assert {v["version"] for v in versions} == {"v2", "v3"}
    assert versions[0]["version"] == "v3"


def test_library_reports_a_partial_harvest(box):
    v3 = next(v for v in app.library_versions("effect")["versions"]
              if v["version"] == "v3")
    assert v3["complete"] is False


def test_library_pages_are_scoped_to_their_version(box):
    v2 = app.library_pages("effect", "v2")
    assert [p["title"] for p in v2["pages"]] == ["Old Way"]

    v3 = app.library_pages("effect", "v3")
    assert [p["title"] for p in v3["pages"]] == ["Error Handling", "Layers"]


def test_library_page_comes_back_rendered(box):
    page = app.library_page("effect", "v3", 1)
    assert page["title"] == "Error Handling"
    assert "fail fast" in page["content"]
    assert "<p>" in page["html"], "the reader renders server-side HTML"


def test_the_reader_does_not_print_the_title_twice():
    # Extracted pages usually open with their own title, and the reader already
    # prints it above the document.
    stripped = app._without_repeated_title("# Error Handling\n\nbody", "Error Handling")
    assert stripped.strip() == "body"


def test_a_heading_that_is_not_the_title_is_left_alone():
    kept = app._without_repeated_title("# Something Else\n\nbody", "Error Handling")
    assert kept.startswith("# Something Else")


def test_the_site_name_in_a_stored_title_does_not_defeat_the_match():
    # Titles come from <title>, which carries the site name; the document's own
    # heading does not.
    stripped = app._without_repeated_title("# Index\n\nbody", "Index | Pydantic Docs")
    assert stripped.strip() == "body"


def test_library_search_finds_a_page_and_names_its_version(box):
    hits = app.library_search(q="npm")["hits"]
    assert hits and hits[0]["technology"] == "effect"
    assert hits[0]["version"] == "v3"


def test_library_search_can_be_scoped_to_one_version(box):
    scoped = app.library_search(q="way", tech="effect", version="v2")["hits"]
    assert scoped and all(h["version"] == "v2" for h in scoped)


def test_library_search_with_no_query_returns_nothing(box):
    assert app.library_search(q="   ")["hits"] == []


@pytest.mark.parametrize("call", [
    lambda: app.library_versions("nosuchthing"),
    lambda: app.library_pages("effect", "v9"),
    lambda: app.library_page("effect", "v3", 99),
])
def test_library_says_what_is_missing_rather_than_crashing(box, call):
    import json

    response = call()
    assert response.status_code == 404
    assert json.loads(response.body)["detail"]


# ── DocsStore: taking a harvest back out ─────────────────
# A harvest can be the wrong project, or a table of contents stored as though
# it were the documentation. Until this existed the store could only grow.
def test_one_version_can_be_removed_and_the_others_survive(box):
    assert app.library_forget_version("effect", "v2")["removed"] == 1
    left = [v["version"] for v in box.versions("effect")]
    assert left == ["v3"]


def test_removing_a_technology_removes_every_version(box):
    assert app.library_forget("effect")["removed"] == 2
    with pytest.raises(StoreError):
        box.versions("effect")


def test_removing_a_technology_leaves_the_others_alone(box):
    before = app.library_index()["total"]
    app.library_forget("effect")
    assert app.library_index()["total"] == before - 1
    assert box.versions("lib00")


@pytest.mark.parametrize("call", [
    lambda: app.library_forget("nosuchthing"),
    lambda: app.library_forget_version("effect", "v9"),
])
def test_deleting_something_that_is_not_there_says_so(box, call):
    import json

    response = call()
    assert response.status_code == 404
    assert json.loads(response.body)["detail"]


def test_the_files_are_actually_gone(box, tmp_path):
    """Not merely unlisted. A delete that leaves the Markdown on disk is a
    leak the index cannot see."""
    path = Path(box.entry("effect", "v2")["file"])
    assert path.exists()
    app.library_forget_version("effect", "v2")
    assert not path.exists()
