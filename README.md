# WorkBuddy JSON Clean Proxy

**修复 WorkBuddy / CodeBuddy（Claude Code 同架构）长会话历史上下文损坏导致的 `400 invalid JSON request body` / `400 JSON parsing failed` 报错。**

一个零依赖（仅 Python 3 标准库）的本地 HTTP 清洗代理：客户端 → `127.0.0.1:8318`（本代理）→ 你的 LLM 网关（OpenAI 兼容端点），在链路中间自动修复请求体损坏。

---

## 问题现象

在 WorkBuddy / CodeBuddy 中配置自定义模型（经 LLM 网关/代理转发）后，**长会话或含大工具输出的场景**下，发消息偶发报错：

```
Error Code: 0 (或 3002)
Message: 自定义模型 xxx 错误，请切换模型或重试
Server Detail: "Invalid request: invalid JSON request body"
（上游网关侧则表现为 "400 JSON parsing failed"）
```

**特征规律**（多用户复现确认）：
- 同一个会话内，**第一次请求成功、第二次失败**，之后反复失败
- 换模型、换网关不解决（损坏发生在客户端请求体本身）
- **直连官方 API（如 DeepSeek）可能"看起来正常"**——因为官方 Go 实现容忍无效 UTF-8（替换为 U+FFFD），而严格解析的网关直接 400

## 根因分析（抓包实锤）

对失败请求体做逐字节分析，WorkBuddy 客户端在**序列化/压缩对话历史**时产生两类损坏：

### 1. 截断的多字节 UTF-8

中文字符被切半，产生无效字节序列（`invalid continuation byte`）：

```
...包 含 公 司 名 [缺失字节!] 基 础 设 施...
```

### 2. 非法 JSON 转义

反斜杠后的字符被错误替换，产生非标准转义（JSON 只允许 `\" \\ \/ \b \f \n \r \t \uXXXX`）：

```
...only your final reply.\\o understand the outcome...
    （应为 \n，实际为 \o）
```

### 3. 深度结构损坏（偶发）

字符串边界错乱、引号失衡，整体 JSON 无法解析。

### 为什么官方直连"正常"？

对照实验（同样的坏请求体）：

| 损坏类型 | DeepSeek 官方 API | 严格网关（New API / OpenRouter） |
|---|---|---|
| 截断 UTF-8 | ✅ 200 容忍（替换为 �） | ✅ 实际也容忍 |
| 非法转义 `\w` | ❌ 400 拒绝 | ❌ 400 拒绝（`invalid JSON request body`） |
| 深度结构损坏 | ❌ 拒绝 | ❌ 拒绝 |

结论：**根因是客户端的序列化 bug，与网关无关**。DeepSeek 直连"正常"只是因为其请求体恰好未触发转义类损坏（历史短、反斜杠少）；一旦历史混入工具输出（`\\r\\n`、路径等反斜杠文本），任何网关都会拒绝。

### 同类已知问题（社区）

- [Byte-Slicing a Claude Agent's Context Payload Poisoned Every Retry](https://jonroosevelt.com/blog/truncating-json-by-byte-slicing-creates-permanent-errors-par/) —— 按字节切片压缩上下文导致 JSON 损坏，且损坏被持久化、每次重试都失败
- [Claude Code issue #76664](https://claudeissues.com/issue/76664-api-400-request-body-is-not-valid-json-truncated-mid-stream-after-large-tool-out) —— 大工具输出后请求体截断，会话不可恢复
- [Claude Code issue #23390](https://github.com/anthropics/claude-code/issues/23390) —— 上下文压缩后永久 400，只能新开会话

## 解决方案：三级修复

本代理对请求体做三级修复，全部失败才降级重建：

### 第 1 级：UTF-8 + 非法转义修复（逐字节扫描）

- 无效 UTF-8 字节 → 替换为 U+FFFD
- 非法转义（`\w` `\o` 等）→ 将反斜杠转义为 `\\`，后续字符作为普通字符

### 第 2 级：整体 JSON 校验

修复后尝试 `json.loads`：
- 成功 → 直接转发（绝大多数情况）
- 失败 → 进入第 3 级

### 第 3 级：深度损坏智能重建

提取顶层字段（`model` / `stream` / `max_tokens` 等）+ **最后一条完整消息**，重建合法 JSON。保证请求可送达（虽丢失部分早期历史，但保住当前轮次）。

### 响应侧

- **流式（SSE）**：逐行透传，不做缓冲
- **非流式**：完整转发
- 上游不可达 → 返回 502 JSON 错误（不挂死）

## 部署

### 1. 启动代理

```bash
python clean_proxy.py --upstream http://127.0.0.1:13000
# 可选参数:
#   --listen 127.0.0.1    监听地址 (默认 127.0.0.1)
#   --port 8318           监听端口 (默认 8318)
#   --upstream URL        上游 OpenAI 兼容端点 (默认 http://127.0.0.1:13000)
#   --log clean-proxy.log 日志文件路径
```

Windows 静默后台运行（无控制台窗口）：

```powershell
pythonw clean_proxy.py --upstream http://127.0.0.1:13000
```

### 2. 配置 WorkBuddy 自定义模型

编辑 `~/.workbuddy/models.json`，把自定义模型的 `url` 指向代理：

```json
[
  {
    "id": "my-model",
    "name": "My Model (via clean proxy)",
    "vendor": "Custom",
    "url": "http://127.0.0.1:8318/v1/chat/completions",
    "apiKey": "sk-xxx"
  }
]
```

> 注意：`127.0.0.1` 在 NO_PROXY 中，请求不会走系统代理，直连本代理。

### 3. 开机自启（Windows）

注册表 Run 键（无需管理员）：

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
cpa-clean-proxy = "C:\path\to\pythonw.exe" "C:\path\to\clean_proxy.py" --upstream http://127.0.0.1:13000
```

### 4. 验证

```bash
curl -X POST http://127.0.0.1:8318/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxx" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hi"}]}'
```

## 验证结果（真实环境）

用**实际抓包捕获的 173KB 损坏请求体**做对照测试：

| 路径 | 结果 |
|---|---|
| 直连网关 | ❌ 400 `invalid JSON request body`（复现用户报错） |
| 经清洗代理 | ✅ 200，SSE 流式正常输出 |

连续 5 轮对话压力测试：全部 200。

## 常见问题

**Q: 为什么 DeepSeek 官方直连正常，经网关就报错？**
A: 官方 Go 实现容忍无效 UTF-8；且直连时历史短、反斜杠少，未触发转义损坏。损坏在客户端请求体里，与网关无关。

**Q: 代理会不会影响性能？**
A: 流式逐行透传，无缓冲延迟；常驻内存约 40MB；单请求线程隔离，卡死不影响其他请求。

**Q: 深度重建会丢历史吗？**
A: 仅在第 1、2 级都失败时触发，且保留最后一条完整消息。正常情况下（占绝大多数）第 1 级修复后 JSON 即合法，不丢任何内容。

## 许可证

MIT License
