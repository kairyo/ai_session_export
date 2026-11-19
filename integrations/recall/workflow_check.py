"""Four-component acceptance test. All input, state, archives and sockets are test-owned."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from ai_session_export.sources.codex import zstd


DECISION = "Restore the lost files from the nightly backup stored in the vault."
QUESTION = "How do I recover a deleted document using an older copy?"
SESSION = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("SEMANTIC_SEARCH_FALLBACK_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.delenv("SEMANTIC_SEARCH_FALLBACK_MODEL", raising=False)
    model_dir = os.environ.get("RECALL_TEST_MODEL_DIR")
    if model_dir:
        from model2vec import StaticModel
        model = StaticModel.from_pretrained(Path(model_dir).resolve(strict=True))

    def vectors(texts):
        if model_dir:
            return model.encode(texts).tolist()
        topics = (("backup", "vault", "recover", "copy"), ("pottery", "clay", "kiln"))
        return [[float(any(word in text.lower() for word in topic)) for topic in topics] + [0.01]
                for text in texts]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/embeddings" or self.headers.get("Authorization") != "Bearer local-only":
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if (tmp_path / "reject-embeddings").exists():
                self.send_error(400, "Unknown fixture model")
                return
            texts = body["input"]
            seen.extend(texts)
            data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors(texts))]
            response = json.dumps({"object": "list", "data": data, "model": body["model"],
                                   "usage": {"prompt_tokens": 0, "total_tokens": 0}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield ["--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                   "--model", "potion-base-2M" if model_dir else "fixture-vectors"], seen
        finally:
            server.shutdown()
            worker.join()


def cli(cwd, *args, check=True):
    proc = subprocess.run([sys.executable, "-m", "ai_session_export.recall", *map(str, args)],
                          cwd=cwd, text=True, capture_output=True, timeout=40)
    if not check:
        return proc
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def rollout(device, session, title, answer, *, compressed):
    sessions = device / "input"
    sessions.mkdir(parents=True)
    stamp = "2026-01-15T12:00:00Z"
    events = [
        ("session_meta", {"id": session, "cwd": "/synthetic/demo-project"}),
        ("event_msg", {"type": "user_message", "message": "Record this workshop note."}),
        ("event_msg", {"type": "agent_message", "message": answer}),
    ]
    payload = "".join(json.dumps({"type": typ, "timestamp": stamp, "payload": data}) + "\n"
                      for typ, data in events).encode()
    path = sessions / f"rollout-2026-01-15T12-00-00-{session}.jsonl"
    if compressed:
        path = path.with_suffix(".jsonl.zst")
        payload = zstd.compress(payload)
    path.write_bytes(payload)
    (device / "index.jsonl").write_text(json.dumps({"id": session, "thread_name": title}) + "\n")
    return path


def refresh_args(device, provider):
    return ["refresh", "--codex-dir", device / "input", "--session-index", device / "index.jsonl",
            "--output-dir", device / "archive", "--state-file", device / "state.json",
            "--cache-dir", device / "cache", *provider]


def test_cross_device_recall_in_a_fresh_process(tmp_path, endpoint):
    provider, seen = endpoint
    first, second = tmp_path / "M1", tmp_path / "M4"
    rollout(first, SESSION, "恢复演练", DECISION, compressed=True)
    rollout(second, SECOND, "陶艺作坊", "Fire the clay pottery in the kiln.", compressed=False)
    first_archive, second_archive = (p / "archive" / "codex" for p in (first, second))
    assert cli(tmp_path, *refresh_args(first, provider))["index"]["files_updated"] == 1
    first_file, = first_archive.glob("*.md")

    second_archive.mkdir(parents=True)
    shutil.copy(first_file, second_archive / "replica.md")
    (second_archive / "README.md").write_text("Unrelated project notes")
    foreign = tmp_path / "outside.md"
    foreign.write_text(first_file.read_text().replace(DECISION, "FOREIGN_MARKER"))
    (second_archive / "outside-link.md").symlink_to(foreign)
    result = cli(tmp_path, *refresh_args(second, provider), "--also-archive", first_archive)
    assert "exported=1" in result["export"] and result["index"]["files_scanned"] == 3
    assert all("FOREIGN_MARKER" not in doc and "Unrelated project notes" not in doc for doc in seen)
    for device in (first, second):
        assert (device / "state.json").is_file() and (device / "cache" / "index.sqlite3").is_file()

    seen.clear()
    scope = ["--archive", first_archive, "--archive", second_archive]
    lexical = cli(tmp_path, "find", *scope, "--query", "上回怎么做的", "--term", "恢复演练")
    assert lexical["method"] == "lexical" and [e["session_id"] for e in lexical["evidence"]] == [SESSION]
    assert seen == []
    missing_cache = tmp_path / "missing-cache"
    unindexed = cli(tmp_path, "find", *scope, "--cache-dir", missing_cache,
                    *provider, "--query", QUESTION, check=False)
    assert unindexed.returncode and not missing_cache.exists() and seen == []

    guide = Path(os.environ["CONTEXT_ARCHIVE_GUIDE"]).resolve(strict=True)
    search = ["find", *scope, "--cache-dir", second / "cache", *provider, "--query", QUESTION]
    packet = cli(tmp_path, *search, "--guide", guide)
    assert packet["retrieval_guide"] == guide.read_text(encoding="utf-8")
    assert packet["method"] == "semantic" and seen == [QUESTION]
    matches = packet["evidence"]
    assert len({e["session_id"] for e in matches}) == len(matches)
    hit = matches[0]
    assert (hit["session_id"], hit["title"], hit["source"], hit["project"]) == (
        SESSION, "恢复演练", "codex", "demo-project")
    assert hit["date"] == "2026-01-15" and hit["quote"] == DECISION and "score" not in hit
    assert Path(hit["file"]).read_text().splitlines()[hit["line"] - 1] == DECISION

    # A narrower scope using the same cache cannot surface the other Mac's file.
    copy = second_archive / "replica.md"
    copy.unlink()
    scoped = cli(tmp_path, "find", "--archive", second_archive, "--cache-dir", second / "cache",
                 *provider, "--query", QUESTION)
    assert all(e["session_id"] == SECOND for e in scoped["evidence"])
    # A cached passage that has since changed cannot supply an old quote.
    first_file.write_text(first_file.read_text().replace(DECISION, "This passage was revised."))
    stale = cli(tmp_path, *search)
    assert all(e["quote"] != DECISION for e in stale["evidence"])
    assert seen == [QUESTION] * 3  # No documents submitted by these three lookups.
    invalid = cli(tmp_path, *search, "--guide", foreign, check=False)
    assert invalid.returncode and "differs from the pinned" in invalid.stderr and seen == [QUESTION] * 3


def test_refresh_failure_and_retry(tmp_path, endpoint):
    provider, seen = endpoint
    device = tmp_path / "M1"
    path = rollout(device, SESSION, "模拟", DECISION, compressed=True)
    args = refresh_args(device, provider)
    lock_path = device / "state.json.recall.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = cli(tmp_path, *args, check=False)
        assert busy.returncode and "another recall refresh" in busy.stderr
    payload = path.read_bytes()
    path.write_bytes(payload[:-4])
    failed = cli(tmp_path, *args, check=False)
    assert failed.returncode and "export-codex failed" in failed.stderr
    assert seen == [] and not (device / "cache").exists()
    path.write_bytes(payload)
    failure_flag = tmp_path / "reject-embeddings"
    failure_flag.touch()
    rejected = cli(tmp_path, *args, check=False)
    assert rejected.returncode and "index-codex failed" in rejected.stderr
    archive, = (device / "archive" / "codex").glob("*.md")
    assert DECISION in archive.read_text() and seen == []
    failure_flag.unlink()
    repaired = cli(tmp_path, *args)
    assert "exported=0" in repaired["export"] and repaired["index"]["files_updated"] == 1
    seen.clear()
    again = cli(tmp_path, *args)
    assert "exported=0" in again["export"] and again["index"]["files_updated"] == 0 and seen == []
