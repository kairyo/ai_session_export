"""Find verifiable passages in an explicitly scoped Codex Markdown archive."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


GUIDE_DIGEST = "b1770314d40f77a41b8cf3b2cd1bf5b1ab383fef40ce95d4d08593ed6cedf68f"
ROLE = re.compile(r"## (?:Assistant|User)(?: \[\d{2}:\d{2}\])?")


def read_header(stream):
    """Read the exporter's own frontmatter format, leaving the file at its body."""
    if stream.readline().rstrip("\r\n") != "---":
        return None
    fields = {}
    for line_number, line in enumerate(stream, 2):
        if line.rstrip("\r\n") == "---":
            if fields.get("source") == "codex" and all(fields.get(k) for k in ("session_id", "title", "date")):
                return fields, line_number
            break
        key, _, value = line.partition(":")
        if key == "source":
            fields[key] = value.strip()
        elif key in {"session_id", "title", "date", "project_directory"}:
            try:
                decoded = json.loads(value)
            except ValueError:
                return None
            if not isinstance(decoded, str):
                return None
            fields[key] = decoded
    return None


def archive_files(roots):
    paths = {}
    for root in roots:
        directory = root.resolve(strict=True)
        for path in sorted(directory.iterdir()):
            if path.suffix != ".md" or path.is_symlink() or not path.is_file():
                continue
            # The upstream manifest uses one stripped UTF-8 path per line.
            if str(path).splitlines() != [str(path)] or str(path).strip() != str(path):
                raise ValueError("Archive filename cannot be represented in the semantic file list")
            try:
                with path.open(encoding="utf-8") as stream:
                    header = read_header(stream)
            except FileNotFoundError:
                continue
            if header:
                paths[path] = header[0]
    return paths


def header_matches(fields, terms):
    values = [fields[k] for k in ("title", "date", "session_id")]
    values.append(Path(fields.get("project_directory", "")).name)
    return any(term.search(value) for term in terms for value in values)


def lexical_candidates(files, literals, patterns):
    rg = shutil.which("rg")
    if not rg or any("\n" in term or "\r" in term for term in literals):
        yield from files
        return
    paths = list(files)
    command = [rg, "--no-config", "--files-with-matches", "--null", "--fixed-strings", "--ignore-case", "--text"]
    for literal in literals:
        command.extend(["-e", literal])
    for start in range(0, len(paths), 64):
        batch = paths[start:start + 64]
        result = subprocess.run([*command, "--", *batch], capture_output=True)
        if result.returncode not in (0, 1):
            yield from batch
            continue
        matches = {Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}
        for path in batch:
            if path in matches or header_matches(files[path], patterns):
                yield path


def passage(path, *, terms=(), chunk=None):
    """Find a literal quote in the current file, including its physical line number."""
    if path.is_symlink():
        return None
    try:
        stream = path.open(encoding="utf-8")
    except FileNotFoundError:
        return None
    with stream:
        header = read_header(stream)
        if not header:
            return None
        fields, last_header_line = header
        project = Path(fields.get("project_directory", "")).name
        match_header = header_matches(fields, terms)
        excerpt = []
        if chunk is not None:
            excerpt = [s.strip() for s in chunk["text"].splitlines() if s.strip()]
            if excerpt and excerpt[0] == chunk["header"].strip():
                excerpt.pop(0)  # The upstream chunker can repeat a heading mid-turn.
            if not excerpt or chunk["metadata"].get("session_id") != fields["session_id"]:
                return None
        window = deque(maxlen=len(excerpt))
        first = None
        in_turn = False
        for number, raw in enumerate(stream, last_header_line + 1):
            line = raw.strip()
            if ROLE.fullmatch(line):
                in_turn = True
                window.clear()
                continue
            if not in_turn or not line:
                continue
            if excerpt:
                window.append((number, line))
                if line == excerpt[-1] and [text for _, text in window] == excerpt:
                    pos, text = next(((n, s) for n, s in window if any(c.isalnum() for c in s)), window[0])
                    first = (pos, text[:320])
                    break
                continue
            hits = [match.start() for term in terms if (match := term.search(line))]
            if hits:
                start = max(0, min(hits) - 60)
                first = (number, line[start:start + 320])
                break
            if match_header and first is None:
                first = (number, line[:320])
        if first:
            return {"source": fields["source"], "session_id": fields["session_id"], "title": fields["title"],
                    "date": fields["date"], "project": project, "file": str(path), "line": first[0], "quote": first[1]}
    return None


def loopback_url(value):
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment):
        raise argparse.ArgumentTypeError("use a loopback embeddings base URL such as http://127.0.0.1:11434/v1")
    return value


def semantic(args, paths, command, extra=()):
    # Pin the destination and ignore ambient provider keys, dotenv files, proxies and fallbacks.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "SEMANTIC_SEARCH_"))}
    env.update(OPENAI_API_KEY="local-only", NO_PROXY="*", no_proxy="*")
    with tempfile.TemporaryDirectory(prefix="recall-scope-") as temp:
        manifest = Path(temp) / "files.txt"
        manifest.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "semantic_search_skill.cli", "--env-file", os.devnull,
             "--base-url", args.base_url, "--model", args.model, command, "--file-list", str(manifest),
             "--cache-dir", str(args.cache_dir), *extra],
            env=env, capture_output=True, text=True, timeout=1800 if command == "rebuild" else 60,
        )
    if proc.returncode:
        raise ValueError(f"semantic-search {command} failed (exit {proc.returncode}): {proc.stderr.strip()[-650:]}")
    return json.loads(proc.stdout)


def lookup(args, files):
    literals = [s.strip() for s in [args.query, *args.term] if s.strip()]
    terms = [re.compile(re.escape(s), re.IGNORECASE) for s in literals]
    if not args.query.strip():
        raise ValueError("query must contain text")
    evidence = {}

    def add(hit):
        if hit:
            evidence.setdefault((hit["source"], hit["session_id"]), hit)

    for path in lexical_candidates(files, literals, terms):
        add(passage(path, terms=terms))
        if len(evidence) >= args.limit:
            break
    method = "lexical" if evidence else "none"
    if files and not evidence:
        if not (args.base_url and args.model and args.cache_dir):
            raise ValueError("no literal match; semantic lookup needs --base-url, --model and --cache-dir")
        hits = semantic(args, files, "query", ["--no-refresh", "--query", args.query,
                                               "--top-k", str(args.limit * 8)])
        for hit in hits:
            chunk = hit["chunk"]
            path = Path(chunk["source_file"])
            if path in files and hit["score"] > 0 and files[path]["session_id"] == chunk["metadata"].get("session_id"):
                add(passage(path, chunk=chunk))
            if len(evidence) >= args.limit:
                break
        if evidence:
            method = "semantic"
    return {"method": method, "evidence": list(evidence.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="run one device export, then update its retrieval index")
    index = commands.add_parser("index", help="update the index from existing Codex Markdown")
    find = commands.add_parser("find", help="retrieve source quotes for another chat")
    for sub in (refresh, index, find):
        sub.add_argument("--cache-dir", type=Path, required=sub is not find)
        sub.add_argument("--base-url", type=loopback_url, required=sub is not find)
        sub.add_argument("--model", required=sub is not find)
    for sub in (index, find):
        sub.add_argument("--archive", type=Path, action="append", required=True, help="one device's codex output directory (repeatable)")
    refresh.add_argument("--codex-dir", type=Path, action="append", required=True)
    refresh.add_argument("--session-index", type=Path, required=True)
    refresh.add_argument("--output-dir", type=Path, required=True, help="this device's export base")
    refresh.add_argument("--state-file", type=Path, required=True)
    refresh.add_argument("--also-archive", type=Path, action="append", default=[], help="additional read-only codex output directory")
    find.add_argument("--query", required=True)
    find.add_argument("--term", action="append", default=[], help="additional literal name or spelling (repeatable)")
    find.add_argument("--limit", type=int, default=3)
    find.add_argument("--guide", type=Path, help="include the pinned context-infrastructure archive guide for a new chat")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            from .recall_refresh import refresh_once
            result = refresh_once(args)
        else:
            files = archive_files(args.archive)
            if args.command == "index":
                result = semantic(args, files, "rebuild") if files else {"files_scanned": 0}
            else:
                if args.limit < 1:
                    raise ValueError("limit must be positive")
                guide = None
                if args.guide:
                    data = args.guide.read_bytes()
                    if hashlib.sha256(data).hexdigest() != GUIDE_DIGEST:
                        raise ValueError("the guide differs from the pinned context-infrastructure archive guide")
                    guide = data.decode("utf-8")
                result = lookup(args, files)
                if guide:
                    result["retrieval_guide"] = guide
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"recall: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
