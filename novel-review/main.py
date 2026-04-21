"""三个 Codex CLI 协作写文章 + 双重检查。

真正的对话模式：
- 每个 codex 启动一次独立 session（拿到 threadId），保持会话连贯
- Python 只当信使：把 Writer 的回复分别转发给两位检查者
- 两位检查者都回复后，Python 才把两份反馈一起转给 Writer
- 文风和创意检查者都写出 APPROVED 就结束；否则最多 3 轮

三个 codex 共享工作目录 ./output，article.md / style_review.md / creative_review.md 在这里。
Python 自己的 transcript 与事件记录放 ./logs，不干扰 codex 的工作区。
"""

import ast
import asyncio
import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
import re

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MAX_ROUNDS = 3
TARGET_LENGTH = 300
PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"   # codex 工作目录
LOGS_DIR = PROJECT_DIR / "logs"       # 调度器日志（codex 看不到）
TRANSCRIPT = LOGS_DIR / "transcript.md"
EVENTS = LOGS_DIR / "events.jsonl"
ARTICLE = OUTPUT_DIR / "article.md"
STYLE_REVIEW = OUTPUT_DIR / "style_review.md"
CREATIVE_REVIEW = OUTPUT_DIR / "creative_review.md"
IGNORED_EVENT_TYPES = {"agent_message_content_delta", "agent_message_delta"}


# ---------- 捕获 codex/event 自定义通知 ----------

_EVENT_RE = re.compile(
    r"Failed to validate notification:.*?Message was: method='codex/event' params=(?P<body>\{.*\}) jsonrpc=",
    re.DOTALL,
)


class _CodexEventCapture(logging.Filter):
    """把 MCP SDK 对 codex 自定义通知的验证告警转成结构化事件日志，并压掉原始噪声。"""

    def __init__(self) -> None:
        super().__init__()
        self.current_speaker: str | None = None
        self.round_idx: int = 0

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Failed to validate notification" not in msg:
            return True
        m = _EVENT_RE.search(msg)
        if m:
            try:
                body = ast.literal_eval(m.group("body"))
            except (ValueError, SyntaxError):
                body = None
            if isinstance(body, dict):
                self._record(body)
        return False

    def _record(self, body: dict) -> None:
        event = body.get("msg") or {}
        etype = event.get("type", "unknown")
        if etype in IGNORED_EVENT_TYPES:
            return
        rec = {
            "ts": time.time(),
            "round": self.round_idx,
            "speaker": self.current_speaker,
            "type": etype,
            "event": event,
        }
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if etype in {"exec_command_begin", "patch_apply_begin", "mcp_tool_call_begin"}:
            hint = _event_hint(event)
            if hint:
                log.info("  [%s] · %s", self.current_speaker, hint)
        elif etype == "error":
            log.warning("  [%s] codex 错误: %s", self.current_speaker, event.get("message", ""))


def _event_hint(event: dict) -> str:
    t = event.get("type", "")
    if t == "exec_command_begin":
        cmd = event.get("command") or event.get("argv") or []
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return f"exec: {str(cmd)[:120]}"
    if t == "patch_apply_begin":
        changes = event.get("changes") or {}
        if isinstance(changes, dict) and changes:
            return f"patch: {', '.join(changes.keys())}"
        return "patch"
    if t == "mcp_tool_call_begin":
        inv = event.get("invocation") or {}
        return f"tool: {inv.get('tool', '?')}"
    return ""


capture = _CodexEventCapture()
logging.getLogger().addFilter(capture)


# ---------- Prompts ----------

WRITER_INIT = """你是文章作者 **Writer**，正在与两位检查者 **StyleChecker**（文风）和 **IdeaChecker**（创意）协作。你们共享当前工作目录。

主题：{topic}

请执行：
1. 直接创作一篇约 {length_hint} 的中文文章，以完整内容覆盖写入当前目录下的 `article.md`
2. 不要向我反问或请求确认，所有创作决策你自己定

重要：协作方式
- 我（调度器）是你和两位检查者之间的信使
- 你的这次回复将被我原样转发给两位检查者
- 在回复里用自然、简短的语气告诉两位检查者：你写了什么，并邀请他们分别从文风和创意角度审阅 `article.md`
- 后续我会把两位检查者的反馈一起原样转给你，你综合两份意见修改 `article.md`，再回复说明改了什么
"""

STYLE_INIT = """你是文风检查者 **StyleChecker**，正在与作者 **Writer** 和创意检查者 **IdeaChecker** 协作。你们共享当前工作目录。

Writer 刚刚完成了一版文章并发来了消息（见下方）。请执行：
1. 读取当前目录的 `article.md`
2. 只从文风角度审阅：语言是否准确自然、节奏是否顺畅、语气是否统一、措辞是否有表现力
3. 把评审意见覆盖写入同目录的 `style_review.md`
4. 做决定：
   - 如果文风整体达标，在你的回复末尾单独一行写 `APPROVED`，同时在 `style_review.md` 末尾也写 `APPROVED`
   - 否则，在回复里用自然语气告诉 Writer 该怎么改，建议要具体、可执行，不要出现 `APPROVED`

重要：协作方式
- 我（调度器）是你和其他人之间的信使
- 你的回复会先由我保存，等 IdeaChecker 也回复后，再一起转发给 Writer
- 不要展开评价创意优劣，创意由 IdeaChecker 负责；如果发现明显创意问题，只需简短提醒即可

---
Writer 刚才说：
{writer_msg}
"""

CREATIVE_INIT = """你是创意检查者 **IdeaChecker**，正在与作者 **Writer** 和文风检查者 **StyleChecker** 协作。你们共享当前工作目录。

Writer 刚刚完成了一版文章并发来了消息（见下方）。请执行：
1. 读取当前目录的 `article.md`
2. 只从创意角度审阅：立意是否鲜明、切入是否新鲜、意象或设定是否有记忆点、表达是否避免陈词滥调
3. 把评审意见覆盖写入同目录的 `creative_review.md`
4. 做决定：
   - 如果创意整体达标，在你的回复末尾单独一行写 `APPROVED`，同时在 `creative_review.md` 末尾也写 `APPROVED`
   - 否则，在回复里用自然语气告诉 Writer 应该怎样增强创意，建议要具体、可执行，不要出现 `APPROVED`

重要：协作方式
- 我（调度器）是你和其他人之间的信使
- 你的回复会先由我保存，等 StyleChecker 也回复后，再一起转发给 Writer
- 不要细抠具体措辞和语病，文风由 StyleChecker 负责；如果发现严重文风问题，只需简短提醒即可

---
Writer 刚才说：
{writer_msg}
"""

RELAY_TO_WRITER = """两位检查者刚才说：

【StyleChecker】
{style_msg}

【IdeaChecker】
{creative_msg}

---
请综合两份反馈修改 `article.md`（覆盖写入完整版），优先处理尚未 `APPROVED` 的部分，并尽量保留已被认可的优点。然后用简短的话回复告诉两位检查者你改了什么。你的回复会被我原样分别转给两位检查者。
"""

RELAY_TO_STYLE = """Writer 刚才说：

{writer_msg}

---
请重新读取 `article.md`，只从文风角度继续审阅并更新 `style_review.md`。如果这次达标，就在回复末尾单独一行写 `APPROVED`；否则继续给出具体修改建议。你的回复会先由我保存，待 IdeaChecker 也回复后，再一起转给 Writer。"""

RELAY_TO_CREATIVE = """Writer 刚才说：

{writer_msg}

---
请重新读取 `article.md`，只从创意角度继续审阅并更新 `creative_review.md`。如果这次达标，就在回复末尾单独一行写 `APPROVED`；否则继续给出具体修改建议。你的回复会先由我保存，待 StyleChecker 也回复后，再一起转给 Writer。"""

LAST_ROUND_NUDGE = (
    "\n\n（附：这已是最后一轮，请给出最终判断；若已达到可接受标准请写 `APPROVED`，"
    "否则明确指出最关键的剩余问题。）"
)


# ---------- Transcript ----------

def tx(text: str = "") -> None:
    with TRANSCRIPT.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def tx_bubble(speaker: str, kind: str, content: str, meta: str = "") -> None:
    header = f"### {speaker} → {kind}"
    if meta:
        header += f"  _{meta}_"
    tx("")
    tx(header)
    tx("")
    tx("```text")
    tx(content.strip())
    tx("```")


# ---------- Codex 调度 ----------

def _extract_result(res) -> tuple[str, str | None]:
    """返回 (文本回复, threadId)。"""
    thread_id = None
    if res.structuredContent:
        thread_id = res.structuredContent.get("threadId")
    parts = []
    for item in res.content or []:
        t = getattr(item, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts).strip(), thread_id


async def codex_start(
    session: ClientSession, prompt: str, cwd: str
) -> tuple[str, str]:
    """新开一个 codex 会话。返回 (回复, threadId)。"""
    res = await session.call_tool(
        "codex",
        arguments={
            "prompt": prompt,
            "cwd": cwd,
            "sandbox": "workspace-write",
            "approval-policy": "never",
        },
    )
    if res.isError:
        raise RuntimeError(f"codex start failed: {_extract_result(res)[0]}")
    text, tid = _extract_result(res)
    if not tid:
        raise RuntimeError("codex 未返回 threadId")
    return text, tid


async def codex_reply(session: ClientSession, thread_id: str, prompt: str) -> str:
    """在已有会话上继续对话，只返回文本。"""
    res = await session.call_tool(
        "codex-reply",
        arguments={"threadId": thread_id, "prompt": prompt},
    )
    if res.isError:
        raise RuntimeError(f"codex-reply failed: {_extract_result(res)[0]}")
    return _extract_result(res)[0]


async def start_mcp(stack: AsyncExitStack, name: str, cwd: str) -> ClientSession:
    log.info("[%s] 启动 codex mcp-server (cwd=%s)", name, cwd)
    params = StdioServerParameters(command="codex", args=["mcp-server"], cwd=cwd)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


# ---------- 主流程 ----------

def length_hint() -> str:
    return f"{TARGET_LENGTH} 字"


def is_approved(msg: str) -> bool:
    return "APPROVED" in msg.upper()


def review_status(style_msg: str, creative_msg: str) -> str:
    style_state = "APPROVED" if is_approved(style_msg) else "待修改"
    creative_state = "APPROVED" if is_approved(creative_msg) else "待修改"
    return f"文风={style_state}，创意={creative_state}"


async def main(topic: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    for f in (
        ARTICLE,
        STYLE_REVIEW,
        CREATIVE_REVIEW,
        OUTPUT_DIR / "novel.md",
        OUTPUT_DIR / "review.md",
        TRANSCRIPT,
        EVENTS,
    ):
        f.unlink(missing_ok=True)

    cwd = str(OUTPUT_DIR.resolve())
    tx("# 协作轨迹\n")
    tx(f"- 主题: {topic}")
    tx(f"- 工作目录: `{cwd}`")
    tx(f"- 目标字数: {length_hint()}")
    tx(f"- 最大轮数: {MAX_ROUNDS}")
    tx(f"- 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    log.info("主题: %s", topic)
    log.info("工作目录: %s", cwd)
    log.info("目标字数: %s", length_hint())
    log.info("最大迭代轮数: %d", MAX_ROUNDS)

    async with AsyncExitStack() as stack:
        writer_mcp = await start_mcp(stack, "Writer", cwd)
        style_mcp = await start_mcp(stack, "StyleChecker", cwd)
        creative_mcp = await start_mcp(stack, "IdeaChecker", cwd)

        # ---------- Round 1 ----------
        capture.round_idx = 1
        log.info("========== Round 1 ==========")
        tx("\n---\n\n## Round 1\n")

        # Writer 启动：写作 + 首次发言
        capture.current_speaker = "Writer"
        init_w = WRITER_INIT.format(topic=topic, length_hint=length_hint())
        tx_bubble("调度器", "Writer (首次任务)", init_w)
        log.info("[Writer] 启动会话，写第 1 版文章...")
        t0 = time.time()
        writer_msg, writer_tid = await codex_start(writer_mcp, init_w, cwd)
        log.info("[Writer] 完成 (%.1fs, threadId=%s)", time.time() - t0, writer_tid[:8])
        log.info("[Writer → Reviewers] %s", writer_msg[:200].replace("\n", " "))
        tx_bubble("Writer", "→ Reviewers", writer_msg, f"thread={writer_tid[:8]}")

        # 文风检查者启动：收到 Writer 消息后审阅
        capture.current_speaker = "StyleChecker"
        init_style = STYLE_INIT.format(writer_msg=writer_msg)
        if MAX_ROUNDS == 1:
            init_style += LAST_ROUND_NUDGE
        tx_bubble("调度器", "StyleChecker (转发 + 任务)", init_style)
        log.info("[StyleChecker] 启动会话，审第 1 版文风...")
        t0 = time.time()
        style_msg, style_tid = await codex_start(style_mcp, init_style, cwd)
        log.info("[StyleChecker] 完成 (%.1fs, threadId=%s)", time.time() - t0, style_tid[:8])
        log.info("[StyleChecker → Writer] %s", style_msg[:200].replace("\n", " "))
        tx_bubble("StyleChecker", "→ Writer", style_msg, f"thread={style_tid[:8]}")

        # 创意检查者启动：收到 Writer 消息后审阅
        capture.current_speaker = "IdeaChecker"
        init_creative = CREATIVE_INIT.format(writer_msg=writer_msg)
        if MAX_ROUNDS == 1:
            init_creative += LAST_ROUND_NUDGE
        tx_bubble("调度器", "IdeaChecker (转发 + 任务)", init_creative)
        log.info("[IdeaChecker] 启动会话，审第 1 版创意...")
        t0 = time.time()
        creative_msg, creative_tid = await codex_start(creative_mcp, init_creative, cwd)
        log.info("[IdeaChecker] 完成 (%.1fs, threadId=%s)", time.time() - t0, creative_tid[:8])
        log.info("[IdeaChecker → Writer] %s", creative_msg[:200].replace("\n", " "))
        tx_bubble("IdeaChecker", "→ Writer", creative_msg, f"thread={creative_tid[:8]}")

        approved = is_approved(style_msg) and is_approved(creative_msg)
        log.info("当前状态: %s", review_status(style_msg, creative_msg))
        if approved:
            log.info("========== 两位检查者均 APPROVED，对话结束 ==========")
            tx("\n> ✅ 两位检查者均 APPROVED，对话结束")

        # ---------- Round 2..N ----------
        for round_idx in range(2, MAX_ROUNDS + 1):
            if approved:
                break
            capture.round_idx = round_idx
            log.info("========== Round %d ==========", round_idx)
            tx(f"\n---\n\n## Round {round_idx}\n")

            # 等两位检查者都给完反馈后，再统一转给 Writer
            capture.current_speaker = "Writer"
            relay_w = RELAY_TO_WRITER.format(style_msg=style_msg, creative_msg=creative_msg)
            tx_bubble("调度器", "Writer (汇总两份反馈)", relay_w)
            log.info("[Writer] 综合两位检查者意见修改...")
            t0 = time.time()
            writer_msg = await codex_reply(writer_mcp, writer_tid, relay_w)
            log.info("[Writer] 完成 (%.1fs)", time.time() - t0)
            log.info("[Writer → Reviewers] %s", writer_msg[:200].replace("\n", " "))
            tx_bubble("Writer", "→ Reviewers", writer_msg)

            # 把 Writer 的话分别转给两位检查者，再等两份反馈都回来
            capture.current_speaker = "StyleChecker"
            relay_style = RELAY_TO_STYLE.format(writer_msg=writer_msg)
            if round_idx == MAX_ROUNDS:
                relay_style += LAST_ROUND_NUDGE
            tx_bubble("调度器", "StyleChecker (转发 Writer)", relay_style)
            log.info("[StyleChecker] 审第 %d 版文风...", round_idx)
            t0 = time.time()
            style_msg = await codex_reply(style_mcp, style_tid, relay_style)
            log.info("[StyleChecker] 完成 (%.1fs)", time.time() - t0)
            log.info("[StyleChecker → Writer] %s", style_msg[:200].replace("\n", " "))
            tx_bubble("StyleChecker", "→ Writer", style_msg)

            capture.current_speaker = "IdeaChecker"
            relay_creative = RELAY_TO_CREATIVE.format(writer_msg=writer_msg)
            if round_idx == MAX_ROUNDS:
                relay_creative += LAST_ROUND_NUDGE
            tx_bubble("调度器", "IdeaChecker (转发 Writer)", relay_creative)
            log.info("[IdeaChecker] 审第 %d 版创意...", round_idx)
            t0 = time.time()
            creative_msg = await codex_reply(creative_mcp, creative_tid, relay_creative)
            log.info("[IdeaChecker] 完成 (%.1fs)", time.time() - t0)
            log.info("[IdeaChecker → Writer] %s", creative_msg[:200].replace("\n", " "))
            tx_bubble("IdeaChecker", "→ Writer", creative_msg)

            approved = is_approved(style_msg) and is_approved(creative_msg)
            log.info("当前状态: %s", review_status(style_msg, creative_msg))
            if approved:
                log.info("========== 两位检查者均 APPROVED，对话结束 ==========")
                tx("\n> ✅ 两位检查者均 APPROVED，对话结束")
                break
            log.info("[Reviewers] 仍有待修改项，进入下一轮")

        if not approved:
            log.info("========== 达到最大轮数，对话结束 ==========")
            tx("\n> ⏹️ 达到最大轮数，对话结束")

    # ---------- Summary ----------
    tx(f"\n---\n- 结束: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if ARTICLE.exists():
        log.info("最终文章: %s (%d 字符)", ARTICLE, len(ARTICLE.read_text(encoding='utf-8')))
    if STYLE_REVIEW.exists():
        log.info("文风审评: %s (%d 字符)", STYLE_REVIEW, len(STYLE_REVIEW.read_text(encoding='utf-8')))
    if CREATIVE_REVIEW.exists():
        log.info(
            "创意审评: %s (%d 字符)",
            CREATIVE_REVIEW,
            len(CREATIVE_REVIEW.read_text(encoding="utf-8")),
        )
    log.info("对话轨迹: %s", TRANSCRIPT)
    log.info("事件流:   %s", EVENTS)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('用法: uv run main.py "文章主题"')
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
