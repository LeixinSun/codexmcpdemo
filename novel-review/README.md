# Novel Review

两个 Codex CLI 实例**通过消息对话**协作写小说 + 审稿。

## 架构

```
      ┌──────────────┐      Writer 的回复       ┌────────────────┐
      │   Writer     │ ───────── 转发 ────────▶ │    Reviewer    │
      │  codex CLI   │                          │   codex CLI    │
      │  (thread A)  │ ◀──────── 转发 ───────── │  (thread B)    │
      └──────┬───────┘      Reviewer 的回复      └────────┬───────┘
             │                                            │
             └──────── 共享工作目录 ./output ──────────────┘
                       novel.md  /  review.md
```

- Python 主程序启动两个 `codex mcp-server` 子进程，每个都是一个完整的 Codex agent（自带 LLM）
- 每个 codex **维持自己的长会话**（用 `codex` 初始化拿到 threadId，之后用 `codex-reply` 继续）
- Python **只当信使**：把 Writer 的回复原样转给 Reviewer，反之亦然。不注入固定模板
- Reviewer 判定达标就在回复里写 `APPROVED`，对话立即结束
- 最多 3 轮

整个过程不需要 Agents SDK 或 OpenAI API Key——codex 本身用 `~/.codex/` 下的配置处理所有 LLM 调用。

## 前置条件

- Python 3.10+、uv、codex CLI 已安装并配置好（`~/.codex/auth.json`、`~/.codex/config.toml`）

## 运行

```bash
uv run main.py "写一篇关于时间旅行者的短篇科幻小说，800字左右"
```

## 产物

| 路径 | 说明 |
|---|---|
| `output/novel.md`      | 最终小说（两个 codex 共享的"作品"） |
| `output/review.md`     | 最终审评意见（末尾带 `APPROVED`） |
| `logs/transcript.md`   | 人类可读的完整对话轨迹，含每轮消息气泡 |
| `logs/events.jsonl`    | codex 内部事件流（工具调用、推理、task_complete 等）|
