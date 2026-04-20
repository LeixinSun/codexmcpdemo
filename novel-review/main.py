"""两个 Codex CLI 互相对话写小说 + 审稿。

真正的对话模式：
- 每个 codex 启动一次独立 session（拿到 threadId），保持会话连贯
- Python 只当信使：把 Writer 的回复原样转发给 Reviewer，反之亦然
- Reviewer 回复里含 APPROVED 就结束；否则最多 3 轮

两个 codex 共享工作目录 ./output，novel.md / review.md 在这里。
Python 自己的 transcript 与事件记录放 ./logs，不干扰 codex 的工作区。
"""

import ast
import asyncio
import json
import logging
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MAX_ROUNDS = 3
PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"   # codex 工作目录
LOGS_DIR = PROJECT_DIR / "logs"       # 调度器日志（codex 看不到）
TRANSCRIPT = LOGS_DIR / "transcript.md"
EVENTS = LOGS_DIR / "events.jsonl"


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

WRITER_INIT = """你是小说作家 **Writer**，正在与一位编辑 **Reviewer** 协作创作。你们共享当前工作目录。

主题：{topic}

请执行：
1. 直接创作一篇短篇小说（约 {length_hint}），以完整内容覆盖写入当前目录下的 `novel.md`
2. 不要向我反问或请求确认，所有创作决策你自己定

重要：协作方式
- 我（调度器）是你和 Reviewer 之间的信使
- 你的这次回复将被我**原样转发给 Reviewer**
- 在回复里用自然、简短的语气告诉 Reviewer：你写的是什么、邀请他审阅 novel.md
- 后续我会把 Reviewer 的回复也原样转给你，你按他的意见修改 novel.md 并再次回复
"""

REVIEWER_INIT = """你是资深文学编辑 **Reviewer**，正在与作家 **Writer** 协作。你们共享当前工作目录。

Writer 刚刚完成第一稿并发来了消息（见下方）。请执行：
1. 读取当前目录的 `novel.md`
2. 从文笔风格、情节结构、人物塑造、主题表达、细节描写五个维度审稿，把评审意见覆盖写入同目录的 `review.md`
3. 做决定：
   - 如果小说整体达标，在你的回复末尾单独一行写 `APPROVED`（大写），同时在 review.md 末尾也写 APPROVED，结束对话
   - 否则，在回复里用自然语气告诉 Writer 需要改什么（具体到段落/方向），不要出现 APPROVED

重要：协作方式
- 我（调度器）是你和 Writer 之间的信使
- 你的回复将被我**原样转发给 Writer**
- 不要过于苛刻，整体质量不错就直接 APPROVED

---
Writer 刚才说：
{writer_msg}
"""

RELAY_TO_WRITER = """Reviewer 刚才说：

{reviewer_msg}

---
请按上述意见修改 `novel.md`（覆盖写入完整版），然后用简短的话回复告诉 Reviewer 你改了什么。你的回复会被原样转给 Reviewer。"""

RELAY_TO_REVIEWER = """Writer 刚才说：

{writer_msg}

---
请重新读取 `novel.md`，继续审阅并更新 `review.md`。若这次达标就在回复末尾单独一行写 `APPROVED`；否则继续给出修改建议。你的回复会被原样转给 Writer。"""

LAST_ROUND_NUDGE = "\n\n（附：这已是最后一轮，请在本轮做出最终决定并写 APPROVED。）"


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

def length_hint(topic: str) -> str:
    # 从主题里捕一下"xxx 字"作为长度提示，缺省给个范围
    m = re.search(r"(\d{2,5})\s*字", topic)
    return f"{m.group(1)} 字" if m else "800 字"


async def main(topic: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    for f in (OUTPUT_DIR / "novel.md", OUTPUT_DIR / "review.md", TRANSCRIPT, EVENTS):
        f.unlink(missing_ok=True)

    cwd = str(OUTPUT_DIR.resolve())
    tx(f"# 协作轨迹\n")
    tx(f"- 主题: {topic}")
    tx(f"- 工作目录: `{cwd}`")
    tx(f"- 最大轮数: {MAX_ROUNDS}")
    tx(f"- 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    log.info("主题: %s", topic)
    log.info("工作目录: %s", cwd)
    log.info("最大迭代轮数: %d", MAX_ROUNDS)

    async with AsyncExitStack() as stack:
        writer_mcp = await start_mcp(stack, "Writer", cwd)
        reviewer_mcp = await start_mcp(stack, "Reviewer", cwd)

        # ---------- Round 1 ----------
        capture.round_idx = 1
        log.info("========== Round 1 ==========")
        tx("\n---\n\n## Round 1\n")

        # Writer 启动：创作 + 首次发言
        capture.current_speaker = "Writer"
        init_w = WRITER_INIT.format(topic=topic, length_hint=length_hint(topic))
        tx_bubble("调度器", "Writer (首次任务)", init_w)
        log.info("[Writer] 启动会话，创作第 1 稿...")
        t0 = time.time()
        writer_msg, writer_tid = await codex_start(writer_mcp, init_w, cwd)
        log.info("[Writer] 完成 (%.1fs, threadId=%s)", time.time() - t0, writer_tid[:8])
        log.info("[Writer → Reviewer] %s", writer_msg[:200].replace("\n", " "))
        tx_bubble("Writer", "→ Reviewer", writer_msg, f"thread={writer_tid[:8]}")

        # Reviewer 启动：收到 Writer 消息后审稿
        capture.current_speaker = "Reviewer"
        init_r = REVIEWER_INIT.format(writer_msg=writer_msg)
        if MAX_ROUNDS == 1:
            init_r += LAST_ROUND_NUDGE
        tx_bubble("调度器", "Reviewer (转发 + 任务)", init_r)
        log.info("[Reviewer] 启动会话，审第 1 稿...")
        t0 = time.time()
        reviewer_msg, reviewer_tid = await codex_start(reviewer_mcp, init_r, cwd)
        log.info("[Reviewer] 完成 (%.1fs, threadId=%s)", time.time() - t0, reviewer_tid[:8])
        log.info("[Reviewer → Writer] %s", reviewer_msg[:200].replace("\n", " "))
        tx_bubble("Reviewer", "→ Writer", reviewer_msg, f"thread={reviewer_tid[:8]}")

        approved = "APPROVED" in reviewer_msg.upper()
        if approved:
            log.info("========== Reviewer APPROVED，对话结束 ==========")
            tx("\n> ✅ Reviewer APPROVED，对话结束")

        # ---------- Round 2..N ----------
        for round_idx in range(2, MAX_ROUNDS + 1):
            if approved:
                break
            capture.round_idx = round_idx
            log.info("========== Round %d ==========", round_idx)
            tx(f"\n---\n\n## Round {round_idx}\n")

            # 把 Reviewer 的话转给 Writer
            capture.current_speaker = "Writer"
            relay_w = RELAY_TO_WRITER.format(reviewer_msg=reviewer_msg)
            tx_bubble("调度器", "Writer (转发 Reviewer)", relay_w)
            log.info("[Writer] 根据 Reviewer 意见修改...")
            t0 = time.time()
            writer_msg = await codex_reply(writer_mcp, writer_tid, relay_w)
            log.info("[Writer] 完成 (%.1fs)", time.time() - t0)
            log.info("[Writer → Reviewer] %s", writer_msg[:200].replace("\n", " "))
            tx_bubble("Writer", "→ Reviewer", writer_msg)

            # 把 Writer 的话转给 Reviewer
            capture.current_speaker = "Reviewer"
            relay_r = RELAY_TO_REVIEWER.format(writer_msg=writer_msg)
            if round_idx == MAX_ROUNDS:
                relay_r += LAST_ROUND_NUDGE
            tx_bubble("调度器", "Reviewer (转发 Writer)", relay_r)
            log.info("[Reviewer] 审第 %d 稿...", round_idx)
            t0 = time.time()
            reviewer_msg = await codex_reply(reviewer_mcp, reviewer_tid, relay_r)
            log.info("[Reviewer] 完成 (%.1fs)", time.time() - t0)
            log.info("[Reviewer → Writer] %s", reviewer_msg[:200].replace("\n", " "))
            tx_bubble("Reviewer", "→ Writer", reviewer_msg)

            if "APPROVED" in reviewer_msg.upper():
                approved = True
                log.info("========== Reviewer APPROVED，对话结束 ==========")
                tx("\n> ✅ Reviewer APPROVED，对话结束")
                break
            else:
                log.info("[Reviewer] 未通过，进入下一轮")

        if not approved:
            log.info("========== 达到最大轮数，对话结束 ==========")
            tx("\n> ⏹️ 达到最大轮数，对话结束")

    # ---------- Summary ----------
    tx(f"\n---\n- 结束: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    novel = OUTPUT_DIR / "novel.md"
    review = OUTPUT_DIR / "review.md"
    if novel.exists():
        log.info("最终小说: %s (%d 字符)", novel, len(novel.read_text()))
    if review.exists():
        log.info("最终审评: %s (%d 字符)", review, len(review.read_text()))
    log.info("对话轨迹: %s", TRANSCRIPT)
    log.info("事件流:   %s", EVENTS)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('用法: uv run main.py "小说主题"')
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
