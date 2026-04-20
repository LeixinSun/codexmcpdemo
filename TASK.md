# 任务：用 Codex MCP + Agents SDK 实现"写小说 + Review"闭环

## 需求

用 OpenAI Agents SDK 编排两个 Agent，共享同一个工作目录，协作完成小说创作：

- **Writer Agent**：根据主题写小说，保存为 novel.md。收到 Reviewer 反馈后修改小说。每次写完 handoff 给 Reviewer。
- **Reviewer Agent**：读取 novel.md，从文笔、情节、人物塑造等维度给出评审意见，写入 review.md。不通过则 handoff 回 Writer 并附修改建议；通过则输出 APPROVED 结束。

两个 Agent 各自连一个 Codex MCP Server（通过 MCPServerStdio 启动 `codex mcp-server`），cwd 都设为 `./output`。最多迭代 3 轮。

## 技术栈

- Python 3.10+，用 **uv** 管理环境和依赖
- 依赖：openai、openai-agents、python-dotenv
- 我用 mirror API，需要通过 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 环境变量配置，用 `set_default_openai_client(AsyncOpenAI(...))` 设置自定义 client
- Codex CLI 已装好，`codex mcp-server` 可用

## 项目结构

```
novel-review/
├── .env              # OPENAI_API_KEY, OPENAI_BASE_URL
├── pyproject.toml    # uv 项目配置，声明依赖
├── main.py           # 全部逻辑放这一个文件：启动 MCP、定义 agent、运行闭环
├── output/           # 共享工作目录，小说和 review 都在这
└── README.md         # 简短说明怎么跑
```

所有逻辑放 main.py 一个文件，不要拆模块。加日志输出能看到每轮谁在干什么。

## 运行方式

```bash
uv run main.py "写一篇关于时间旅行者的短篇科幻小说，800字左右"
```
