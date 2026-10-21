# 实测报告

**日期：** 2026-10-21  
**目录：** `G:\code\free-search\`

## 环境

- Node.js `v24.15.0`，Python `3.13.13`，满足最低版本。
- G 盘剩余约 `61.5 GB`。
- Exa MCP 可连接；initialize 与 tools/call 都收到 HTTP 200。
- SerpAPI key 从本机配置读取成功；报告不记录 key。初始余额查询为 `238`。

## 免费后端实测

每个后端按任务书的 5 条查询顺序串行请求：2 条中文、2 条英文、1 条长尾。

| 后端 | 成功数 | 结果数（5 条查询） | 结论 |
|---|---:|---|---|
| Exa MCP | 5/5 | 10、10、10、10、10 | 可用。中文 SSE 解析修复后全部返回结果。 |
| Bing HTML | 5/5 | 10、10、10、7、10 | 纯 HTTP 可用；按 `.b_algo` 卡片解析。这里只记录非空结果数，不评估语义相关性。 |
| DDG HTML | 0/5 | 0、0、0、0、0 | 五次均为人工挑战页；可见提示要求选择含鸭子的方块。均以 `captcha` 失败并继续降级。 |

默认冒烟脚本另外跑了 3 条查询：Exa `3/3`、Bing `3/3`、DDG `0/3`，总计 `6/9` 有来源，`9/9` 都返回规范的成功或错误结构。挑战页是任务书已知限制，不会被误报为空搜索成功。

## 付费兜底

### SerpAPI

- 任务开始时余额为 `238`。本次发出 4 次搜索请求：3 条单后端查询和 1 次强制前置免费后端失败的降级链；四次都得到 8–9 条结果。
- 结束时余额查询为 `235`，净减少 `3`。第四次复用了前面测试过的查询，可能命中了服务端缓存；这是对余额差异的推测，实际 HTTP 搜索请求数仍是 4。
- 使用的是 Free Plan 额度，本次人民币费用为 `¥0.00`。返回结构将 SerpAPI 标成 `paid: true`，并以 `cost_cny: 0.0` 表示没有人民币扣费。

### DeepSeek broker

- `/healthz` 返回 HTTP 200，响应标识服务为 `credential-broker`。
- 任务要求的 3 条搜索请求，以及 2 次用于核对 `count=5/10` 的请求，都在 `/v1/tools/search` 收到 HTTP 404；没有一次进入搜索处理并返回结果。根路径 `/` 和 `/healthz` 正常，但该搜索路由不存在。
- 因此 DeepSeek 的真实成功验收未完成。按 404 表示路由未受理来估算，本次搜索费用为 `¥0.00`；该 broker 没有可用的账单查询接口，无法独立核账。
- 离线模拟通过：余额为 0 时记录 `quota_exhausted` 并转到 DeepSeek；模拟成功响应返回 `paid: true`、`cost_cny: 0.005`，并在缺少 `snippet` 时使用 `summary`；broker 健康检查失败返回 `broker_down`。实际 `/v1/tools/search` 的 404 则保留为 `http_4xx`，不计成功或模拟费用。

## DSH 插件选择与验证

选择**方案 A：TypeScript 重写**，插件不依赖 Python。DSH provider 按 Exa → Bing → DDG → 可选 SerpAPI 的顺序处理空结果、HTTP 错误和挑战页；SerpAPI 使用余额预检及 10 分钟缓存。DSH 契约没有 `paid` / `cost_cny` 字段，因此插件在 `content` 中提供这两项元数据。插件按该契约实现到 SerpAPI，不调用 DeepSeek broker。

- `npm run build` / `tsc -p tsconfig.json` 通过。
- `FreeSearchProvider` 可 import、构造，`available()` 返回 `true`。
- `provider.search()` live smoke 成功，返回 Exa 来源。
- Python 语法编译、CLI 人类可读 / JSON / 指定后端失败退出码均通过。
- `settings.fragment.json` 可解析为合法 JSON。

## 最大的问题

Exa 中文结果含 Unicode 行分隔符 U+2028。Python `splitlines()` 会把它误当 SSE 换行，令有效响应变成 JSON 解析失败。解析器已改为只按 CR/LF 拆分 SSE 行，并重跑 5 条后达到 `5/5`。另一个环境阻塞项是本机 broker 虽健康，但没有任务书要求的搜索路由。

## 仓库与体积

- `git status --short -- free-search` 显示新增的 `free-search/` 目录；未在该目录外写入项目文件。仓库全局状态还有 `G:\code` 下其他路径的现有改动，本次没有触及。
- 目录总大小约 `23.53 MB`，小于 50 MB；其中约 23.3 MB 是本地 TypeScript/DSH 编译依赖。清理 npm 探测包与 Python 字节码缓存时，环境策略拒绝了删除命令，因此小型 `work/` 和 `__pycache__/` 临时文件仍在目录内。