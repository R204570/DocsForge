"""Offline tests for reading a project's dependency manifests."""

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manifests


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


# ── versions ─────────────────────────────────────────────
@pytest.mark.parametrize("spec,expected", [
    ("==1.10.13", "1.10.13"),
    ("^3.1.0", "3.1.0"),
    (">=2.11,<3", "2.11"),
    ("~1.2", "1.2"),
    ("*", ""),
    ("", ""),
])
def test_the_pinned_number_is_read_out_of_a_spec(spec, expected):
    assert manifests.pinned_version(spec) == expected


def test_doc_versions_widen_from_exact_to_major():
    # A project pinned to 1.10.13 will not find docs under that exact string;
    # sites publish /1.10/ or /v1/. Widest last keeps the answer as precise as
    # the site allows.
    assert manifests.doc_versions("==1.10.13") == [
        "1.10.13", "v1.10.13", "1.10", "v1.10", "1", "v1"]


def test_an_unpinned_dependency_offers_no_version():
    assert manifests.doc_versions("*") == []


# ── formats ──────────────────────────────────────────────
def test_package_json(tmp_path):
    write(tmp_path, "package.json", """
        {"dependencies": {"effect": "^3.1.0", "zod": "3.23.8"},
         "devDependencies": {"vitest": "^1.0.0"}}
    """)
    deps = {d.name: d for d in manifests.read_project(tmp_path)}
    assert set(deps) == {"effect", "zod", "vitest"}
    assert deps["effect"].version == "^3.1.0"
    assert deps["effect"].ecosystem == "npm"


def test_requirements_txt_skips_flags_and_comments(tmp_path):
    write(tmp_path, "requirements.txt", """
        # a comment
        pydantic==1.10.13
        requests>=2.31   # trailing comment
        -r other.txt
        --index-url https://example.invalid
        fastapi
    """)
    deps = {d.name: d.version for d in manifests.read_project(tmp_path)}
    assert set(deps) == {"pydantic", "requests", "fastapi"}
    assert deps["pydantic"] == "==1.10.13"
    assert deps["fastapi"] == ""


def test_pyproject_pep621_and_poetry(tmp_path):
    write(tmp_path, "pyproject.toml", """
        [project]
        dependencies = ["httpx>=0.27", "rich"]

        [tool.poetry.dependencies]
        python = "^3.11"
        typer = "^0.12"
        pandas = {version = "^2.0", extras = ["all"]}
    """)
    deps = {d.name: d.version for d in manifests.read_project(tmp_path)}
    assert "httpx" in deps and "rich" in deps and "typer" in deps and "pandas" in deps
    assert "python" not in deps, "the interpreter is not a dependency to document"
    assert deps["pandas"] == "^2.0", "a table-form spec still yields its version"


def test_cargo_and_go(tmp_path):
    write(tmp_path, "Cargo.toml", """
        [dependencies]
        serde = "1.0"
        tokio = {version = "1.38", features = ["full"]}
    """)
    write(tmp_path, "go.mod", """
        module example.com/x
        go 1.22

        require (
            github.com/gin-gonic/gin v1.10.0
            golang.org/x/sync v0.7.0 // indirect
        )
    """)
    deps = {d.name: d for d in manifests.read_project(tmp_path)}
    assert deps["serde"].ecosystem == "crates"
    assert deps["tokio"].version == "1.38"
    assert deps["github.com/gin-gonic/gin"].ecosystem == "go"
    assert deps["golang.org/x/sync"].version == "v0.7.0"


# ── walking ──────────────────────────────────────────────
def test_vendor_directories_are_not_walked(tmp_path):
    write(tmp_path, "package.json", '{"dependencies": {"real": "1.0.0"}}')
    write(tmp_path, "node_modules/left-pad/package.json",
          '{"dependencies": {"transitive": "1.0.0"}}')
    names = {d.name for d in manifests.read_project(tmp_path)}
    assert names == {"real"}, "node_modules holds one manifest per package"


def test_a_pinned_version_beats_an_unpinned_mention_of_the_same_package(tmp_path):
    write(tmp_path, "requirements.txt", "pydantic\n")
    write(tmp_path, "api/requirements.txt", "pydantic==1.10.13\n")
    deps = {d.name: d.version for d in manifests.read_project(tmp_path)}
    assert deps["pydantic"] == "==1.10.13"


def test_a_project_with_no_manifests_is_empty_not_an_error(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    assert manifests.read_project(tmp_path) == []


def test_malformed_manifests_are_skipped_rather_than_fatal(tmp_path):
    write(tmp_path, "package.json", "{ this is not json")
    write(tmp_path, "requirements.txt", "requests>=2.31\n")
    names = {d.name for d in manifests.read_project(tmp_path)}
    assert names == {"requests"}
