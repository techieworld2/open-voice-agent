from pathlib import Path
import shutil
import subprocess

import pytest


def test_worklet_capture_playback_and_flush():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; install it to run AudioWorklet rendering tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "--test", "tests/worklet.test.cjs"], cwd=root,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
