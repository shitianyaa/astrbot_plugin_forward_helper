"""合并转发辅助方法。

一个方法：`send_forward(...)` —— 把若干条内容打包成一条 QQ「合并转发」聊天记录发出。
不做任何条件门禁：由调用方（agent / 其它插件）在需要时自行调用。

平台事实：`Node` / `Nodes` 仅 OneBot v11（aiocqhttp）支持；其它平台或发送被拒时，
按 `degrade_on_failure` 降级为逐条普通消息发送。

图片来源：http(s) 链接、file://、base64 data URI、本地路径；单节点最多 9 张，
超出的部分会被忽略并在结果 detail 中注明。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse
from urllib.request import url2pathname

from astrbot.api import logger

try:  # 兼容不同版本的导入路径
    from astrbot.api.message_components import Image, Node, Nodes, Plain
except ImportError:  # pragma: no cover
    from astrbot.core.message.components import Image, Node, Nodes, Plain

try:
    from astrbot.api.all import MessageChain
except ImportError:  # pragma: no cover
    from astrbot.api.event import MessageChain


#: 支持 Node/Nodes 合并转发的平台 ID（OneBot v11 系）
PLATFORMS_WITH_NODES: tuple[str, ...] = ("aiocqhttp", "onebot", "onebot_v11")

DEFAULT_CHUNK_SIZE = 6
MAX_IMAGES_PER_NODE = 9


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ForwardItem:
    """一个转发节点：署名 + 文本 + 若干图片（本地路径或 http(s) 链接）。"""

    name: str = ""
    uin: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ForwardResult:
    ok: bool
    merged: bool = False  # 是否全部块都以 Nodes 合并转发送达
    reason: str = ""
    detail: str = ""
    sent_items: int = 0
    sent_nodes: int = 0
    chunks: int = 0
    merged_chunks: int = 0  # 以合并转发送达的块数（部分成功时与 degraded/failed 共同刻画）
    failed_chunks: int = 0  # 最终未送达的块数
    degraded: bool = False
    dry_run: bool = False

    def summary(self) -> str:
        if self.dry_run:
            head = "合并转发预检通过（未发送）" if self.ok else "合并转发预检不通过"
        elif self.ok and self.merged:
            head = "合并转发成功"
        elif self.ok and self.merged_chunks:
            head = "合并转发部分成功，失败块已降级为逐条发送"
        elif self.ok and self.degraded:
            head = "合并转发未成立，已降级为逐条发送"
        elif self.ok:
            head = "发送成功"
        elif self.merged_chunks or not self.degraded:
            head = "合并转发失败"
        else:
            head = "降级逐条发送失败"
        parts = [head]
        if self.ok and not self.dry_run:
            stats = (
                f"条目 {self.sent_items} 个 / 合并节点 {self.sent_nodes} 个"
                f" / 分块 {self.chunks} 块"
            )
            if self.chunks and 0 < self.merged_chunks < self.chunks:
                stats += f"（其中合并送达 {self.merged_chunks} 块）"
            parts.append(stats)
        if self.reason:
            parts.append(f"原因：{self.reason}")
        if self.detail:
            parts.append(f"细节：{self.detail}")
        return "；".join(parts)


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #
def normalize_items(raw: Iterable[Any] | None) -> list[ForwardItem]:
    """把 dict / 字符串 / ForwardItem 混合输入统一成 ForwardItem 列表。"""
    items: list[ForwardItem] = []
    for entry in raw or []:
        if isinstance(entry, ForwardItem):
            items.append(entry)
            continue
        if isinstance(entry, str):
            items.append(ForwardItem(text=entry))
            continue
        if isinstance(entry, dict):
            images = entry.get("images") or entry.get("image") or []
            if isinstance(images, str):
                images = [images]
            items.append(
                ForwardItem(
                    name=str(entry.get("name") or ""),
                    uin=str(entry.get("uin") or ""),
                    text=str(entry.get("text") or entry.get("content") or ""),
                    images=[str(p) for p in images if str(p).strip()],
                )
            )
            continue
        items.append(ForwardItem(text=str(entry)))
    return [i for i in items if i.text or i.images]


def is_supported_image_source(source: str) -> bool:
    """http(s)、file://、base64 data URI 或本地路径；其余（如非 base64 的 data URI）不支持。"""
    src = str(source).strip()
    lowered = src.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return True
    if lowered.startswith("data:"):
        return "base64," in src
    return True  # 其余一律按本地路径处理，存在性由 dry_run 提示或发送侧报错


def missing_local_images(items: Sequence[ForwardItem]) -> list[str]:
    """dry_run 用：列出不存在且又不是远程/data URI 的图片路径。"""
    missing: list[str] = []
    for item in items:
        for src in item.images[:MAX_IMAGES_PER_NODE]:
            s = str(src).strip()
            if s.lower().startswith(("http://", "https://", "data:", "file://")):
                continue
            if not os.path.isfile(s):
                missing.append(s)
    return missing


def build_image(source: str) -> Any:
    """base64 data URI → fromBytes；file:// → 还原本地路径；http(s) → fromURL；其余按本地路径。"""
    src = str(source).strip()
    lowered = src.lower()
    if lowered.startswith("data:"):
        if "base64," not in src:
            raise ValueError("仅支持 base64 编码的 data URI 图片")
        return Image.fromBytes(base64.b64decode(src.split(",", 1)[1]))
    if lowered.startswith("file://"):
        return Image.fromFileSystem(url2pathname(urlparse(src).path))
    if lowered.startswith(("http://", "https://")):
        return Image.fromURL(src)
    return Image.fromFileSystem(src)


def build_node(item: ForwardItem, title: str = "", default_name: str = "合并转发", default_uin: str = "0") -> Any:
    """构造单个 Node：标题（可选）+ 文本 + 图片。"""
    content: list[Any] = []
    if title:
        content.append(Plain(title))
    if item.text:
        content.append(Plain(item.text))
    for src in item.images[:MAX_IMAGES_PER_NODE]:
        content.append(build_image(src))
    if not content:  # 兜底，Node 内容不能为空
        content.append(Plain("(空节点)"))
    uin = (item.uin or default_uin or "0").strip() or "0"
    name = (item.name or default_name or "合并转发").strip()
    return Node(uin=uin, name=name, content=content)


def build_chunks(items: Sequence[ForwardItem], size: int = DEFAULT_CHUNK_SIZE) -> list[list[ForwardItem]]:
    step = size if size and size > 0 else DEFAULT_CHUNK_SIZE
    return [list(items[i : i + step]) for i in range(0, len(items), step)] or [[]]


def build_nodes(
    items: Sequence[ForwardItem],
    title: str = "",
    default_name: str = "合并转发",
    default_uin: str = "0",
) -> Any:
    """把条目包成一个 Nodes（一条聊天记录）。"""
    return Nodes([build_node(i, title, default_name, default_uin) for i in items])


# --------------------------------------------------------------------------- #
# 平台能力
# --------------------------------------------------------------------------- #
def platform_type_of(context: Any = None, event: Any = None, umo: str = "") -> str:
    """取平台**类型**（如 aiocqhttp），不是平台实例 id。

    注意：`event.get_platform_id()` 返回的是平台实例 id（用户自定义，如 "default"），
    平台类型必须走 `event.get_platform_name()`；没有 event 时才用
    `context.get_platform_inst(id).meta().name` 反查。

    平台名跟「发到哪」走：
    - 未传 umo 或 umo 就是事件所在会话：事件平台名支持 Node 时直接返回；否则按
      umo 前缀 / 事件平台实例 id 反查真实平台类型，**优先返回支持 Node 的那个**；
      全都不支持时退回事件平台名。定时任务（cron）触发的运行里事件平台名恒为
      "cron"，靠这条反查路径落到目标会话的真实平台上（2026-09-25 补丁）。
    - umo 指向其它会话（跨会话）：以目标 umo 前缀反查的平台类型为准——哪怕它是
      telegram 这类不支持 Node 的平台也据实返回，让调用方直接降级，而不是把
      Nodes 发去不支持的平台白白重试。
    """
    event_name = ""
    if event is not None:
        try:
            event_name = str(event.get_platform_name() or "").strip()
        except Exception:  # pragma: no cover
            event_name = ""
    event_pid = ""
    if event is not None:
        try:
            event_pid = str(event.get_platform_id() or "").strip()
        except Exception:  # pragma: no cover
            event_pid = ""
    umo_pid = umo.split(":", 1)[0].strip() if umo else ""

    current_umo = ""
    if event is not None:
        try:
            current_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        except Exception:  # pragma: no cover
            current_umo = ""

    def _inst_name(pid: str) -> str:
        if not pid or context is None:
            return ""
        try:
            inst = context.get_platform_inst(pid)
            if inst is None:
                return ""
            return str(getattr(inst.meta(), "name", "") or "").strip()
        except Exception:  # pragma: no cover
            return ""

    same_session = (not umo) or umo == current_umo

    if not same_session:
        # 跨会话：目标会话的平台类型说了算
        target_name = _inst_name(umo_pid)
        if target_name:
            return target_name
        if supports_merged_forward(event_name):
            return event_name

    if supports_merged_forward(event_name):
        return event_name

    # 事件平台不支持 Node：按 umo 前缀 → 事件平台实例 id 的顺序反查，
    # 优先返回支持 Node 的那个
    for pid in (umo_pid, event_pid):
        name = _inst_name(pid)
        if name and supports_merged_forward(name):
            return name

    # 全都不支持时保持原有语义（事件平台名优先）
    if event_name:
        return event_name
    for pid in (umo_pid, event_pid):
        name = _inst_name(pid)
        if name:
            return name
    return umo_pid or event_pid


def supports_merged_forward(platform_type: str) -> bool:
    """OneBot v11（aiocqhttp）支持 Node/Nodes；类型名可能带后缀，故用包含判断。"""
    value = (platform_type or "").strip().lower()
    if not value:
        return False
    return value in PLATFORMS_WITH_NODES or any(
        value.startswith(p) for p in PLATFORMS_WITH_NODES
    )


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
async def _send_chain(
    context: Any, event: Any, target_umo: str, chain: list[Any], retries: int
) -> tuple[bool, str]:
    last = ""
    same_session = bool(event is not None and event.unified_msg_origin == target_umo)
    for attempt in range(1, max(retries, 1) + 1):
        try:
            if same_session:
                await event.send(event.chain_result(chain))
            else:
                await context.send_message(target_umo, MessageChain(chain))
            return True, ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"[forward_helper] 发送失败 attempt={attempt}/{max(retries, 1)}: {last}"
            )
            if attempt < max(retries, 1):
                await asyncio.sleep(min(attempt * 2, 6))
    return False, last


async def _send_degraded(
    context: Any, event: Any, target_umo: str, items: Sequence[ForwardItem]
) -> tuple[bool, str]:
    """降级：逐条普通消息发送（署名 + 文本 + 图片）。"""
    same_session = bool(event is not None and event.unified_msg_origin == target_umo)
    for item in items:
        chain: list[Any] = []
        if item.name:
            chain.append(Plain(f"{item.name}："))
        if item.text:
            chain.append(Plain(item.text))
        for src in item.images[:MAX_IMAGES_PER_NODE]:
            chain.append(build_image(src))
        if not chain:
            continue
        try:
            if same_session:
                await event.send(event.chain_result(chain))
            else:
                await context.send_message(target_umo, MessageChain(chain))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return True, ""


async def send_forward(
    context: Any,
    items: Iterable[Any] | str | None,
    *,
    event: Any = None,
    umo: str = "",
    title: str = "",
    chunk_size: int | None = None,
    retries: int = 2,
    degrade_on_failure: bool = True,
    default_node_name: str = "合并转发",
    default_uin: str = "0",
    dry_run: bool = False,
) -> ForwardResult:
    """合并转发主方法。

    Args:
        context: AstrBot 的 Context 对象（插件里的 self.context）。
        items: 条目列表，元素可为 dict{name,text,images[]}、字符串或 ForwardItem；
            也可整体传一个能解析为数组的 JSON 字符串。
        event: 触发事件；给了就取所在会话，并可直接用 event 发回当前会话。
        umo: 目标会话（platform:message_type:session_id）；缺省用 event 所在会话。
            指向其它会话时按目标平台能力判断是否合并转发。
        title: 首个节点顶部的标题文本。
        chunk_size: 每块节点数（默认 6），超过则分多条聊天记录发送。
        retries: 单块发送失败时的重试次数（总尝试 = 1 + retries，至少尝试 1 次）。
        degrade_on_failure: 平台不支持或被拒时，是否降级为逐条发送。
        default_node_name / default_uin: 条目未指定时的署名与 uin 兜底。
        dry_run: 只回报将要发送的内容与目标（含本地图片缺失提示），不实际发送。

    Returns:
        ForwardResult：ok 表示全部块最终送达（合并或降级）；
        merged/merged_chunks/failed_chunks/degraded 刻画部分成功与降级明细。
    """
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            return ForwardResult(
                ok=False,
                reason="items 无法按 JSON 解析：请传数组，或能解析为数组的 JSON 字符串",
            )
    if items is not None and not isinstance(items, (list, tuple)):
        return ForwardResult(
            ok=False,
            reason=f"items 需要是数组，收到 {type(items).__name__}",
        )

    normalized = normalize_items(items)
    current_umo = getattr(event, "unified_msg_origin", "") if event is not None else ""
    target_umo = (umo or current_umo).strip()

    if not normalized:
        return ForwardResult(ok=False, reason="没有可发送的条目")
    if not target_umo:
        return ForwardResult(ok=False, reason="缺少目标会话（既没有 event 也没有 umo）")

    bad_images = [
        str(src).strip()
        for item in normalized
        for src in item.images[:MAX_IMAGES_PER_NODE]
        if not is_supported_image_source(src)
    ]
    if bad_images:
        return ForwardResult(
            ok=False,
            reason=(
                f"不支持的图片地址 {bad_images[0]!r}："
                "仅支持 http(s)、file://、base64 data URI 或本地路径"
            ),
        )

    truncated_images = sum(
        max(0, len(item.images) - MAX_IMAGES_PER_NODE) for item in normalized
    )
    trunc_note = (
        f"；{truncated_images} 张图片超出单节点 {MAX_IMAGES_PER_NODE} 张上限，将被忽略"
        if truncated_images
        else ""
    )

    platform_type = platform_type_of(context, event, target_umo)
    chunks = build_chunks(normalized, chunk_size or DEFAULT_CHUNK_SIZE)

    if dry_run:
        missing = missing_local_images(normalized)
        detail = (
            f"未实际发送；目标会话 {target_umo}，平台类型 {platform_type or '未知'}，"
            f"合并转发{'可用' if supports_merged_forward(platform_type) else '不可用（将降级）'}"
        )
        if missing:
            detail += f"；注意 {len(missing)} 个本地图片文件不存在：{'、'.join(missing[:3])}"
        detail += trunc_note
        return ForwardResult(
            ok=True,
            dry_run=True,
            sent_items=len(normalized),
            chunks=len(chunks),
            detail=detail,
        )

    if supports_merged_forward(platform_type):
        total = len(chunks)
        merged_chunks = 0
        degraded_chunks = 0
        failed_chunks = 0
        first_error = ""
        sent_nodes = 0
        for index, chunk in enumerate(chunks):
            nodes = build_nodes(
                chunk,
                title if index == 0 else "",
                default_node_name,
                default_uin,
            )
            ok, err = await _send_chain(context, event, target_umo, [nodes], retries)
            if ok:
                merged_chunks += 1
                sent_nodes += len(chunk)
                continue
            first_error = err or first_error
            if not degrade_on_failure:
                failed_chunks += 1
                break
            ok, err2 = await _send_degraded(context, event, target_umo, chunk)
            if ok:
                degraded_chunks += 1
            else:
                failed_chunks += 1
                first_error = err2 or first_error
        all_delivered = (merged_chunks + degraded_chunks) == total
        detail = ""
        if merged_chunks < total:
            detail = (
                f"合并送达 {merged_chunks}/{total} 块"
                + (f"，{degraded_chunks} 块降级为逐条发送" if degraded_chunks else "")
                + (f"，{failed_chunks} 块未送达" if failed_chunks else "")
            )
        return ForwardResult(
            ok=all_delivered,
            merged=merged_chunks == total,
            reason="" if all_delivered else f"{failed_chunks} 块未送达（{first_error}）",
            detail=detail + trunc_note,
            sent_items=len(normalized),
            sent_nodes=sent_nodes,
            chunks=total,
            merged_chunks=merged_chunks,
            failed_chunks=failed_chunks,
            degraded=degraded_chunks > 0,
        )

    if not degrade_on_failure:
        return ForwardResult(
            ok=False,
            reason=f"平台类型 {platform_type or '未知'} 不支持合并转发（Node/Nodes 仅 OneBot v11 可用），且未开启降级",
            sent_items=len(normalized),
            chunks=len(chunks),
        )

    ok, err = await _send_degraded(context, event, target_umo, normalized)
    return ForwardResult(
        ok=ok,
        merged=False,
        reason="" if ok else err,
        sent_items=len(normalized),
        chunks=len(chunks),
        merged_chunks=0,
        failed_chunks=0 if ok else 1,
        degraded=True,
        detail=f"平台类型 {platform_type or '未知'} 不支持合并转发，已逐条发送" + trunc_note,
    )
