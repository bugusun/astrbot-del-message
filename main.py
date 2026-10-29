"""在聊天里删除最近一轮对话（不发给 LLM，也不写进上下文）。

原理：
- 该指令由本插件直接处理，并调用 ``event.should_call_llm(False)``，
  这样 AstrBot 不会为这条消息发起 LLM 请求，历史保存逻辑
  （只在 LLM 流程里执行）也就不会把这条指令写进上下文。
- 插件直接改写当前会话的 conversation.history，把上一轮整段删掉。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from astrbot.api import logger, sp, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter

try:  # AstrBot 用它给「同一个会话的 LLM 请求」串行化
    from astrbot.core.utils.session_lock import session_lock_manager
except Exception:  # pragma: no cover - 兼容旧版本
    session_lock_manager = None

LOCK_TIMEOUT = 3.0
"""等待会话空闲的最长时间（秒）。"""

CHECKPOINT_ROLE = "_checkpoint"
"""AstrBot 内部检查点消息的角色，不属于真实对话内容。"""


def _content_text(content: Any) -> str:
    """把消息 content 压成一小段可读文本，只用于回执预览。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            for key in ("text", "think", "content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
    return "\n".join(parts)


def _is_checkpoint(message: Any) -> bool:
    return isinstance(message, Mapping) and message.get("role") == CHECKPOINT_ROLE


def _is_user(message: Any) -> bool:
    return isinstance(message, Mapping) and message.get("role") == "user"


def _is_assistant(message: Any) -> bool:
    return isinstance(message, Mapping) and message.get("role") == "assistant"


def _is_command_turn(message: Any, prefixes: list[str]) -> bool:
    """判断这条 user 消息是否本身就是一条唤醒前缀指令。"""
    if not _is_user(message):
        return False
    text = _content_text(message.get("content")).lstrip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip()
    for prefix in prefixes:
        if not prefix:
            continue
        if first_line.startswith(prefix):
            return True
        # 唤醒前缀可能是 "/"，而 AstrBot 在剥掉前缀后再匹配指令名
        for cmd in ("del", "undo"):
            if first_line == f"{prefix}{cmd}" or first_line.startswith(f"{prefix}{cmd} "):
                return True
    return False


def select_delete_start(
    history: list[Any],
    wake_prefixes: list[str],
    *,
    whole_turn: bool = True,
) -> int | None:
    """返回要删除区间的起始下标；返回 None 表示没有可删内容。

    规则（``whole_turn=True``）：
    - 找到最后一条 ``_checkpoint`` 之外的 assistant 消息，连同它前面那一整段
      （用户提问 + 工具调用 + 工具结果）一起删除；
    - 如果它前面紧邻的消息本身就是一条 ``/del``、``/undo`` 之类的指令，
      说明上一轮其实没被回复，此时再多删一整轮，避免留下没有回复的孤句。
    """
    if not isinstance(history, list):
        return None

    indexes = [i for i, item in enumerate(history) if not _is_checkpoint(item)]
    if not indexes:
        return None

    last_assistant = next((i for i in reversed(indexes) if _is_assistant(history[i])), None)
    last_user = next((i for i in reversed(indexes) if _is_user(history[i])), None)

    # 尾部存在「还没被回复」的消息（上一次 LLM 报错、被中断，或上一条就是本指令），
    # 它不属于任何已完成的一轮，单独删掉即可，绝不连带删除上一轮。
    if last_user is not None and (last_assistant is None or last_user > last_assistant):
        return last_user

    if last_assistant is None:
        return last_user

    if not whole_turn:
        inner_user = next(
            (i for i in reversed(indexes) if i <= last_assistant and _is_user(history[i])),
            None,
        )
        return inner_user if inner_user is not None else last_assistant

    start = last_assistant
    # 往回吞掉属于这一轮的内容（工具调用 / 工具结果 / 检查点 / user）
    probe = last_assistant - 1
    while probe >= 0:
        item = history[probe]
        if _is_checkpoint(item):
            probe -= 1
            continue
        start = probe
        if _is_user(item):
            break
        probe -= 1

    # 上一轮如果是一条指令（例如 /del），它本身没有被回复，再多删一整轮
    if start - 1 >= 0 and _is_command_turn(history[start], wake_prefixes):
        extra = select_delete_start(history[:start], wake_prefixes, whole_turn=True)
        if extra is not None:
            start = extra

    # 注意：切点之前如果紧邻检查点消息，必须保留它。
    # 检查点绑定在它前面的那条消息上（load 时会挂到 messages[-1]），
    # 保留它不会产生悬空引用；反过来删掉它才会丢失该轮的检查点信息。
    return start


class TurnEraserPlugin(star.Star):
    """删除最近一轮对话的指令插件。"""

    def __init__(self, context: star.Context, config: Mapping | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._undo_stack: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ 工具
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text))
        # 关键：禁止框架默认的 LLM 请求，这条指令就不会进入上下文
        event.should_call_llm(False)

    async def _wake_prefixes(self, event: AstrMessageEvent) -> list[str]:
        prefixes: list[str] = []
        try:
            conf = self.context.get_config(umo=event.unified_msg_origin)
            raw = conf.get("wake_prefix", "/")
            if isinstance(raw, str):
                prefixes = [raw]
            elif isinstance(raw, (list, tuple)):
                prefixes = [str(item) for item in raw]
        except Exception as exc:
            logger.debug(f"[turn_eraser] 读取 wake_prefix 失败: {exc}")
        if not prefixes:
            prefixes = ["/"]
        return [p for p in prefixes if p]

    async def _load_history(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, list[Any] | None]:
        conv_id = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        if not conv_id:
            return None, None
        conv = await self.context.conversation_manager.get_conversation(
            event.unified_msg_origin, conv_id
        )
        if not conv or not conv.history:
            return conv_id, []
        history = conv.history
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            history = []
        return conv_id, history

    async def _idle_or_busy(self, event: AstrMessageEvent) -> bool:
        """返回 True 表示该会话已经空闲（没有 LLM 正在回复）。

        AstrBot 在发起 LLM 请求时会锁住 ``unified_msg_origin``，请求结束后才
        把历史写回会话。这里等同一把锁，避免出现「删完又被正在生成的回复覆盖」。
        """
        if session_lock_manager is None:
            return True
        try:
            async with asyncio.timeout(LOCK_TIMEOUT):
                async with session_lock_manager.acquire_lock(
                    event.unified_msg_origin
                ):
                    return True
        except TimeoutError:
            return False
        except Exception as exc:  # 拿不到锁管理器时不应阻断指令
            logger.debug(f"[turn_eraser] 获取会话锁失败（忽略）: {exc}")
            return True

    # ------------------------------------------------------------------ 指令
    @filter.command("del", alias={"undo", "撤销"})
    async def delete_last_turn(self, event: AstrMessageEvent) -> None:
        """删除最近一轮对话（不写入聊天上下文）"""
        if bool(self._cfg("admin_only", False)) and not event.is_admin():
            self._reply(event, "这个指令只允许管理员使用。")
            return

        if not await self._idle_or_busy(event):
            self._reply(event, "机器人还在回复上一条消息，等它说完再删吧。")
            return

        try:
            conv_id, history = await self._load_history(event)
        except Exception as exc:
            logger.error(f"[turn_eraser] 读取会话失败: {exc}", exc_info=True)
            self._reply(event, f"读取当前会话失败：{exc}")
            return

        if not conv_id:
            self._reply(event, "当前还没有任何对话记录，没东西可删。")
            return
        if not history:
            self._reply(event, "当前对话是空的，没东西可删。")
            return

        prefixes = await self._wake_prefixes(event)
        start = select_delete_start(
            history,
            prefixes,
            whole_turn=bool(self._cfg("whole_turn", True)),
        )
        if start is None:
            self._reply(event, "没有找到可以删除的一轮对话。")
            return

        removed = history[start:]
        if not removed:
            self._reply(event, "没有找到可以删除的一轮对话。")
            return

        truncated = history[:start]
        try:
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                conv_id,
                history=truncated,
            )
        except Exception as exc:
            logger.error(f"[turn_eraser] 写入会话失败: {exc}", exc_info=True)
            self._reply(event, f"删除失败：{exc}")
            return

        if bool(self._cfg("allow_undo", True)):
            self._undo_stack[event.unified_msg_origin] = removed
            if len(self._undo_stack) > 200:
                self._undo_stack.pop(next(iter(self._undo_stack)))

        logger.info(
            f"[turn_eraser] {event.unified_msg_origin} 删除 {len(removed)} 条消息，"
            f"剩余 {len(truncated)} 条"
        )

        preview = ""
        if bool(self._cfg("show_preview", True)):
            for item in removed:
                if _is_user(item):
                    preview = _content_text(item.get("content")).strip()
                    break
            preview = " ".join(preview.split())
            preview = preview[: int(self._cfg("preview_length", 40) or 40)]

        if preview:
            self._reply(
                event,
                f"已删除最近一轮对话：{preview}\n"
                f"（剩余 {len(truncated)} 条上下文，这条指令不会进入对话）",
            )
        else:
            self._reply(
                event,
                f"已删除最近一轮对话。\n"
                f"（剩余 {len(truncated)} 条上下文，这条指令不会进入对话）",
            )

    @filter.command("undel", alias={"恢复对话"})
    async def undo_delete(self, event: AstrMessageEvent) -> None:
        """恢复刚刚被 /del 删除的一轮对话"""
        if bool(self._cfg("admin_only", False)) and not event.is_admin():
            self._reply(event, "这个指令只允许管理员使用。")
            return
        if not bool(self._cfg("allow_undo", True)):
            self._reply(event, "恢复功能已被关闭。")
            return

        if not await self._idle_or_busy(event):
            self._reply(event, "机器人还在回复上一条消息，等它说完再操作吧。")
            return

        removed = self._undo_stack.pop(event.unified_msg_origin, None)
        if not removed:
            self._reply(event, "没有可以恢复的删除记录。")
            return

        try:
            conv_id, history = await self._load_history(event)
            if not conv_id:
                self._reply(event, "当前还没有任何对话，无法恢复。")
                return
            history = history or []
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                conv_id,
                history=history + removed,
            )
        except Exception as exc:
            self._undo_stack[event.unified_msg_origin] = removed
            logger.error(f"[turn_eraser] 恢复失败: {exc}", exc_info=True)
            self._reply(event, f"恢复失败：{exc}")
            return

        self._reply(event, f"已恢复 {len(removed)} 条消息到当前对话末尾。")

    # -------------------------------------------------------------- 生命周期
    async def terminate(self) -> None:
        self._undo_stack.clear()
        try:
            await sp.remove_async("global", "global", "turn_eraser_state")
        except Exception:
            pass
        logger.info("[turn_eraser] 插件已卸载")
