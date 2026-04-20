# codexmcp — 两个 Codex CLI 对话写小说

一个演示项目：用 MCP 协议同时启动两个独立的 `codex` CLI 实例，让它们像两个人类协作者一样互相发消息——一个负责写小说（Writer），一个负责审稿（Reviewer）——直到 Reviewer 说 `APPROVED` 对话才结束。

Python 主程序只扮演**信使**，把一方的回复原样转给另一方，不注入任何模板化指令。

## 架构

```
      ┌──────────────┐     Writer 的原话      ┌────────────────┐
      │   Writer     │ ───────── 转发 ──────▶ │    Reviewer    │
      │  codex CLI   │                        │   codex CLI    │
      │  (thread A)  │ ◀──────── 转发 ─────── │  (thread B)    │
      └──────┬───────┘     Reviewer 的原话     └────────┬───────┘
             │                                          │
             └───────── 共享工作目录 ./output ──────────┘
                         novel.md  /  review.md
```

核心要点：

- **两个真正独立的 Codex agent**：Python 用 `subprocess` 各启动一个 `codex mcp-server`，它们是两个完整的 Codex 智能体进程，各自有自己的 LLM 会话、自己的文件工具、自己的思考过程
- **每个 codex 维持长会话**：首次调用 `codex` 工具拿到 `threadId`，之后所有"对方发来的消息"都通过 `codex-reply` 工具喂进同一个 thread，保持思维和上下文连贯
- **Python 只当信使**：不设计对话流程、不拼模板。Writer 回复什么，Python 原样转给 Reviewer；Reviewer 回复什么，Python 原样转回 Writer
- **终止条件**：Reviewer 自己判定小说达标时，在回复末尾写 `APPROVED`，Python 抓到关键字就收工。超过 3 轮兜底结束
- **零额外 LLM 成本**：不用 OpenAI SDK、不用 Agents SDK、不需要 API key。codex 子进程直接复用 `~/.codex/auth.json` + `~/.codex/config.toml`

## 目录

```
codexmcp/
├── TASK.md                       # 原始需求文档
├── novel-review/
│   ├── main.py                   # 全部调度逻辑（一个文件）
│   ├── pyproject.toml            # uv 依赖（只依赖 mcp）
│   ├── README.md                 # 子项目说明
│   ├── output/                   # 运行产物（已 gitignore）
│   │   ├── novel.md              # 最终小说
│   │   └── review.md             # 最终审评（末尾带 APPROVED）
│   └── logs/                     # 调度器日志（已 gitignore）
│       ├── transcript.md         # 人类可读的对话轨迹
│       └── events.jsonl          # codex 内部事件流（工具调用、推理、patch）
```

## 前置条件

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) —— 项目用它管环境和依赖
- [Codex CLI](https://github.com/openai/codex) 已安装并登录，`codex mcp-server` 可用
  - 即 `~/.codex/auth.json` 与 `~/.codex/config.toml` 已配置好

## 快速开始

```bash
cd novel-review
uv run main.py "写一篇关于时间旅行者的短篇科幻小说，800字左右"
```

首次运行 `uv` 会自动解析依赖、建 venv。之后：

- 看进度：控制台实时打每个 codex 的工具调用、耗时、消息摘要
- 看对话：`logs/transcript.md`，气泡式呈现每轮每个角色说了什么
- 看细节：`logs/events.jsonl`，codex 内部的每条事件（每次运行上千条）
- 看作品：`output/novel.md` 和 `output/review.md`

## 对话流程示例

```
========== Round 1 ==========
[Writer] 启动会话，创作第 1 稿...
  [Writer] · patch: novel.md
  [Writer] · exec: cat novel.md
[Writer] 完成 (55.7s, threadId=019daa55)
[Writer → Reviewer] 我写了一篇约百字的时间旅行微型小说，核心是"未来的自己
                     劝现在的自己别改写过去"，走的是克制一点的伤感反转。请
                     审阅 novel.md。
[Reviewer] 启动会话，审第 1 稿...
  [Reviewer] · exec: cat novel.md
  [Reviewer] · patch: review.md
[Reviewer] 完成 (76.0s, threadId=019daa43)
[Reviewer → Writer] 整体已达标。节奏是稳的，反转和伤感都收得比较准...
                     APPROVED
========== Reviewer APPROVED，对话结束 ==========
```

## 技术备注

- **threadId 的获取**：`codex` 工具返回的 `structuredContent` 里带 `threadId`，保存后用 `codex-reply` 即可接续会话
- **自定义通知处理**：codex mcp-server 会发送标准 MCP 之外的 `codex/event` 通知（内容是 agent 内部事件流），标准 MCP Python SDK 会打一堆 pydantic 验证警告。`main.py` 用 `logging.Filter` 从这些警告里解析出结构化事件写入 JSONL，同时压掉控制台噪音
- **工作目录隔离**：两个 codex 的 `cwd` 都设为 `./output`，只看得到 `novel.md` / `review.md`。调度器的 `transcript.md` 与 `events.jsonl` 放在 `./logs`，不污染 codex 的工作区

## License

MIT
