---
name: free-search
description: 免费优先的网页搜索，走 Exa 公共 MCP / Bing / DuckDuckGo 降级链，前三个都失败才用有额度的 SerpAPI，最后用本地 DeepSeek broker 兜底。需要联网查资料、核实事实、找最新信息时用这个，不要用内置 WebSearch。
---

# 免费搜索

用一条命令行替代内置 WebSearch。返回真实的搜索结果（title / url / snippet），不经过模型二次总结。

## 什么时候用

- 需要查资料、核实事实、找最新信息
- 需要多个来源交叉验证
- 内置 WebSearch 已被 deny 掉时（见「安装」）

## 怎么用

```powershell
python G:\code\free-search\cli\search.py "<查询>"
```

要结构化数据：

```powershell
python G:\code\free-search\cli\search.py "<查询>" --json
```

### 参数

| 参数 | 用途 |
|---|---|
| `--json` | 输出完整结构，含 `backend` / `paid` / `cost_cny` / `attempts` |
| `--backend <id>` | 只跑指定后端（调试用） |
| `--proxy <url>` | 临时指定代理，如 `http://127.0.0.1:10809` |

### 默认降级顺序

```
exa-mcp   →  bing-html  →  ddg-html  →  serpapi(有额度才用)  →  deepseek(¥0.005/次)
   免费          免费          免费            250次/月              最终兜底
```

**免费后端能成功就不会碰付费的。** 结果里的 `paid` 和 `cost_cny` 告诉你这次花没花钱。

## 结果怎么读

```json
{
  "backend": "exa-mcp",
  "sources": [{"title": "...", "url": "...", "snippet": "..."}],
  "paid": false,
  "cost_cny": 0.0,
  "attempts": [{"backend": "exa-mcp", "ok": true, "ms": 1800}],
  "error": null,
  "truncated": false
}
```

**关键：`sources` 为空 = 搜索失败，不是「网上没有这个信息」。** 一定要看 `error` 和 `attempts`。

## 失败了怎么办

先看 `attempts` 里每个后端的 `failure_reason`：

| 失败原因 | 说明 | 怎么办 |
|---|---|---|
| `captcha` | 被反爬拦截 | 换查询措辞，或给 `--proxy` |
| `timeout` / `network` | 连不上 | **多半是网络问题，见下** |
| `empty` | 返回 200 但没结果 | 换更具体或更宽泛的查询 |
| `quota_exhausted` | SerpAPI 额度用完，已跳过 | 正常，链会继续往下走 |
| `broker_down` | DeepSeek 兜底时 broker 不通 | 前面几个应该已经成功了，忽略 |

### 网络问题（最常见的坑）

**DuckDuckGo 在国内直连必然超时。** 如果 `ddg-html` 报 `timeout` 或 `network`：

1. **先确认是不是代理没开**——很多机器系统代理没开但人以为开了。
2. 问用户代理端口，然后带上参数重试：

```powershell
python G:\code\free-search\cli\search.py "<查询>" --proxy http://127.0.0.1:<端口>
```

3. 如果用户说没有代理，**就接受 ddg-html 不可用**——链会自动降级到下一个后端，不影响整体。

> 脚本会自动读系统代理（Windows 注册表 / macOS 系统设置 / 环境变量 `HTTP_PROXY`）。读得到就不用管；读不到才需要 `--proxy`。

## 不要做的事

- **不要用内置 WebSearch**（已通过 settings 禁用）。它会把搜索请求发到上游模型服务并按其计费。
- **不要并发调用**。后端有速率限制，串行更稳。
- **不要因为 `sources` 为空就说「没找到相关信息」**——那是搜索失败，换个查询或说明失败原因。
- **不要把 SerpAPI / DeepSeek 当默认**。它们是兜底，免费后端能出结果就不该落到它们。

## 安装

1. 把整个 `free-search/` 目录拷到 `~/.claude/skills/free-search/`。
2. 把 `settings.fragment.json` 合并进 `~/.claude/settings.json`：

   ```json
   { "permissions": { "deny": ["WebSearch"] } }
   ```

3. 重启 Claude Code 让配置生效。

**为什么不自动关？** Claude Code 的 skill 只能教模型怎么用工具，**不能在运行时禁用 harness 提供的内置工具**。内置 WebSearch 的开关在 `settings.json`，skill 改不了它。所以必须手动合并配置——这不是偷懒，是架构限制。

**WebFetch 不用关**：它是客户端抓取，不走上游按次计费。

## 依赖

- Python 3.9+
- `requests`（可选，没有就退回 `urllib`）
- 无 API key 必需（SerpAPI / DeepSeek 只在兜底时用到）
