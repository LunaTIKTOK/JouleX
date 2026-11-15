"""M1.2 — CI workflow (GitHub Actions).

The workflow file must exist, parse, trigger on push/PR to main, pin
its tool versions, and run the full local verification set (pinned
measurement, pytest suite, demo.py, quickstart.py, m4_demo.py,
sandbox parity/exercise/smoke) — with no secrets, no live Hedera, and
no step that only works on the builder's machine (no hardcoded local
paths: a fresh runner checks out to a different directory).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VERIFY_PIN = ROOT / "scripts" / "verify_pin.py"
SANDBOX = ROOT / "sandbox"


def _load_workflow():
    # PyYAML parses the bare key `on:` as boolean True (YAML 1.1).
    with open(WORKFLOW, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _run_steps(cfg):
    steps = []
    for job in cfg.get("jobs", {}).values():
        for step in job.get("steps", []):
            if isinstance(step, dict) and step.get("run"):
                steps.append(step["run"])
    return steps


def test_workflow_file_exists():
    assert WORKFLOW.is_file(), "M1.2: .github/workflows/ci.yml missing"


def test_workflow_parses_as_yaml():
    cfg = _load_workflow()
    assert isinstance(cfg, dict)
    assert "jobs" in cfg and isinstance(cfg["jobs"], dict)
    assert cfg["jobs"], "workflow defines no jobs"


def test_workflow_triggers_on_push_and_pr_to_main():
    cfg = _load_workflow()
    on = cfg.get("on", cfg.get(True))
    assert isinstance(on, dict), "workflow has no `on:` triggers"
    assert "push" in on and "pull_request" in on
    for trigger in ("push", "pull_request"):
        branches = on[trigger].get("branches", [])
        assert "main" in branches, f"{trigger} does not target main"


def test_workflow_pins_tool_versions():
    cfg = _load_workflow()
    text = json.dumps(cfg)
    assert "setup-python" in text
    assert "3.12" in text, "CI must pin Python 3.12 (the version the suite runs on)"
    assert "setup-node" in text
    assert '"24"' in text or "24" in text, "CI must pin a Node major version"


def test_workflow_runs_full_suite():
    runs = _run_steps(_load_workflow())
    joined = "\n".join(runs)
    assert "python3 -m pytest tests/ -q" in joined


def test_workflow_runs_demo_quickstart_and_m4_demo():
    runs = _run_steps(_load_workflow())
    joined = "\n".join(runs)
    assert "python3 demo.py" in joined
    assert "python3 quickstart.py" in joined
    assert "python3 scripts/m4_demo.py" in joined


def test_workflow_runs_sandbox_checks():
    runs = _run_steps(_load_workflow())
    joined = "\n".join(runs)
    for script in ("tests/parity.cjs", "tests/exercise.cjs", "tests/smoke.cjs"):
        assert f"node {script}" in joined, f"CI never runs sandbox {script}"


def test_workflow_verifies_pinned_measurement():
    runs = _run_steps(_load_workflow())
    joined = "\n".join(runs)
    assert "verify_pin.py" in joined, "CI must fail fast on a stale measurement pin"


def test_workflow_uses_no_secrets_or_live_hedera():
    cfg = _load_workflow()
    text = json.dumps(cfg)
    assert "secrets." not in text, "CI must not consume any secret"
    for banned in ("JOULEX_HCS_PRIVATE_KEY", "JOULEX_HCS_OPERATOR",
                   "hcs_submit", "mainnet", "git push"):
        assert banned not in text, f"CI must not reference {banned}"


def test_verify_pin_script_exists():
    assert VERIFY_PIN.is_file(), "scripts/verify_pin.py missing"


def test_verify_pin_passes_on_real_tree():
    proc = subprocess.run(
        [sys.executable, str(VERIFY_PIN)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PIN OK" in proc.stdout


def _build_tmp_tree(tmp_path):
    shutil.copytree(ROOT / "joulex", tmp_path / "joulex")
    (tmp_path / "data" / "provenance").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "measurements.json",
                tmp_path / "data" / "measurements.json")
    for link in (ROOT / "data" / "provenance").glob("*.link.json"):
        shutil.copy(link, tmp_path / "data" / "provenance" / link.name)
    return tmp_path


def test_verify_pin_fails_on_tampered_tree(tmp_path):
    root = _build_tmp_tree(tmp_path)
    target = root / "joulex" / "lmsr.py"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n# tampered for the pin test\n")
    proc = subprocess.run(
        [sys.executable, str(VERIFY_PIN), "--root", str(root)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, "tampered tree must fail the pin check"
    assert "mismatch" in (proc.stdout + proc.stderr).lower()


def test_sandbox_checks_have_no_hardcoded_local_path():
    for script in ("parity.cjs", "exercise.cjs", "smoke.cjs"):
        text = (SANDBOX / "tests" / script).read_text(encoding="utf-8")
        assert "/home/hatch" not in text, f"{script} hardcodes the builder path"
        assert "/home/" not in text, f"{script} hardcodes a home directory"


def test_sandbox_smoke_runs_from_another_directory(tmp_path):
    """Portability proof: the sandbox checks must pass when the repo
    lives somewhere else entirely (a fresh runner's checkout path) —
    and must read THAT copy's HTML, not the builder's absolute path.
    The tampered-copy run is the negative control: it must fail."""
    dest = tmp_path / "joulex-copy"
    shutil.copytree(SANDBOX, dest)
    env = dict(os.environ)
    env["NODE_PATH"] = str(ROOT / "node_modules")

    def run_smoke(cwd):
        return subprocess.run(
            ["node", "tests/smoke.cjs"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
        )

    proc = run_smoke(dest)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SMOKE OK" in proc.stdout

    # Negative control: break the COPY's banner; the run must now fail,
    # proving it read the copy (a hardcoded repo path would still pass).
    html_path = dest / "joulex-sandbox.html"
    html = html_path.read_text(encoding="utf-8")
    assert "PAPER ONLY" in html
    html_path.write_text(html.replace("PAPER ONLY", "XXXXX"), encoding="utf-8")
    proc = run_smoke(dest)
    assert proc.returncode != 0, "smoke run ignored the copied HTML"
