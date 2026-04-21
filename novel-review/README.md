# Novel Review

三个 Codex CLI 实例通过消息对话协作写文章与双重检查。

## 架构

```text
                       Python 调度器 / 邮差
                               |
      -------------------------------------------------------
      |                         |                           |
      ▼                         ▼                           ▼
┌──────────────┐        ┌────────────────┐          ┌────────────────┐
│    Writer    │        │  StyleChecker  │          │   IdeaChecker  │
│  codex CLI   │        │   codex CLI    │          │   codex CLI    │
│  (thread A)  │        │   (thread B)   │          │   (thread C)   │
└──────┬───────┘        └────────┬───────┘          └────────┬───────┘
       │                         │                           │
       └──────────────── 共享工作目录 ./output ───────────────┘
             article.md / style_review.md / creative_review.md
```

- Python 主程序启动三个 `codex mcp-server` 子进程，每个都是一个完整的 Codex agent
- 每个 codex 都维持自己的长会话：首次用 `codex` 初始化拿到 `threadId`，后续用 `codex-reply` 继续
- Writer 负责写一篇约 300 字的文章到 `output/article.md`
- `StyleChecker` 只检查文风，把意见写到 `output/style_review.md`
- `IdeaChecker` 只检查创意，把意见写到 `output/creative_review.md`
- Python 只当信使：先把 Writer 的消息分别转给两位检查者，等两位检查者都回复后，再把两份反馈一起转给 Writer
- 只有当两位检查者都在回复里写出 `APPROVED` 时，对话才结束；否则最多 3 轮

整个过程不需要 Agents SDK 或 OpenAI API Key。`codex` 本身使用 `~/.codex/` 下的配置处理模型调用。

## 前置条件

- Python 3.10+
- `uv`
- `codex` CLI 已安装并配置好，例如 `~/.codex/auth.json`、`~/.codex/config.toml`

## 运行

```bash
uv run main.py "写一篇关于时间旅行者的短文"
```

## 产物

| 路径 | 说明 |
|---|---|
| `output/article.md` | Writer 产出的最终文章 |
| `output/style_review.md` | 文风检查意见，达标时末尾带 `APPROVED` |
| `output/creative_review.md` | 创意检查意见，达标时末尾带 `APPROVED` |
| `logs/transcript.md` | 人类可读的完整对话轨迹，含每轮消息气泡 |
| `logs/events.jsonl` | Codex 内部事件流，例如工具调用、patch、task_complete 等 |
