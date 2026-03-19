import json
import os
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core import logger
from astrbot.core.message.components import File as FileComponent
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# Optional: set ASTRBOT_AUDIT_BASE_URL to expose a download link instead of uploading the file.
AUDIT_BASE_URL = os.getenv("ASTRBOT_AUDIT_BASE_URL", "").rstrip("/")


class Main(star.Star):
    """审计日志插件：记录 LLM 实际收到的上下文和回复，并通过文件或链接发送给管理员。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        base = Path(get_astrbot_data_path()) / "audit_logs"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("创建审计日志目录失败: %s", exc)
        self.base_dir = base

    def _get_log_path(self, date: str | None = None) -> str:
        """根据日期返回对应的审计日志文件路径，默认使用当天（UTC）。"""
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return str(self.base_dir / f"{date}.jsonl")

    def _load_audit_records(
        self, date: str | None = None, limit: int = 10
    ) -> list[dict]:
        """加载指定日期的部分审计记录（按文件顺序，最多 limit 条）。"""
        path = self._get_log_path(date)
        records: list[dict] = []

        if not os.path.exists(path):
            return records

        try:
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        records.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.error("读取审计日志失败: %s", exc)

        return records

    async def audit_search(
        self, event: AstrMessageEvent, session_id: str = "", limit: str = "5"
    ) -> None:
        """审计查询：发送审计日志文件，或在配置了 AUDIT_BASE_URL 时返回下载链接。"""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not session_id:
            path = self._get_log_path()
            if not os.path.exists(path):
                event.set_result(MessageEventResult().message("今天还没有审计日志。"))
                return

            file_name = f"audit-{day}.jsonl"
            if AUDIT_BASE_URL:
                url = f"{AUDIT_BASE_URL}/{day}.jsonl"
                event.set_result(
                    MessageEventResult().message(f"当天审计日志下载链接：{url}")
                )
            else:
                result = MessageEventResult().message(f"当天审计日志文件：{file_name}")
                result.chain.append(FileComponent(name=file_name, file=path))
                event.set_result(result)
            return

        max_count = int(limit) if limit.isdigit() else 5
        records = self._load_audit_records(limit=10000)
        session_records = [
            r for r in records if r.get("session_id", "").startswith(session_id)
        ]

        if not session_records:
            event.set_result(
                MessageEventResult().message(f"没有找到会话 {session_id} 的审计记录。")
            )
            return

        safe_sid = "".join(c if c.isalnum() else "_" for c in session_id)[:32]
        sub_name = f"audit-{day}-session-{safe_sid}.jsonl"
        sub_path = str(self.base_dir / sub_name)

        try:
            with open(sub_path, "w", encoding="utf-8") as f:
                for rec in session_records[:max_count]:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.error("写入会话审计子文件失败: %s", exc)
            event.set_result(MessageEventResult().message("生成会话审计文件失败。"))
            return

        if AUDIT_BASE_URL:
            url = f"{AUDIT_BASE_URL}/{sub_name}"
            event.set_result(
                MessageEventResult().message(
                    f"会话 {session_id} 的审计日志下载链接：{url}"
                )
            )
        else:
            result = MessageEventResult().message(
                f"会话 {session_id} 的审计日志文件：{sub_name}"
            )
            result.chain.append(FileComponent(name=sub_name, file=sub_path))
            event.set_result(result)

    async def audit_stats(self, event: AstrMessageEvent, date: str = "") -> None:
        """审计统计：返回简短文本统计信息。"""
        path = self._get_log_path(date or None)
        if not os.path.exists(path):
            msg = (
                f"没有找到日期 {date} 的审计日志。" if date else "今天还没有审计日志。"
            )
            event.set_result(MessageEventResult().message(msg))
            return

        total_records = 0
        unique_sessions: set[str] = set()
        unique_users: set[str] = set()
        total_tokens = 0
        total_mem0_injections = 0
        models_used: set[str] = set()

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    total_records += 1
                    unique_sessions.add(record.get("session_id", ""))
                    unique_users.add(record.get("user_id", ""))
                    if record.get("token_usage"):
                        total_tokens += record["token_usage"]
                    total_mem0_injections += len(record.get("mem0_injected", []))
                    if record.get("model"):
                        models_used.add(record["model"])
        except Exception as exc:  # noqa: BLE001
            logger.error("读取审计统计失败: %s", exc)
            event.set_result(MessageEventResult().message("读取审计统计失败。"))
            return

        day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        msg_lines = [
            f"审计统计 - {day}",
            f"总记录数: {total_records}",
            f"活跃会话数: {len(unique_sessions)}",
            f"活跃用户数: {len(unique_users)}",
            f"Token总数: {total_tokens:,}",
            f"Mem0注入次数: {total_mem0_injections}",
            f"使用模型数: {len(models_used)}",
        ]
        if models_used:
            msg_lines.append("使用模型: " + ", ".join(sorted(models_used)))

        event.set_result(MessageEventResult().message("\n".join(msg_lines)))

    def _build_context_snapshot(self, req: ProviderRequest) -> list[dict]:
        """尽量还原 LLM 实际收到的 messages 列表。"""
        messages: list[dict] = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})

        ctx = req.contexts or []
        if isinstance(ctx, list):
            for m in ctx:
                if isinstance(m, dict):
                    messages.append(m)

        return messages

    async def _append_current_user_message(
        self,
        req: ProviderRequest,
        messages: list[dict],
    ) -> None:
        """把当前用户消息追加到 context_snapshot 末尾。"""
        try:
            user_msg = await req.assemble_context()
        except Exception as exc:  # noqa: BLE001
            logger.warning("组装用户消息失败，将使用简化版本: %s", exc)
            if req.prompt:
                user_msg = {"role": "user", "content": req.prompt}
            else:
                return
        messages.append(user_msg)

    def _safe_dumps(self, payload: dict) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("审计日志序列化失败: %s", exc)
            return "{}"

    @filter.on_llm_response()
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """在每轮 LLM 响应后写入一条审计记录。"""
        req: ProviderRequest | None = event.get_extra("provider_request")
        if not req:
            return

        context_snapshot = self._build_context_snapshot(req)
        await self._append_current_user_message(req, context_snapshot)

        mem0_injected = event.get_extra("mem0_injected") or []
        window = event.get_extra("short_term_window")
        pending_count = getattr(window, "pending_count", 0) if window else 0

        model = None
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider:
                model = provider.get_model()
        except Exception:  # noqa: BLE001
            model = None

        token_usage = response.usage.total if response.usage else None

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": event.unified_msg_origin,
            "user_id": event.get_sender_id(),
            "user_message": req.prompt,
            "assistant_reply": response.completion_text,
            "mem0_injected": mem0_injected,
            "context_snapshot": context_snapshot,
            "pending_count": pending_count,
            "model": model,
            "token_usage": token_usage,
        }

        line = self._safe_dumps(record)
        path = self._get_log_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("写入审计日志失败(%s): %s", path, exc)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("audit")
    async def audit_command(self, event: AstrMessageEvent, args: str = "") -> None:
        """审计命令：/audit search [session_id] [limit] 或 /audit stats [date]"""
        parts = args.strip().split() if args else []

        if not parts:
            help_msg = (
                "审计命令用法：\n"
                "/audit search [session_id] [limit] - 查询审计记录（发送 jsonl 文件）\n"
                "/audit stats [date] - 查看审计统计（文本）\n"
                "\n"
                "示例：\n"
                "/audit search (发送当天完整审计 jsonl 文件)\n"
                "/audit search session123 10 (发送只包含该会话记录的 jsonl 文件)\n"
                "/audit stats (今天的统计)\n"
                "/audit stats 2024-01-01 (指定日期统计)"
            )
            event.set_result(MessageEventResult().message(help_msg))
            return

        command = parts[0]

        try:
            if command == "search":
                session_id = parts[1] if len(parts) > 1 else ""
                limit = parts[2] if len(parts) > 2 else "5"
                await self.audit_search(event, session_id, limit)
            elif command == "stats":
                date = parts[1] if len(parts) > 1 else ""
                await self.audit_stats(event, date)
            else:
                event.set_result(
                    MessageEventResult().message(
                        "未知命令。可用命令：\n"
                        "/audit search [session_id] [limit] - 查询审计记录（发送 jsonl 文件）\n"
                        "/audit stats [date] - 查看审计统计（文本）",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("处理审计命令失败: %s", exc)
            event.set_result(MessageEventResult().message(f"处理命令时出错：{exc}"))
