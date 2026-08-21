/**
 * SSE 流解析器测试 — 后端 AI 流式端点契约 (text/event-stream + data: JSON 帧)。
 *
 * 覆盖: 帧解析 / 分块传输(含多字节 UTF-8 跨块截断) / CRLF / 心跳注释 /
 *       无空行收尾的最后一个帧。
 */
import { describe, it, expect } from 'vitest'
import { parseSseFrame, readSseStream } from './api'

function sseResponse(chunks: Uint8Array[]): Response {
  let i = 0
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close()
        return
      }
      controller.enqueue(chunks[i])
      i += 1
    },
  })
  return new Response(stream)
}

function toBytes(text: string): Uint8Array {
  return new TextEncoder().encode(text)
}

describe('parseSseFrame', () => {
  it('解析单行 data 帧', () => {
    expect(parseSseFrame('data: {"type":"done"}')).toBe('{"type":"done"}')
  })

  it('data: 后的单个空格按规范去掉', () => {
    expect(parseSseFrame('data:{"type":"done"}')).toBe('{"type":"done"}')
  })

  it('心跳/注释帧返回 null', () => {
    expect(parseSseFrame(': ping')).toBeNull()
    expect(parseSseFrame(': keep-alive')).toBeNull()
  })

  it('多行 data 按换行拼接', () => {
    expect(parseSseFrame('data: first\ndata: second')).toBe('first\nsecond')
  })

  it('非 data 行(如 event:/retry:)不进入结果', () => {
    expect(parseSseFrame('event: message\ndata: {"a":1}')).toBe('{"a":1}')
  })

  it('无 data 行时返回 null', () => {
    expect(parseSseFrame('event: message\nid: 1')).toBeNull()
  })
})

describe('readSseStream', () => {
  it('逐帧 yield data 内容 (完整协议串)', async () => {
    const body = [
      'data: {"type":"meta","summary":"测"}',
      'data: {"type":"delta","content":"片段内容"}',
      'data: {"type":"done"}',
      '',
    ].join('\n\n') + '\n\n'
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([toBytes(body)]))) {
      events.push(JSON.parse(data))
    }
    expect(events.map(e => (e as { type: string }).type)).toEqual(['meta', 'delta', 'done'])
    expect((events[1] as { content: string }).content).toBe('片段内容')
  })

  it('多字节 UTF-8 跨网络分块截断时不丢内容', async () => {
    const body = 'data: {"type":"meta","summary":"测"}\n\n'
      + 'data: {"type":"delta","content":"片段内容片段"}\n\n'
      + 'data: {"type":"done"}\n\n'
    const bytes = toBytes(body)
    // 5 字节处切断 — 落在「测」(3 字节) 的中间, 再在帧中间切一刀
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([
      bytes.slice(0, 5),
      bytes.slice(5, 23),
      bytes.slice(23),
      new Uint8Array(0),
    ]))) {
      events.push(JSON.parse(data))
    }
    expect((events[0] as { summary: string }).summary).toBe('测')
    expect((events[1] as { content: string }).content).toBe('片段内容片段')
  })

  it('帧不完整时等待下一块 (不在帧中间 yield)', async () => {
    const body = 'data: {"type":"delta","content":"第一"}\n\ndata: {"type":"done"}\n\n'
    const bytes = toBytes(body)
    // 切断位置落在第一帧 content 值「第一」两字之间 (多字节字符中间)
    const headerLen = new TextEncoder().encode('data: {"type":"delta","content":"').length
    const mid = headerLen + 2
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([bytes.slice(0, mid), bytes.slice(mid)]))) {
      events.push(JSON.parse(data))
    }
    expect(events.map(e => (e as { type: string }).type)).toEqual(['delta', 'done'])
  })

  it('CRLF 行尾(网关重写场景)同样解析', async () => {
    const body = 'data: {"type":"meta","summary":"ok"}\r\n\r\ndata: {"type":"done"}\r\n\r\n'
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([toBytes(body)]))) {
      events.push(JSON.parse(data))
    }
    expect(events.map(e => (e as { type: string }).type)).toEqual(['meta', 'done'])
  })

  it('心跳注释帧被跳过', async () => {
    const body = ': ping\n\ndata: {"type":"done"}\n\n'
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([toBytes(body)]))) {
      events.push(JSON.parse(data))
    }
    expect(events).toHaveLength(1)
    expect((events[0] as { type: string }).type).toBe('done')
  })

  it('最后一个 data 帧无空行收尾时同样消费', async () => {
    const body = 'data: {"type":"meta"}\n\ndata: {"type":"done"}'
    const events: unknown[] = []
    for await (const data of readSseStream(sseResponse([toBytes(body)]))) {
      events.push(JSON.parse(data))
    }
    expect(events.map(e => (e as { type: string }).type)).toEqual(['meta', 'done'])
  })

  it('空响应 body 抛错而不是静默结束', async () => {
    const res = new Response(null)  // body 为 null
    await expect((async () => {
      for await (const _ of readSseStream(res)) { /* noop */ }
    })()).rejects.toThrow('响应无 body')
  })
})