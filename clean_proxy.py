#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy / CodeBuddy JSON 清洗代理 (JSON Clean Proxy)

WorkBuddy(自定义模型) -> 127.0.0.1:8318 (本代理) -> 你的 LLM 网关 (OpenAI 兼容端点)

背景:
    WorkBuddy/CodeBuddy(与 Claude Code 同架构)在长会话/含大工具输出场景下,
    打包对话历史时会产生损坏的请求体, 典型报错:
        - 400 invalid JSON request body
        - 400 JSON parsing failed (OpenRouter / 上游网关侧)
    根因(抓包实锤):
        1. 截断的多字节 UTF-8 字符 (中文字符被切半 -> invalid continuation byte)
        2. 非法 JSON 转义 (如 \w \o 等非标准 escape)
        3. 深度结构损坏 (字符串边界错乱)

本代理在客户端与上游之间做三级修复:
    1. UTF-8 修复: 无效字节替换为 U+FFFD
    2. 非法转义修复: 非标准转义转义为字面量, 保证 JSON 合法
    3. 深度损坏重建: 无法整体解析时, 保留头部字段 + 最后一条完整消息 + 尾部字段

特性:
    - 流式(SSE)响应逐行透传, 非流式完整转发
    - 单请求线程隔离(ThreadingHTTPServer), 单次卡死不影响其他请求
    - 日志写入文件, 便于诊断
    - 上游不可达时返回 502 而非挂死

用法:
    python clean_proxy.py [--listen 127.0.0.1] [--port 8318] [--upstream http://127.0.0.1:13000] [--log clean-proxy.log]

依赖: 仅 Python 3 标准库, 无第三方依赖。
"""

import argparse
import http.server
import json
import re
import sys
import urllib.error
import urllib.request

# JSON 合法转义序列
VALID_ESC = set(b'"\\/bfnrtu')


def sanitize_utf8_and_escapes(raw: bytes) -> bytes:
    """修复非法转义 + 非法 UTF-8"""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5C:  # backslash
            if i + 1 < n:
                nxt = raw[i + 1]
                if nxt in VALID_ESC:
                    out.append(b)
                    out.append(nxt)
                    i += 2
                    continue
                else:
                    # 非法转义: 反斜杠转义为 \\，后续字符作为普通字符
                    out.append(b)
                    out.append(b)
                    i += 1
                    continue
            else:
                out.append(b)
                i += 1
                continue
        else:
            out.append(b)
            i += 1
    try:
        return bytes(out).decode('utf-8', errors='replace').encode('utf-8')
    except Exception:
        return bytes(out)


def rebuild_payload(raw: bytes) -> bytes:
    """深度损坏时重建: 保留头部字段 + 最后一条完整消息 + 尾部字段"""
    txt = raw.decode('utf-8', errors='replace')
    head = {}
    # 提取顶层简单字段
    for key in ['model', 'stream', 'max_tokens', 'temperature', 'reasoning', 'tools', 'tool_choice']:
        m = re.search(r'"%s"\s*:\s*("[^"]*"|\{[^{}]*\}|\[[^\]]*\]|true|false|null|\d+(?:\.\d+)?)' % re.escape(key), txt)
        if m:
            try:
                head[key] = json.loads(m.group(1))
            except Exception:
                pass
    # 提取最后一条完整消息
    last_msg = None
    for m in re.finditer(r'"role"\s*:\s*"[^"]*"', txt):
        start = m.start()
        brace = txt.rfind('{', 0, start)
        end = txt.find('}', start)
        if brace >= 0 and end > start:
            cand = txt[brace:end + 1]
            try:
                last_msg = json.loads(cand)
            except Exception:
                pass
    result = {}
    for k in ('model', 'stream', 'max_tokens', 'temperature', 'reasoning', 'tools'):
        if k in head:
            result[k] = head[k]
    if last_msg:
        result['messages'] = [last_msg]
    else:
        result['messages'] = [{'role': 'user', 'content': '(上下文已损坏，请重试或开新对话)'}]
    try:
        return json.dumps(result, ensure_ascii=False).encode('utf-8')
    except Exception:
        return json.dumps({'messages': [{'role': 'user', 'content': '请重试'}]}).encode('utf-8')


def sanitize(raw: bytes) -> bytes:
    """三级修复: UTF-8/转义 -> JSON 校验 -> 智能重建"""
    cleaned = sanitize_utf8_and_escapes(raw)
    try:
        json.loads(cleaned.decode('utf-8', errors='strict'))
        return cleaned
    except Exception:
        pass
    return rebuild_payload(cleaned)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write("%s\n" % (fmt % args))
        except Exception:
            pass

    def _forward(self, body: bytes, headers: dict):
        url = UPSTREAM + self.path
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in headers.items():
            if k.lower() in ('host', 'content-length', 'connection', 'accept-encoding'):
                continue
            req.add_header(k, v)
        req.add_header('Content-Length', str(len(body)))
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            try:
                self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(('{"error":{"message":"upstream error: %s"}}' % str(e)).encode())
            except Exception:
                pass
            return
        # 响应头
        self.send_response(resp.status)
        is_stream = str(headers.get('stream', '')).lower() == 'true'
        for k, v in resp.headers.items():
            if k.lower() in ('transfer-encoding', 'connection'):
                continue
            if is_stream and k.lower() == 'content-length':
                continue  # 流式不转发 Content-Length
            self.send_header(k, v)
        self.end_headers()
        # 响应体
        if is_stream:
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except Exception:
                    break
        else:
            try:
                self.wfile.write(resp.read())
            except Exception:
                pass
        try:
            self.wfile.flush()
        except Exception:
            pass

    def do_GET(self):
        url = UPSTREAM + self.path
        req = urllib.request.Request(url, method='GET')
        for k, v in self.headers.items():
            if k.lower() in ('host', 'content-length', 'connection', 'accept-encoding'):
                continue
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass
            return
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() in ('transfer-encoding', 'connection'):
                continue
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(resp.read())
        except Exception:
            pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b''
        cleaned = sanitize(raw)
        if len(cleaned) != len(raw):
            try:
                with open(LOG_FILE, 'a', encoding='utf-8') as f:
                    f.write("[CLEAN] %d -> %d bytes\n" % (len(raw), len(cleaned)))
            except Exception:
                pass
        hdrs = {k: v for k, v in self.headers.items()}
        self._forward(cleaned, hdrs)

    do_PUT = do_POST


def main():
    global UPSTREAM, LOG_FILE
    parser = argparse.ArgumentParser(description='WorkBuddy JSON Clean Proxy')
    parser.add_argument('--listen', default='127.0.0.1', help='listen address (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8318, help='listen port (default: 8318)')
    parser.add_argument('--upstream', default='http://127.0.0.1:13000', help='upstream OpenAI-compatible endpoint')
    parser.add_argument('--log', default='clean-proxy.log', help='log file path')
    args = parser.parse_args()

    UPSTREAM = args.upstream
    LOG_FILE = args.log

    server = http.server.ThreadingHTTPServer((args.listen, args.port), ProxyHandler)
    server.daemon_threads = True
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write("clean-proxy started on %s:%d -> %s\n" % (args.listen, args.port, UPSTREAM))
    except Exception:
        pass
    sys.stderr.write("clean-proxy listening on %s:%d -> %s\n" % (args.listen, args.port, UPSTREAM))
    server.serve_forever()


if __name__ == '__main__':
    main()
