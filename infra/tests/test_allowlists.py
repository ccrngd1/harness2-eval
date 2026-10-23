"""N.5: the egress allowlists are FROZEN byte-for-byte (NetOps §5.1).
Any edit to these files must fail the build until NetOps re-reviews."""

import pathlib

SCRIPTS = pathlib.Path(__file__).parents[1] / "scripts" / "proxy"

# §5.1 setup-phase CONNECT allowlist — the frozen set, exactly.
SETUP_CONNECT = {
    "nodejs.org",
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "models.dev",
}

# §5.1 setup-phase plain-HTTP (apt) allowlist.
SETUP_HTTP80 = {
    "us-east-1.ec2.archive.ubuntu.com",
    "security.ubuntu.com",
}


def _entries(name: str) -> set[str]:
    lines = (SCRIPTS / name).read_text().splitlines()
    return {
        ln.strip()
        for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    }


def test_setup_connect_allowlist_frozen():
    assert _entries("allow-setup.txt") == SETUP_CONNECT


def test_setup_http80_allowlist_frozen():
    assert _entries("allow-setup-http80.txt") == SETUP_HTTP80


def test_run_allowlist_empty():
    assert _entries("allow-run.txt") == set(), "§5.1: run-phase egress is EMPTY"


def test_closed_allowlist_empty():
    assert _entries("allow-closed.txt") == set(), "closed-phase egress is EMPTY"


def test_no_contamination_names_on_any_list():
    # §5.2 names must never creep onto any allowlist. (models.dev is the
    # §5.1-sanctioned exception during SETUP only: the catalog is baked
    # before the first rollout and the run/closed lists stay empty.)
    contamination = {
        "huggingface.co",
        "hf.co",
        "raw.githubusercontent.com",
        "mcp.exa.ai",
        "opencode.ai",
    }
    for name in ("allow-setup.txt", "allow-setup-http80.txt", "allow-run.txt", "allow-closed.txt"):
        assert not (_entries(name) & contamination), f"contamination name on {name}"
