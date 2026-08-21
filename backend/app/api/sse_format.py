"""AI 流式端点的 SSE (text/event-stream) 帧格式辅助。

此前这些端点用 application/x-ndjson(每行一个 JSON)冒充流式,浏览器
DevTools 里看不到标准 SSE 帧,代理/网关也可能按普通 JSON 缓冲。
统一改为标准 SSE: 每个事件一行 ``data: {json}`` + 空行结尾。

注意: payload 必须是单行 JSON 字符串(事件内部的换行已由 json.dumps
转义为 \\n),这样每个事件恰好是一条 data 行,前后端解析都无歧义。
"""
from __future__ import annotations

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def sse_event(payload: str) -> str:
    """把一行 JSON 事件串包装成标准 SSE data 帧(以空行结尾)。"""
    return f"data: {payload}\n\n"