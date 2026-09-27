"""astrbot_plugin_forward_helper

一个纯粹的合并转发方法：`forward_api.send_forward(...)`，
由 agent（或其它插件）在需要时自行调用，不做任何条件门禁。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .forward_api import send_forward

PLUGIN_NAME = "astrbot_plugin_forward_helper"


def _cfg(config, key, default):
    try:
        value = config.get(key, default) if config is not None else default
    except Exception:
        value = default
    return default if value is None else value


def _event_of(context: ContextWrapper[AstrAgentContext]):
    try:
        return getattr(context.context, "event", None)
    except Exception:
        return None


class SendForwardTool(FunctionTool):
    """把若干条内容打包成一条 QQ 合并转发聊天记录。"""

    def __init__(self, plugin: "ForwardHelperPlugin"):
        super().__init__(
            name="send_forward",
            description=(
                "把若干条内容打包成一条 QQ「合并转发」聊天记录发出。"
                "只该在用户明确要求合并转发/打包发送时调用。"
                "参数 items 按顺序成为聊天记录里的每个节点。"
                "平台不支持 Node 时自动降级为逐条发送。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": (
                            "要转发的条目，按顺序成为聊天记录里的每个节点。"
                            "每项形如 {name: 署名, text: 正文, images: [本地路径或http链接]}。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "该节点的署名"},
                                "text": {"type": "string", "description": "该节点的正文"},
                                "images": {
                                    "type": "array",
                                    "description": "该节点附带的图片，本地路径或 http(s) 链接",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                    "title": {
                        "type": "string",
                        "description": "可选，聊天记录首个节点顶部的标题",
                    },
                    "umo": {
                        "type": "string",
                        "description": "可选，目标会话 platform:message_type:session_id；缺省发回当前会话",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "可选，true 表示只回报将要发送的内容与目标，不实际发送",
                    },
                },
                "required": ["items"],
            },
        )
        self.plugin = plugin

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        cfg = self.plugin.config
        result = await send_forward(
            self.plugin.context,
            kwargs.get("items"),
            event=_event_of(context),
            umo=str(kwargs.get("umo") or ""),
            title=str(kwargs.get("title") or ""),
            chunk_size=int(_cfg(cfg, "chunk_size", 6)),
            retries=int(_cfg(cfg, "retries", 2)),
            degrade_on_failure=bool(_cfg(cfg, "degrade_on_failure", True)),
            default_node_name=str(_cfg(cfg, "default_node_name", "合并转发")),
            default_uin=str(_cfg(cfg, "default_uin", "0")),
            dry_run=bool(kwargs.get("dry_run")),
        )
        logger.info(f"[{PLUGIN_NAME}] send_forward -> {result.summary()}")
        return result.summary()


class ForwardHelperPlugin(Star):
    """提供合并转发方法。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        # 全插件只注册这一份 LLM 工具；不要再用 @filter.llm_tool 注册同名工具
        # （AstrBot 会按名字替换，谁生效取决于注册顺序）。
        try:
            self.context.add_llm_tools(SendForwardTool(self))
        except Exception as exc:  # pragma: no cover
            logger.error(f"[{PLUGIN_NAME}] 注册工具失败: {type(exc).__name__}: {exc}")

    async def terminate(self):
        logger.info(f"[{PLUGIN_NAME}] terminated")
