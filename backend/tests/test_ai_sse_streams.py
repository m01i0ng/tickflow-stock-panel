"""AI 流式端点必须输出标准 SSE (text/event-stream), 而非 NDJSON。

历史上这些端点用 application/x-ndjson(每行一个 JSON), 浏览器 DevTools
看不到标准 SSE 帧, 代理也可能整体缓冲后才下发。这里直接调用端点函数并
逐帧断言 data 帧格式, 防止回退到 NDJSON。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.api import financials, market_recap, rps, stock_analysis
from app.api.sse_format import SSE_HEADERS, sse_event


def test_sse_event_frames_payload():
    assert sse_event('{"type":"done"}') == 'data: {"type":"done"}\n\n'


def test_sse_headers_disable_buffering():
    assert SSE_HEADERS["Cache-Control"] == "no-cache"
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"


def _fake_request() -> SimpleNamespace:
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(".")))
    state = SimpleNamespace(repo=repo, capabilities=SimpleNamespace())
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _fake_events(*args, **kwargs):
    yield json.dumps({"type": "meta", "summary": "测试"}, ensure_ascii=False)
    yield json.dumps({"type": "delta", "content": "片段"}, ensure_ascii=False)
    yield json.dumps({"type": "done"}, ensure_ascii=False)


async def _collect(response) -> str:
    parts = []
    async for chunk in response.body_iterator:
        parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


def _parse_events(body: str) -> list[dict]:
    events = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame.startswith("data: "):
            continue
        events.append(json.loads(frame[len("data: "):]))
    return events


async def _assert_sse_endpoint(response):
    assert response.media_type == "text/event-stream"
    body = await _collect(response)
    events = _parse_events(body)
    assert [e["type"] for e in events] == ["meta", "delta", "done"]
    assert events[1]["content"] == "片段"
    return body


async def test_stock_analysis_analyze_is_sse(monkeypatch):
    monkeypatch.setattr(stock_analysis, "analyze_stock_stream", _fake_events)
    req = stock_analysis.AnalyzeRequest(symbol="000001.SZ", focus="")
    response = await stock_analysis.analyze_stock(_fake_request(), req)
    await _assert_sse_endpoint(response)


async def test_financials_analyze_is_sse(monkeypatch):
    monkeypatch.setattr(financials, "_require_financial", lambda capset: None)
    monkeypatch.setattr(financials, "analyze_financials_stream", _fake_events)
    req = financials.AnalyzeRequest(symbol="000001.SZ", focus="")
    response = await financials.analyze_financials(_fake_request(), req)
    await _assert_sse_endpoint(response)


async def test_market_recap_analyze_is_sse(monkeypatch):
    monkeypatch.setattr(market_recap, "recap_market_stream", _fake_events)
    req = market_recap.AnalyzeRequest(as_of=None, focus="")
    response = await market_recap.analyze_market(_fake_request(), req)
    await _assert_sse_endpoint(response)


async def test_rotation_analyze_is_sse(monkeypatch):
    monkeypatch.setattr(rps, "analyze_rotation_stream", _fake_events)
    req = rps.AnalyzeRequest(days=12, kind="concept", focus="")
    response = await rps.analyze_rotation(_fake_request(), req)
    await _assert_sse_endpoint(response)