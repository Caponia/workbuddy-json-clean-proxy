# -*- coding: utf-8 -*-
"""
WorkBuddy -> 127.0.0.1:8318 (清洗代理) -> 10.66.66.1:13000 (New API via WireGuard)

修复 WorkBuddy 长会话历史上下文损坏（抓包实锤，图片历史尤甚）：
1. 截断的多字节 UTF-8 字符 (invalid UTF-8 -> U+FFFD)
2. 非法 JSON 转义 (如 \w 等非法 escape)
3. 深度结构损坏 (字符串边界错乱) -> 智能重建: 保留头部字段 + 最后一条完整消息 + 尾部字段

流式(SSE)响应逐行透传，非流式完整转发。
"""
import http.server
import urllib.request
import urllib.error
import json
import sys
import re
import io

UPSTREAM = "http://10.66.66.1:13000"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8318
LOG_FILE = r"C:\Users\admin\.wireguard\clean-proxy.log"

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


def sanitize(raw: bytes) -> bytes:
    """三级修复：UTF-8/转义 -> JSON 校验 -> 智能重建"""
    cleaned = sanitize_utf8_and_escapes(raw)
    # 尝试直接解析
    try:
        json.loads(cleaned.decode('utf-8', errors='strict'))
        return cleaned
    except Exception:
        pass
    # 深度损坏，智能重建
    return rebuild_payload(cleaned)


def rebuild_payload(raw: bytes) -> bytes:
    """深度损坏时重建：保留头部字段 + 最后一条完整消息 + 尾部字段"""
    txt = raw.decode('utf-8', errors='replace')
    # 提取头部字段（model, stream, max_tokens 等）
    head = {}
    tail = {}
    # 尝试提取顶层字段
    for key in ['model', 'stream', 'max_tokens', 'temperature', 'reasoning', 'tools', 'tool_choice']:
        m = re.search(r'"%s"\s*:\s*("[^"]*"|\{[^{}]*\}|\[[^\]]*\]|true|false|null|\d+(?:\.\d+)?)' % re.escape(key), txt)
        if m:
            try:
                head[key] = json.loads(m.group(1))
            except Exception:
                pass
    # 提取 messages 数组的最后一条完整消息（找最后一个 "role" 到最近的 "}"）
    last_msg = None
    for m in re.finditer(r'"role"\s*:\s*"[^"]*"', txt):
        start = m.start()
        # 向前找 '{'
        brace = txt.rfind('{', 0, start)
        # 向后找匹配的 '}'（简单方式：找下一个独立 '}'）
        end = txt.find('}', start)
        if brace >= 0 and end > start:
            cand = txt[brace:end + 1]
            try:
                last_msg = json.loads(cand)
            except Exception:
                pass
    # 组装
    result = {}
    if 'model' in head:
        result['model'] = head['model']
    if 'stream' in head:
        result['stream'] = head['stream']
    if 'max_tokens' in head:
        result['max_tokens'] = head['max_tokens']
    if 'temperature' in head:
        result['temperature'] = head['temperature']
    # 消息
    if last_msg:
        result['messages'] = [last_msg]
    else:
        result['messages'] = [{'role': 'user', 'content': '(上下文已损坏，请重试或开新对话)'}]
    if 'reasoning' in head:
        result['reasoning'] = head['reasoning']
    if 'tools' in head:
        result['tools'] = head['tools']
    try:
        return json.dumps(result, ensure_ascii=False).encode('utf-8')
    except Exception:
        return json.dumps({'model': 'gpt-5.6-luna', 'messages': [{'role': 'user', 'content': '请重试'}]}).encode('utf-8')


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
        # 关键: 禁用系统代理环境变量(HTTP_PROXY/HTTPS_PROXY), 强制直连上游。
        # 否则 urllib 会把 10.66.66.1 等私网地址交给 127.0.0.1:7890 代理转发 -> 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in headers.items():
            if k.lower() in ('host', 'content-length', 'connection', 'accept-encoding'):
                continue
            req.add_header(k, v)
        req.add_header('Content-Length', str(len(body)))
        try:
            resp = opener.open(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            # 上游不可达时返回 502
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
                continue  # 流式不转发 Content-Length（长度未知）
            self.send_header(k, v)
        self.end_headers()
        # 响应体
        if is_stream:
            # SSE 逐行透传
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
            data = resp.read()
            try:
                self.wfile.write(data)
            except Exception:
                pass
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _is_stream(self, headers):
        return str(headers.get('stream', '')).lower() == 'true'

    def do_GET(self):
        url = UPSTREAM + self.path
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, method='GET')
        for k, v in self.headers.items():
            if k.lower() in ('host', 'content-length', 'connection', 'accept-encoding'):
                continue
            req.add_header(k, v)
        try:
            resp = opener.open(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
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
        # 清洗
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
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write("clean-proxy started on %s:%d -> %s\n" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM))
    except Exception:
        pass
    server.serve_forever()


if __name__ == '__main__':
    main()
