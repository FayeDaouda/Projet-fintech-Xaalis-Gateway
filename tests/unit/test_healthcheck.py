import os
import time
from pathlib import Path

import pytest

from xaalis.infra import healthcheck


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    beat.touch()
    assert healthcheck.check_heartbeat(beat)


def test_stale_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    beat.touch()
    old = time.time() - 120
    os.utime(beat, (old, old))
    assert not healthcheck.check_heartbeat(beat)


def test_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    assert not healthcheck.check_heartbeat(tmp_path / "absent")


def test_probe_refuses_non_local_urls() -> None:
    assert not healthcheck.check_url("http://example.com/healthz")


def test_probe_reports_unreachable_local_port() -> None:
    assert not healthcheck.check_url("http://127.0.0.1:9/healthz", timeout=0.2)


def test_main_uses_heartbeat_file_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "beat"
    beat.touch()
    monkeypatch.setenv("XAALIS_HEALTHCHECK_FILE", str(beat))
    assert healthcheck.main() == 0
    beat.unlink()
    assert healthcheck.main() == 1
