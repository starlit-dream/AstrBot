"""Mem0 桥接：search/add 封装、错误处理。"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from astrbot.core import logger

from .models import ShortTermEntry


_MEM0_INSTALL_LOCK = asyncio.Lock()
_MEM0_INSTALL_ATTEMPTED = False


async def ensure_mem0_installed() -> bool:
    """确保 mem0ai 已安装并可导入。

    - 未安装：打印 warning 并自动尝试安装 mem0ai
    - 安装失败：打印 error
    """
    global _MEM0_INSTALL_ATTEMPTED
    try:
        from mem0 import Memory  # noqa: F401

        return True
    except ImportError:
        logger.warning(
            "未安装 mem0ai，Mem0 长期记忆功能不可用。将自动尝试安装：pip install mem0ai"
        )

    async with _MEM0_INSTALL_LOCK:
        # 双重检查：等待锁期间可能已由其他协程完成安装
        try:
            from mem0 import Memory  # noqa: F401

            return True
        except ImportError:
            pass

        if _MEM0_INSTALL_ATTEMPTED:
            # 已尝试安装过，避免无限重复；直接返回 False
            return False

        _MEM0_INSTALL_ATTEMPTED = True
        try:
            from astrbot.core import pip_installer

            await pip_installer.install(package_name="mem0ai>=0.1.0")
        except Exception as exc:
            logger.error("自动安装 mem0ai 失败：%s", exc)
            return False

        try:
            importlib.invalidate_caches()
            from mem0 import Memory  # noqa: F401

            logger.info("mem0ai 安装成功，Mem0 长期记忆功能已恢复可用。")
            return True
        except Exception as exc:
            logger.error("mem0ai 安装后仍无法导入 Mem0：%s", exc)
            return False


def _get_client() -> Any | None:
    try:
        from mem0 import Memory

        return Memory
    except ImportError:
        logger.warning("未安装 mem0ai，Mem0 长期记忆功能不可用。")
        return None


def _create_memory(config: dict | None = None) -> Any | None:
    cls = _get_client()
    if cls is None:
        return None
    try:
        if config:
            # 从 AstrBot 的 mem0_config 中提取 LLM / embedder 配置，尽量自动补全 Mem0 需要的参数
            import os

            # 1) 处理 LLM 配置
            llm = (config or {}).get("llm") or {}
            llm_conf = llm.get("config") or {}

            api_key = llm_conf.get("api_key") or llm_conf.get("apiKey")
            api_base = llm_conf.get("api_base") or llm_conf.get("openai_base_url")

            # 不主动覆盖用户手动设置的环境变量，只在缺失时补上
            if api_key and not os.getenv("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = str(api_key)
            if api_base:
                if not os.getenv("OPENAI_BASE_URL"):
                    os.environ["OPENAI_BASE_URL"] = str(api_base)
                # 避免把未知参数 api_base 直接传给 Mem0 的 OpenAIConfig
                if "api_base" in llm_conf:
                    llm_conf.pop("api_base", None)
                # 兼容 Mem0 对 openai_base_url 的期望字段名
                llm_conf.setdefault("openai_base_url", str(api_base))
                llm["config"] = llm_conf
                config["llm"] = llm

            # 2) 处理 embedder 配置（结构与 llm 相同，但走 BaseEmbedderConfig）
            embedder = (config or {}).get("embedder") or {}
            embed_conf = embedder.get("config") or {}
            embed_api_base = embed_conf.get("api_base") or embed_conf.get("openai_base_url")

            if embed_api_base:
                # 同样避免把 api_base 这种 Mem0 不认识的字段传进去
                if "api_base" in embed_conf:
                    embed_conf.pop("api_base", None)
                embed_conf.setdefault("openai_base_url", str(embed_api_base))
                embedder["config"] = embed_conf
                config["embedder"] = embedder

            return cls.from_config(config)
        return cls()
    except Exception as exc:
        logger.error("Mem0 初始化失败: %s", exc)
        return None


# 全局实例，按需初始化
_mem0_instance: Any | None = None
_mem0_config: dict | None = None


def init_mem0(config: dict | None = None) -> bool:
    """初始化 Mem0 实例。"""
    global _mem0_instance, _mem0_config
    _mem0_config = config
    # 避免在同一进程内重复初始化底层存储（如本地 Qdrant），
    # 导致 Windows 上出现 [WinError 32] 文件被占用。
    if _mem0_instance is None and config:
        _mem0_instance = _create_memory(config)
    return _mem0_instance is not None


def get_mem0() -> Any | None:
    """获取已初始化的 Mem0 实例（仅在有配置时才创建）。"""
    global _mem0_instance, _mem0_config
    if _mem0_config is None:
        # 没有任何配置，视为未启用 Mem0，避免走默认 Memory() → 环境变量
        return None
    if _mem0_instance is None:
        _mem0_instance = _create_memory(_mem0_config)
    return _mem0_instance


async def search_long_term(session_id: str, query: str, limit: int = 5) -> list[str]:
    """
    按语义检索长期记忆，返回记忆文本列表。
    失败时返回空列表，不抛错。
    """
    import asyncio

    m = get_mem0()
    if not m:
        return []

    def _search() -> list[dict]:
        # 兼容不同版本 Mem0：新版本支持 top_k，老版本只支持 limit
        try:
            result = m.search(query, user_id=session_id, top_k=limit)
        except TypeError:
            result = m.search(query, user_id=session_id, limit=limit)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        if isinstance(result, dict) and "memories" in result:
            return result["memories"]
        return []

    try:
        raw = await asyncio.to_thread(_search)
        texts: list[str] = []
        for r in raw:
            if isinstance(r, dict):
                t = r.get("memory") or r.get("text") or r.get("content", "")
                if t:
                    texts.append(str(t))
            elif isinstance(r, str):
                texts.append(r)
        return texts
    except Exception as exc:
        logger.warning("Mem0 search 失败 (session=%s): %s", session_id, exc)
        return []


async def add_from_pending(session_id: str, pending: list[ShortTermEntry]) -> bool:
    """
    将 pending 对话提交 Mem0.add。
    成功返回 True，失败返回 False（pending 保留在短期窗口中，下次重试）。
    """
    import asyncio

    m = get_mem0()
    if not pending:
        return True
    if not m:
        logger.warning(
            "Mem0 未初始化或不可用，跳过写入（session=%s）。pending 将保留下次重试。",
            session_id,
        )
        return False

    def _add() -> None:
        for e in pending:
            msgs = [
                {"role": "user", "content": e.user},
                {"role": "assistant", "content": e.assistant},
            ]
            m.add(msgs, user_id=session_id)

    try:
        await asyncio.to_thread(_add)
        return True
    except Exception as exc:
        logger.warning("Mem0 add 失败 (session=%s): %s，pending 将保留下次重试", session_id, exc)
        return False


def format_mem0_for_system(memories: list[str]) -> str:
    """将检索到的长期记忆格式化为注入 system_prompt 的文本。"""
    if not memories:
        return ""
    lines = ["[长期记忆]", *[f"- {m}" for m in memories]]
    return "\n".join(lines)
