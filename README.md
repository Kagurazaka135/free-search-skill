# free-search（Claude Code Skill）

给 Claude Code 的免费优先网页搜索。走一条自动降级的后端链，不需要任何 API key 就能用。

替代内置 `WebSearch` —— 后者会把搜索请求发到上游模型服务，按次计费（DeepSeek 从 2026-11-01 起对 Harness/Claude Code/Codex 的 server 端搜索调用收费）。

## 它解决什么问题

内置 `WebSearch` 的问题：

- 搜索由**上游服务端**执行，你无法控制它用哪家引擎、也看不到它怎么失败
- 从 2026-11-01 起按次计费（DeepSeek ¥5/1000 次）
- 失败时你分不清"没搜到"和"搜索坏了"

这个 skill 的做法：

- 五个后端按**免费 → 付费**顺序降级，免费能出结果就绝不碰付费的
- 每次返回都带 `backend` / `paid` / `cost_cny`，花钱了你会知道
- **空结果 = 失败**，会继续降级，不会伪装成"没找到"

## 降级链

```
exa-mcp   →  bing-html  →  ddg-html  →  serpapi(有额度才用)  →  deepseek(¥0.005/次)
   免费          免费          免费            250次/月              最终兜底
```

| 后端 | 需要 key | 成本 | 备注 |
|---|---|---|---|
| `exa-mcp` | 否 | 免费 | Exa 公共 MCP 端点，无需注册 |
| `bing-html` | 否 | 免费 | 解析 Bing 结果页 |
| `ddg-html` | 否 | 免费 | **国内需代理**，且必须用浏览器 UA |
| `serpapi` | 是 | 250 次/月 | 调前先查余额，用完自动跳过 |
| `deepseek` | 走本地 broker | ¥0.005/次 | 前四个全挂才用 |

## 安装

```bash
# 1. 拷到 Claude Code 的 skills 目录
mkdir -p ~/.claude/skills/free-search
cp -r core cli skill/SKILL.md skill/settings.fragment.json ~/.claude/skills/free-search/

# 2. 关掉内置 WebSearch（必须手动，skill 自己关不掉）
#    把 settings.fragment.json 的内容合并进 ~/.claude/settings.json

# 3. 重启 Claude Code
```

> Windows 上 `~` 是 `C:\Users\<用户名>`。

`settings.fragment.json` 就三行：

```json
{ "permissions": { "deny": ["WebSearch"] } }
```

**为什么不自动关？** Claude Code 的 skill 只能教模型怎么用工具，**不能在运行时禁用 harness 提供的内置工具**。这是架构限制，不是偷懒。

**`WebFetch` 不用关** —— 它是客户端抓取，不走上游按次计费。

## 用法

```bash
python cli/search.py "rust tokio"
python cli/search.py "北京 供暖时间" --json
python cli/search.py "query" --backend ddg-html      # 只跑指定后端
python cli/search.py "query" --proxy http://127.0.0.1:10809
```

### 输出

```
[exa-mcp] rust tokio (10 条)
1. Tokio - An asynchronous Rust runtime
   https://tokio.rs/
   Tokio is a runtime for writing reliable asynchronous applications...
```

`--json` 给完整结构：

```json
{
  "query": "rust tokio",
  "backend": "exa-mcp",
  "sources": [{"title": "...", "url": "...", "snippet": "..."}],
  "paid": false,
  "cost_cny": 0.0,
  "attempts": [{"backend": "exa-mcp", "ok": true, "ms": 1800}],
  "error": null,
  "truncated": false
}
```

## 失败判定

**不看 HTTP 状态码，看有没有真的拿到结果。** 以下都算失败并继续降级：

| 情况 | 判定 |
|---|---|
| 超时 / 连接错误 | `timeout` / `network` |
| HTTP 4xx / 5xx | `http_4xx` / `http_5xx` |
| **HTTP 200 但结果为空** | `empty` |
| **HTTP 200 但解析出 0 条** | `parse_error` |
| 返回验证码页 | `captcha` |
| SerpAPI 余额不足（主动跳过） | `quota_exhausted` |
| DeepSeek 兜底时 broker 不通 | `broker_down` |

## 已知限制

- **DuckDuckGo 在国内直连必然超时**。脚本会自动读系统代理（Windows 注册表 / macOS 系统设置 / `HTTP_PROXY`），读不到时需要 `--proxy`。
- **DDG 会拦截爬虫 UA**。代码里用的是完整 Chrome UA，不要换成短 UA。
- **Bing 空查询会返回"兜底推荐卡"**。代码会识别并判为 empty，但这条规则是保守的——如果真实查询的 10 条结果标题和摘要都不含查询词，可能被误判为空。
- **SerpAPI 和 DeepSeek 是付费的**，只在前面全挂时启用。

## 测试

```bash
python tests/smoke.py
```

跑模拟用例（编码、参数校验、Bing 过滤、DDG 解码）+ 实时查询。全过退出码 0。

## 结构

```
.
├── core/search_core.py    # 核心：五个后端 + 降级路由，零第三方依赖
├── cli/search.py          # 命令行封装
├── skill/                 # Claude Code skill 本体
│   ├── SKILL.md
│   └── settings.fragment.json
├── tests/smoke.py
└── PITFALLS.md            # 踩坑记录：编码、DDG 三层坑、代理、Bing 垃圾结果…
```

## 踩过的坑

`PITFALLS.md` 记了 7 个，都是"现象 → 根因 → 解法"的形式。几个值得先看的：

- **中文全乱码但英文正常** —— `requests` 对无 charset 的响应默认填 `ISO-8859-1`
- **DuckDuckGo 三层坑** —— 代理 / UA / 链接解包，缺一层就全挂
- **`requests` 读不到 Windows 系统代理** —— 它只读环境变量，`urllib` 才读注册表
- **健康检查通过 ≠ 端点可用** —— `/healthz` 200 但真正的端点 404

## 依赖

- Python 3.9+
- `requests`（可选，没有就退回 `urllib`）
- **不需要任何 API key**

## 许可

MIT
