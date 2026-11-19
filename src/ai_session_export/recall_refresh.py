"""One-shot archive maintenance using process-launcher's child lifecycle."""

import asyncio
import fcntl
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from process_launcher.log import HeartbeatLogger, OutputLogger
from process_launcher.models import RunRequest
from process_launcher.process import ProcessManager


async def run_stage(manager, label, argv):
    finished = asyncio.get_running_loop().create_future()
    task = await manager.start_process(
        RunRequest(label=label, command=argv, timeout=1800, diagnose_on_failure=False),
        on_exit=finished.set_result,
    )
    try:
        info = await asyncio.shield(finished)
    except asyncio.CancelledError:
        await manager.stop_process(task.pid)
        await asyncio.shield(finished)
        raise
    output = Path(info.output_file).read_text(encoding="utf-8")
    if info.exit_code != 0:
        raise RuntimeError(f"{label} failed (exit {info.exit_code})\n{output.rstrip()}")
    return output


async def run_pipeline(args):
    if not any(path.is_dir() for path in args.codex_dir):
        raise ValueError("no supplied Codex input directory exists")
    export = [sys.executable, "-m", "ai_session_export.cli", "--source", "codex",
              "--codex-session-index", str(args.session_index), "--base-dir", str(args.output_dir),
              "--state-file", str(args.state_file)]
    for path in args.codex_dir:
        export.extend(["--codex-dir", str(path)])
    index = [sys.executable, "-m", "ai_session_export.recall", "index",
             "--base-url", args.base_url, "--model", args.model, "--cache-dir", str(args.cache_dir)]
    for path in [args.output_dir / "codex", *args.also_archive]:
        index.extend(["--archive", str(path)])

    with TemporaryDirectory(prefix="recall-launcher-") as log_dir:
        manager = ProcessManager(HeartbeatLogger(log_dir), OutputLogger(log_dir))
        exported = await run_stage(manager, "export-codex", export)
        indexed = await run_stage(manager, "index-codex", index)
    return {"export": exported.strip(), "index": json.loads(indexed)}


def refresh_once(args):
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_file.with_name(args.state_file.name + ".recall.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another recall refresh is using this device's state file") from None
        return asyncio.run(run_pipeline(args))
