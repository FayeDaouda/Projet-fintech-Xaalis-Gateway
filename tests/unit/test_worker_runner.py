import asyncio
from pathlib import Path

import pytest

from xaalis.worker.runner import heartbeat, run_jobs


async def test_heartbeat_writes_file_and_stops_promptly(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    stop = asyncio.Event()
    task = asyncio.create_task(heartbeat(stop, beat))
    await asyncio.sleep(0.05)
    assert beat.exists()
    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_run_jobs_propagates_job_failure() -> None:
    async def failing(_: asyncio.Event) -> None:
        raise RuntimeError("boom")

    async def well_behaved(stop: asyncio.Event) -> None:
        await stop.wait()

    with pytest.raises(ExceptionGroup) as excinfo:
        await asyncio.wait_for(run_jobs([failing, well_behaved], asyncio.Event()), timeout=1)
    assert excinfo.group_contains(RuntimeError, match="boom")
