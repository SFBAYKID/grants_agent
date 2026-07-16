"""Production bot keepalive shell behavior and truthful heartbeat tests."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "run_bot.sh"
HEARTBEAT = re.compile(
    r"^grant_keepalive status=(healthy|restart_attempt|probe_error) "
    r"at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n$"
)


def _executable(path: Path, body: str) -> None:
    """Create one executable fake command inside an isolated test directory."""
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _environment(tmp_path: Path, pgrep_status: int) -> tuple[dict[str, str], Path]:
    """Build a temporary tenant home plus deterministic process/launch commands."""
    home = tmp_path / "home"
    app = home / "grants_agent"
    fake_bin = tmp_path / "bin"
    app.mkdir(parents=True)
    fake_bin.mkdir()
    (app / ".env").write_text("SECRET_TEST_VALUE=must-not-appear\n")
    _executable(
        fake_bin / "pgrep",
        f"#!/usr/bin/env bash\nexit {pgrep_status}\n",
    )
    _executable(
        fake_bin / "nohup",
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$HOME/nohup_calls"\n',
    )
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"})
    return env, home


def test_healthy_tick_logs_once_and_does_not_launch(tmp_path: Path) -> None:
    """A present bot emits one heartbeat without reading secrets or launching."""
    env, home = _environment(tmp_path, 0)
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert HEARTBEAT.fullmatch(result.stdout)
    assert "status=healthy" in result.stdout
    assert "must-not-appear" not in result.stdout
    assert not (home / "nohup_calls").exists()


def test_absent_bot_logs_attempt_and_launches_exact_command(tmp_path: Path) -> None:
    """A genuine not-found probe records an attempt and launches only Grant."""
    env, home = _environment(tmp_path, 1)
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert HEARTBEAT.fullmatch(result.stdout)
    assert "status=restart_attempt" in result.stdout
    calls = home / "nohup_calls"
    for _attempt in range(200):
        if calls.exists():
            break
        time.sleep(0.01)
    assert calls.read_text().strip() == (
        ".venv/bin/python -u -m grant_watch.slack.grant"
    )


def test_probe_error_fails_closed_without_launch(tmp_path: Path) -> None:
    """An unexpected pgrep failure cannot start a duplicate bot."""
    env, home = _environment(tmp_path, 2)
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 2
    assert HEARTBEAT.fullmatch(result.stdout)
    assert "status=probe_error" in result.stdout
    assert not (home / "nohup_calls").exists()
