"""forward_api 纯逻辑回归测试：不发真实消息、不依赖 AstrBot 运行时。

运行：在插件目录下 `python -m pytest tests -q`。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_forward_helper.forward_api import (  # noqa: E402
    ForwardResult,
    build_chunks,
    is_supported_image_source,
    normalize_items,
    platform_type_of,
    send_forward,
    supports_merged_forward,
)

# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #
class _StubMeta:
    def __init__(self, name: str):
        self.name = name


class _StubInst:
    def __init__(self, name: str):
        self._meta = _StubMeta(name)

    def meta(self) -> _StubMeta:
        return self._meta


class _StubContext:
    def __init__(self, platforms: dict[str, str]):
        self._platforms = platforms  # 平台实例 id -> 平台类型名

    def get_platform_inst(self, pid: str):
        name = self._platforms.get(pid)
        return _StubInst(name) if name else None


class _StubEvent:
    def __init__(self, platform_name: str = "", platform_id: str = "", umo: str = ""):
        self._name = platform_name
        self._pid = platform_id
        self.unified_msg_origin = umo

    def get_platform_name(self) -> str:
        return self._name

    def get_platform_id(self) -> str:
        return self._pid


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# items 类型校验（P1：字符串按字符炸成节点的回归）
# --------------------------------------------------------------------------- #
def test_items_json_string_is_parsed():
    result = _run(
        send_forward(None, '[{"text":"hello"}]', umo="aiocqhttp:GroupMessage:1", dry_run=True)
    )
    assert result.ok
    assert result.sent_items == 1


def test_items_invalid_json_string_rejected_not_iterated():
    result = _run(send_forward(None, "not json", umo="aiocqhttp:GroupMessage:1", dry_run=True))
    assert not result.ok
    assert "JSON" in result.reason


def test_items_quoted_json_string_rejected_not_iterated():
    # 合法 JSON 但解析结果是字符串：必须拒绝，不能按字符拆成节点
    result = _run(send_forward(None, '"abc"', umo="aiocqhttp:GroupMessage:1", dry_run=True))
    assert not result.ok
    assert "数组" in result.reason


def test_items_non_list_rejected():
    result = _run(send_forward(None, {"text": "x"}, umo="aiocqhttp:GroupMessage:1", dry_run=True))
    assert not result.ok
    assert "数组" in result.reason


# --------------------------------------------------------------------------- #
# 图片来源校验与 dry_run 可见性
# --------------------------------------------------------------------------- #
def test_is_supported_image_source():
    assert is_supported_image_source("http://a/b.jpg")
    assert is_supported_image_source("https://a/b.jpg")
    assert is_supported_image_source("file:///C:/x.png")
    assert is_supported_image_source("data:image/png;base64,AAAA")
    assert is_supported_image_source("C:/local/x.png")
    assert not is_supported_image_source("data:text/plain,hi")


def test_dry_run_reports_missing_local_images():
    result = _run(
        send_forward(
            None,
            [{"text": "a", "images": ["Z:/no_such_dir_xyz/a.jpg"]}],
            umo="aiocqhttp:GroupMessage:1",
            dry_run=True,
        )
    )
    assert result.ok  # 预检只提示，不拦截
    assert "不存在" in result.detail


def test_dry_run_reports_truncated_images():
    images = [f"http://x/{i}.jpg" for i in range(12)]
    result = _run(
        send_forward(
            None,
            [{"text": "a", "images": images}],
            umo="aiocqhttp:GroupMessage:1",
            dry_run=True,
        )
    )
    assert result.ok
    assert "3 张图片" in result.detail


def test_unsupported_image_source_rejected_before_send():
    result = _run(
        send_forward(
            None,
            [{"text": "a", "images": ["data:text/plain,hi"]}],
            umo="aiocqhttp:GroupMessage:1",
            dry_run=True,
        )
    )
    assert not result.ok
    assert "不支持的图片地址" in result.reason


# --------------------------------------------------------------------------- #
# 平台类型解析
# --------------------------------------------------------------------------- #
CTX = {"qq": "aiocqhttp", "aiocqhttp": "aiocqhttp", "telegram": "telegram", "mytg": "telegram"}


def test_same_session_uses_event_platform():
    event = _StubEvent("aiocqhttp", "qq", "aiocqhttp:GroupMessage:111")
    assert platform_type_of(_StubContext(CTX), event, "aiocqhttp:GroupMessage:111") == "aiocqhttp"


def test_cron_event_falls_back_to_target_umo_platform():
    event = _StubEvent("cron", "cron", "cron:Cron:job")
    got = platform_type_of(_StubContext(CTX), event, "aiocqhttp:GroupMessage:222")
    assert got == "aiocqhttp"


def test_cross_session_prefers_target_platform_even_if_not_onebot():
    event = _StubEvent("aiocqhttp", "qq", "aiocqhttp:GroupMessage:111")
    got = platform_type_of(_StubContext(CTX), event, "telegram:Friend:333")
    assert got == "telegram"


def test_cross_session_resolves_instance_id_to_type_name():
    # UMO 前缀是平台实例 id（可自定义），反查后应得到类型名
    event = _StubEvent("aiocqhttp", "qq", "aiocqhttp:GroupMessage:111")
    got = platform_type_of(_StubContext(CTX), event, "mytg:Friend:444")
    assert got == "telegram"


def test_cross_session_unresolvable_target_falls_back_to_event():
    event = _StubEvent("aiocqhttp", "qq", "aiocqhttp:GroupMessage:111")
    got = platform_type_of(_StubContext(CTX), event, "unknown:Friend:444")
    assert got == "aiocqhttp"


def test_no_event_no_context_uses_umo_prefix():
    assert platform_type_of(None, None, "aiocqhttp:GroupMessage:1") == "aiocqhttp"


def test_supports_merged_forward():
    assert supports_merged_forward("aiocqhttp")
    assert supports_merged_forward("onebot_v11")
    assert supports_merged_forward("OneBot")
    assert supports_merged_forward("aiocqhttp_pro")
    assert not supports_merged_forward("telegram")
    assert not supports_merged_forward("")


# --------------------------------------------------------------------------- #
# 构造与结果语义
# --------------------------------------------------------------------------- #
def test_normalize_items_mixed_inputs():
    items = normalize_items(["纯文本", {"name": "a", "text": "t", "images": ["http://x/1.jpg"]}, "", {"text": "", "images": []}])
    assert len(items) == 2
    assert items[0].text == "纯文本"
    assert items[1].name == "a" and items[1].images == ["http://x/1.jpg"]


def test_build_chunks_sizes():
    items = normalize_items([f"n{i}" for i in range(25)])
    assert [len(c) for c in build_chunks(items, 20)] == [20, 5]
    assert [len(c) for c in build_chunks(items, 0)] == [20, 5]  # 非法 size 用默认
    assert build_chunks([], 20) == [[]]


def test_summary_partial_success_distinct_from_full_failure():
    partial = ForwardResult(
        ok=True, merged=False, sent_items=25, sent_nodes=20, chunks=2,
        merged_chunks=1, failed_chunks=0, degraded=True,
    )
    text = partial.summary()
    assert "部分成功" in text
    assert "合并送达 1 块" in text
    assert "未成立" not in text

    full = ForwardResult(ok=True, merged=True, chunks=1, merged_chunks=1)
    assert full.summary().startswith("合并转发成功")

    fail = ForwardResult(ok=False, chunks=2, merged_chunks=0, failed_chunks=1, degraded=False)
    assert fail.summary().startswith("合并转发失败")

    degrade_fail = ForwardResult(ok=False, degraded=True, merged_chunks=0, failed_chunks=1)
    assert degrade_fail.summary().startswith("降级逐条发送失败")
