# astrbot_plugin_forward_helper

> **这是一个 demo 插件**：演示如何把合并转发封装成 agent 工具与可复用方法，保持最小实现。欢迎参考实现，或直接 import `forward_api.send_forward(...)` 取用。

一个纯粹的合并转发方法。**不设任何条件门禁** —— 需要时由 agent（或其它插件）自行调用。

## 用法

### 工具（LLM function calling）

| 工具 | 作用 |
| --- | --- |
| `send_forward` | 把若干条内容打包成一条 QQ「合并转发」聊天记录发出 |

参数：

- `items`（必填）：数组，每项 `{name, text, images[]}`，按顺序成为聊天记录里的每个节点；
- `title`（可选）：首个节点顶部的标题；
- `umo`（可选）：目标会话 `platform:message_type:session_id`，缺省发回当前会话；
- `dry_run`（可选）：只回报将要发送的内容与目标，不实际发送。

### 代码调用

```python
from astrbot_plugin_forward_helper.forward_api import send_forward

result = await send_forward(
    self.context,
    [
        {"name": "@someone", "text": "正文", "images": ["/tmp/a.jpg"]},
        {"name": "@other",  "text": "第二条", "images": ["https://example.com/b.jpg"]},
    ],
    event=event,          # 给了就发回当前会话
    umo="",               # 或指定其它会话
    title="今日精选",
    dry_run=False,
)
print(result.summary())
```

## 配置（都是技术参数，不是门禁）

| 配置项 | 默认 | 含义 |
| --- | --- | --- |
| `chunk_size` | `20` | 每块节点数，超出则分多条聊天记录 |
| `retries` | `2` | 单块重试次数 |
| `degrade_on_failure` | `true` | 失败/平台不支持时降级为逐条发送 |
| `default_node_name` / `default_uin` | `合并转发` / `0` | 节点署名兜底 |

## 行为

- `aiocqhttp` / `onebot` / `onebot_v11` 走真正的 `Node` / `Nodes` 合并转发；
- 其它平台或发送被平台拒绝时，按 `degrade_on_failure` 逐条普通消息发送，并在返回值里标记「已降级」；
- 条目超过 `chunk_size` 时按块依次发送，失败只影响该块；部分块失败时结果区分「全部合并送达 / 部分块降级 / 部分未送达」（`merged_chunks` / `failed_chunks`）；
- 图片支持本地路径（`Image.fromFileSystem`）、`file://`、`http(s)` 链接（`Image.fromURL`）与 base64 data URI，单节点最多 9 张，超出部分忽略并在结果 detail 中注明；
- `items` 也可整体传能解析为数组的 JSON 字符串；无法解析或不是数组会直接报错，不会发送；
- `Node` / `Nodes` 仅 OneBot v11 支持，这是平台能力而非插件限制。
