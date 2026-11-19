# Recall a prior Codex decision

此流程供下一次对话查找以前的决定。两台 Mac 分别归档到自己的目录，可同时检索两个目录。结果带有会话标题、日期、原话、文件和行号。下面的验收只会生成模拟会话。

| Component | What it contributes |
| --- | --- |
| [ai_session_export](../README.md) | Reads the explicitly selected Codex inputs, including Zstandard rollouts, and writes Markdown with stable session IDs. |
| [process-launcher](https://github.com/grapeot/process-launcher/tree/60ed57adce9d6082ddf27de270c49e7ff801c7e6) | Its process manager runs a single export child and waits for its exit before launching the index child. Each invocation owns its children and temporary logs. |
| [semantic-search-skill](https://github.com/grapeot/semantic-search-skill/tree/a3a6f64765871f9fa068b9856cb5158ef5daa827) | Chunks the Markdown, stores vectors and SQLite metadata locally and ranks passages for a paraphrased question. |
| [context-infrastructure](https://github.com/grapeot/context-infrastructure/blob/0bb2f6e11080702c9078bb1e1c65f3880d40c6cd/rules/skills/ai_session_search_archive.md) | Supplies the agent's archive retrieval and citation guide. The optional `--guide` includes its verified text alongside the evidence so a new chat can use it. |

## Reproduce with generated sessions

Use Python 3.12+ on macOS or Linux for the optional launcher workflow. The base exporter supports Python 3.11+. From the repository root, use a dedicated virtual environment and an explicit checkout of the public guide.

```sh
uv pip install -e '.[dev]' -r integrations/recall/requirements.txt
guide_checkout="$(mktemp -d)"
git -C "$guide_checkout" init -q
git -C "$guide_checkout" fetch --depth=1 https://github.com/grapeot/context-infrastructure.git 0bb2f6e11080702c9078bb1e1c65f3880d40c6cd
git -C "$guide_checkout" checkout -q --detach FETCH_HEAD
export CONTEXT_ARCHIVE_GUIDE="$guide_checkout/rules/skills/ai_session_search_archive.md"
python -m pytest -q integrations/recall/workflow_check.py
```

The test creates two device roots, a compressed Codex session, a plain session and an unrelated decision. It runs the exporter, launcher and semantic index, opens a fresh process, reads the pinned guide and checks the quote against the Markdown. A short-lived loopback fixture supplies deterministic embeddings for CI. To exercise real embeddings with the same assertions, install `model2vec` in the test environment and set `RECALL_TEST_MODEL_DIR` to a local copy of [potion-base-2M](https://huggingface.co/minishlab/potion-base-2M/tree/389b9f64be5aa4ae7a6bc6fe95ef20ce485ae5da). The checked-out guide can be removed after testing or kept for future lookups.

## Run once on one device

Use an explicitly selected private output directory per writer. For example, each device can own its respective `M1/archive/codex` or `M4/archive/codex` in a shared drive. Keep export state, its adjacent concurrency lock and the vector cache local to each Mac. The cache stores archive text and machine-specific absolute paths. Wait for both Markdown folders to finish syncing before indexing them on a reader.

The variables below refer to paths and a local model chosen by the operator. `DEVICE` is this device's output root, `LOCAL` is private device storage, and `FIXTURES` holds the synthetic input tree. `EMBED_URL` points to an OpenAI-compatible loopback embeddings endpoint and `MODEL` names its loaded model.

```sh
ai-session-recall refresh --codex-dir "$FIXTURES/active" --codex-dir "$FIXTURES/archived" \
  --session-index "$FIXTURES/index.jsonl" --output-dir "$DEVICE/archive" \
  --state-file "$LOCAL/export.json" --cache-dir "$LOCAL/recall" \
  --base-url "$EMBED_URL" --model "$MODEL"
```

The launcher starts its two jobs on demand and exits. Export errors stop the index step. Rerun the same command after repair. An index failure is also retryable from the exported Markdown. To index existing output from both devices on a reader, use `index`. During a refresh, `--also-archive "$PEER/archive/codex"` adds an existing peer folder to this device's index.

```sh
ai-session-recall index --archive "$M1/archive/codex" --archive "$M4/archive/codex" \
  --cache-dir "$LOCAL/recall" --base-url "$EMBED_URL" --model "$MODEL"
```

## Use the evidence in the next chat

For a title, project, date or session ID, a literal lookup works with just `--archive` and `--query`. Add `--term` for a remembered spelling or alias. For a paraphrase, pass the same cache and model used during indexing. Choose only the archive roots relevant to the question.

```sh
ai-session-recall find --archive "$M1/archive/codex" --archive "$M4/archive/codex" \
  --cache-dir "$LOCAL/recall" --base-url "$EMBED_URL" --model "$MODEL" \
  --query "How do I recover a deleted document using an older copy?" \
  --limit 1 --guide "$CONTEXT_ARCHIVE_GUIDE"
```

The JSON response contains `method` and `evidence`. A new chat can quote each relevant `quote` and link the returned `file` and 1-based `line` using `[原文](<absolute-file-path:line>)`. The synthetic example retrieves “Restore the lost files from the nightly backup stored in the vault.” Check that a passage supports the answer. An empty list means the lookup produced no verified passage.

中文模拟验收也使用了两条主题不同的会话。用“两台 Mac 一起整理历史对话时，怎样防止互相把文件盖掉？”提问，本地模型返回《协作约定》第 18 行。原文是“每台电脑单独写自己的聊天归档目录，增量进度也各自保存在本机，避免覆盖。” 这项单独验证使用了 [BGE 中文小模型的固定 ONNX 版本](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)。

Each query builds its file list from the selected archive directories. Semantic lookup uses `--no-refresh`, sends only the question to the local embedding endpoint, and checks the retrieved text against the current file. Stale excerpts are discarded and duplicate session IDs appear once. If the index needs updating, refresh it separately. The consuming chat invokes the workflow on demand through its project guide.
