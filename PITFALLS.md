# 踩坑记录

做这个项目时踩到的坑。按「现象 → 根因 → 解法」写，方便以后直接查。

时间：2026-10-21。环境：Windows 11 / Python 3.13 / Node 24。

---

## 1. 中文全是乱码，但英文完全正常

**现象**

```python
search('北京 供暖时间')
# title: 'ä¾\x9bæ\x9a\x96æ\x9c\x8då\x8a¡_å\x8c\x97äº¬å¸...'
```

要 `latin-1 → utf-8` 转一次才对，说明是编码问题不是显示问题——落盘再读回来还是乱码。

**根因**

Exa 的 MCP 响应头是 `content-type: text/event-stream`，**不带 charset**。

`requests` 对没有 charset 的 `text/*` 会默认填 `ISO-8859-1`。所以这段代码：

```python
encoding = response.encoding or "utf-8"
```

`response.encoding` 是 `'ISO-8859-1'`（非空），`or "utf-8"` **永远不会触发**。

**解法**

显式否决这个假默认：

```python
charset_match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
charset = charset_match.group(1) if charset_match else None

if charset and charset.lower() not in {"iso-8859-1", "ascii", "us-ascii"}:
    encoding = charset
else:
    guessed = getattr(response, "apparent_encoding", None)
    encoding = guessed if guessed and guessed.lower() not in {"iso-8859-1", "ascii", "us-ascii"} else "utf-8"
```

**教训**

纯英文测试**永远发现不了**这个问题。任何文本抓取项目，测试用例里必须有中文。

---

## 2. DuckDuckGo 三层坑，缺一层就全挂

这条最费时间，因为三层是叠加的，修好一层才会暴露下一层。

### 第 1 层：直连超时

```
curl https://html.duckduckgo.com/html/?q=test
→ HTTP 000，15 秒超时
```

国内直连不通。要走代理。

### 第 2 层：走了代理还是验证码

```
代理 + UA "Mozilla/5.0 (compatible; free-search/1.0)"
→ HTTP 202，页面标题 "DuckDuckGo"，正文含 "select all squares"
```

**根因：** DDG 会拦短 UA / 机器人 UA。

**解法：** 换完整浏览器 UA。

```python
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
```

验证过：同样的代理，换 UA 后 202 → 200。

### 第 3 层：HTTP 200 但解析出 0 条

代理通了、UA 也对了，返回 200、32 KB、10 个 `result__a`，**但解析结果是空的**。

**根因：** DDG 的结果链接长这样：

```html
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftokio.rs%2F&amp;rut=...">
```

两个特征：

1. **协议相对**（`//` 开头，没有 scheme）
2. **重定向包装**（真实地址在 `uddg` 参数里，URL 编码）

而解析器里写的是：

```python
if href.startswith(("https://", "http://")):
```

`//duckduckgo.com/...` 不匹配 → `url` 字段为空 → 结果被丢弃 → 0 条。

**解法**

```python
def _unwrap_result_href(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target and target[0]:
            return unquote(target[0])
    return href
```

**教训**

「HTTP 200 + 页面有内容」不等于「解析成功」。**必须检查解析出来的条数**，不能只看状态码。

这也是为什么本项目把"200 但 0 条"定义成失败并继续降级。

---

## 3. requests 读不到 Windows 系统代理

**现象**

系统代理开着（注册表 `ProxyEnable=1`，`ProxyServer=127.0.0.1:10809`），但：

```python
requests.get("https://html.duckduckgo.com/...")   # 超时
```

**根因**

| 方式 | 读注册表吗 |
|---|---|
| `urllib.request.getproxies()` | ✅ |
| `requests`（`trust_env=True`） | ❌ 只读环境变量 |

`requests` 的 `trust_env` 指的是**环境变量**（`HTTP_PROXY` 等），不是操作系统设置。

**解法**

自己探测，再显式传：

```python
def _detect_proxies():
    explicit = os.environ.get("FREE_SEARCH_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if explicit:
        return {"http": explicit, "https": explicit}
    detected = getproxies()          # 这个才读注册表
    proxy = detected.get("https") or detected.get("http")
    return {"http": proxy, "https": proxy} if proxy else None

requests.request(..., proxies=_proxies_for_request())
```

`getproxies()` 跨平台：Windows 读注册表、macOS 读系统设置、Linux 读环境变量。

**教训**

"我设了系统代理"和"Python 用了系统代理"是两件事。

---

## 4. Bing 的空查询返回 10 条垃圾结果

**现象**

用一个肯定搜不到的查询（`asdfqwerzxcv12345qwertyuiop`），Bing 返回的页面里：

- **没有**"没有找到相关结果"的提示
- **有** 10 张 `.b_algo` 卡片
- 每张卡片**都有** `<cite>`
- 内容全是无关推荐（百度知道问答、Apple Music 热门等）

**根因**

Bing 的"兜底推荐"用的也是标准结果卡片结构，所以常规的"有没有卡片""有没有 cite"检查全部失效。

**解法（保守做法）**

三层过滤：

1. 页面级空结果提示（"没有找到" / "did not match any documents" 等）
2. 无 `<cite>` 的卡片丢弃
3. **满 10 张卡片且标题+摘要全都不含查询词 → 判为 empty**

第 3 条加了个限定：只在**满 10 张**时生效，短的正常结果集（2–9 条）不受影响。

**教训**

"有结果"和"有相关结果"是两件事。搜索引擎的兜底推荐会污染数据通道。

**已知风险**

真实查询如果 10 条结果的标题和摘要恰好都不含查询词，会被误判为空。这是有意的取舍——宁可偶尔漏，不可把垃圾当结果喂给模型。

---

## 5. 健康检查通过 ≠ 端点可用

**现象**

```python
# broker 的 /healthz
GET http://127.0.0.1:9999/healthz  → 200 OK

# 真正要用的端点
POST http://127.0.0.1:9999/v1/tools/search  → 404
```

**根因**

代理程序（broker）把 `/v1/tools/search` 误判成了 Anthropic 协议请求，拼上了 `/anthropic` 前缀：

```
实际请求: api.deepseek.com/anthropic/v1/tools/search  → 404
应该是:   api.deepseek.com/v1/tools/search            → 200
```

而 `/healthz` 是 broker 自己处理的，不经过路由逻辑，所以永远是 200。

**解法**

健康检查和真实请求要分别验：

```python
health_url = "http://127.0.0.1:9999/healthz"
try:
    _http_request("GET", health_url, timeout=5.0)
except _BackendFailure:
    _fail("broker_down", "health check failed")
# 然后还得真的发一次搜索请求，看它能不能通
```

**教训**

**健康检查只能证明"进程活着"，不能证明"这个功能可用"。** 端到端测试不能省。

---

## 6. 中文结果里的 U+2028 会骗过 SSE 解析器

**现象**

Exa 返回的中文搜索结果解析失败，报 `parse_error`。

**根因**

MCP 的 SSE 响应按行拆分。Python 的 `str.splitlines()` 会把 **U+2028（LINE SEPARATOR）** 也当成换行符：

```python
"标题一 标题二".splitlines()   # ['标题一', '标题二']  ← 被拆了
```

而 U+2028 在 JSON 字符串里是合法字符，不是换行。结果一条完整的 `data: {...}` 被劈成两半，JSON 解析失败。

**解法**

只按 CR/LF 拆，不用 `splitlines()`：

```python
def _parse_sse_json(body: str):
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line.startswith("data:"):
            continue
        ...
```

**教训**

处理网络协议时不要用"智能"的字符串方法。`splitlines()` 很聪明，聪明到你不想它聪明的地方。

---

## 7. `max_results=0` 被静默改成 1

**现象**

```python
search("query", max_results=0)   # 返回 1 条结果，error=None
```

**根因**

```python
limit = max(1, min(MAX_SOURCES, int(max_results)))   # 0 → 1
```

调用了方传 0，可能期待"不要结果"或"应该报错"，结果静默拿到 1 条。

**解法**

钳制之前先校验：

```python
try:
    requested = int(max_results)
except (TypeError, ValueError):
    result["error"] = {"code": "other", "message": "max_results must be an integer"}
    return result
if requested <= 0:
    result["error"] = {"code": "other", "message": f"max_results must be positive, got {requested}"}
    return result
```

超过上限（10）仍然钳制，但标记 `truncated=True`，让调用方知道被截断了。

**教训**

静默修正参数比报错更危险——调用方会以为参数生效了。

---

## 总结：这类项目最容易漏的三件事

1. **空结果算不算失败？** 必须算。只看状态码会把"被拦截"和"没找到"混为一谈。
2. **中文测试了吗？** 纯英文测试能掩盖编码问题。
3. **端到端测了吗？** 健康检查通过不代表功能可用。

对应到代码里的三条硬规则：

- 空结果、解析 0 条、验证码页，**全部算失败并继续降级**
- 测试用例必须含中文
- 每个后端都要有真实请求的冒烟测试，不能只 mock
