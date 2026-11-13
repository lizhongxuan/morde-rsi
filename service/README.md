# 服务（service/）

morde-rsi 的 HTTP 服务：把本地 RSI 循环（`rsi/controller.py`）包成一个
HTTP 服务。外部调用方提交任务，服务在后台跑循环、写证据、落收据，
ADOPT 时只产分支、永不自动合并；需要开 PR 时由 `service/github_pr.py`
生成 PR 正文并创建 PR。

> 范围说明：这是单租户本地服务。**多租户账号 / 计费不在范围**，
> 不要把它当作多用户 SaaS 来用。

## 快速开始

```bash
cd ~/workspace/morde-rsi

# 1. 配置文件（示例复制一份再改；密钥不要写进文件，见下）
cp service/config.example.json service/config.json

# 2. 密钥只走环境变量
export RSI_API_TOKEN='改成一个长随机串'
export DEEPSEEK_API_KEY='你的 DeepSeek API key'

# 3. 校验配置
python3 service.py check-config

# 4. 启动（默认只监听 127.0.0.1:8765）
python3 service.py serve
```

## curl 示例

以下示例假设服务跑在本地 `127.0.0.1:8765`，`$TOKEN` 是 `RSI_API_TOKEN`。

```bash
# 健康检查（公开，不需要 token）
curl http://127.0.0.1:8765/health

# 提交一个任务（202 + job_id）
curl -X POST http://127.0.0.1:8765/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain": "morde", "task_id": "CAND-002"}'

# 查所有任务 / 查单个任务
curl http://127.0.0.1:8765/v1/jobs -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8765/v1/jobs/<job_id> -H "Authorization: Bearer $TOKEN"

# 取任务的证据（验证日志、reward.json）
curl http://127.0.0.1:8765/v1/jobs/<job_id>/evidence \
  -H "Authorization: Bearer $TOKEN"

# 对任务给反馈（kind 见 rsi/feedback.py；human_label 须配 label）
curl -X POST http://127.0.0.1:8765/v1/jobs/<job_id>/feedback \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"kind": "human_label", "label": "wrong", "note": "修完还是显示已发送"}'

# 取消一个排队中 / 运行中的任务
curl -X POST http://127.0.0.1:8765/v1/jobs/<job_id>/cancel \
  -H "Authorization: Bearer $TOKEN"

# 查某个 domain 的当前 frontier（已采纳基线）
curl "http://127.0.0.1:8765/v1/frontier?domain=morde" \
  -H "Authorization: Bearer $TOKEN"
```

## 配置参数（service/config.json）

复制 `service/config.example.json` 后改。**密钥永远不要写进文件**，
只走环境变量。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址。默认只本机可达；改成 `0.0.0.0` 前先想清楚暴露面 |
| `port` | `8765` | 监听端口 |
| `data_dir` | `runs/service` | 任务、收据、证据的落盘目录（相对仓库根） |
| `api_token` | —（无默认值） | Bearer token；也可由环境变量 `RSI_API_TOKEN` 提供，环境变量优先 |
| `fixer.provider` | `openai-compatible` | Fixer 模型协议，目前只支持 OpenAI 兼容接口 |
| `fixer.base_url` | `https://api.deepseek.com/v1` | 模型 API 地址 |
| `fixer.model` | `deepseek-chat` | 模型名 |
| `fixer.api_key_env` | `DEEPSEEK_API_KEY` | 模型 API key 所在的环境变量名（key 本身不在文件里） |
| `fixer.timeout_s` | `120` | 单次模型调用超时（秒） |
| `fixer.max_tokens` | `8192` | 单次模型调用最大输出 tokens |
| `fixer.price_per_1m_input_usd` | `null` | 每百万输入 tokens 的价格（美元）；`null` = 未配置，不计费 |
| `fixer.price_per_1m_output_usd` | `null` | 每百万输出 tokens 的价格（美元）；`null` = 未配置，不计费 |
| `budgets.max_submissions` | `3` | 每 bug 最多提交次数（与 `domains/*/rrsi.json` 一致） |
| `budgets.time_budget_s` | `3600` | 每任务时间预算（秒） |
| `budgets.max_usd_per_job` | `null` | 每任务费用上限（美元）；`null` = 不限；超了即停 |
| `github.create_pr` | `false` | 是否在 ADOPT 后自动创建 PR；`false` 时只产分支、不建 PR |
| `github.token_env` | `GITHUB_TOKEN` | GitHub token 所在的环境变量名（token 本身不在文件里） |
| `domains.morde.repo` | `/home/hatch/workspace/morde-check` | morde domain 的仓库路径 |
| `domains.self.repo` | `/home/hatch/workspace/morde-rsi` | self domain 的仓库路径（循环修自己） |

`python3 service.py check-config` 会校验以上字段并报错指出缺失项，
启动前先跑它。

## API 参考

所有 `/v1/*` 路由都需要 `Authorization: Bearer <api_token>`；
缺 token 或 token 错返回 `401`。`GET /health` 是公开的。

| 方法 | 路由 | 请求体 | 成功响应 | 说明 |
|---|---|---|---|---|
| `GET` | `/health` | — | `200` | 存活检查，不需要 token |
| `POST` | `/v1/jobs` | `{"domain": "morde", "task_id": "CAND-002"}` | `202 {"job_id": "...", "status": "queued"}` | 提交一个任务进队列 |
| `GET` | `/v1/jobs` | — | `200` 任务列表 | 列出全部任务 |
| `GET` | `/v1/jobs/{id}` | — | `200` 任务详情 | 含当前状态与轮次信息 |
| `GET` | `/v1/jobs/{id}/evidence` | — | `200` | 该任务的证据：验证日志、reward.json 等 |
| `POST` | `/v1/jobs/{id}/feedback` | `{"kind": "human_label", "label": "wrong", "note": "..."}`；`label`（5 个固定标签：`correct` / `wrong` / `misdirected` / `missed_point` / `introduced_bug`）、`note`、`round` 均可选，`human_label` 必须配 `label` | `200` | 人工反馈，写入 `human_feedback.jsonl`，走 `rsi/feedback.py` 的三票裁决 |
| `POST` | `/v1/jobs/{id}/cancel` | — | `200` | 取消排队中 / 运行中的任务 |
| `GET` | `/v1/frontier?domain=morde` | — | `200` | 该 domain 的当前 frontier（已采纳基线） |

任务状态：`queued` → `running` → `done` / `failed` / `cancelled`。
`done` 只表示循环跑完了，不等于采纳——裁决见任务详情里的 `verdict`。

## 循环怎么跑

1. `service/worker.py` 的 Worker 轮询队列（`service/store.py` 的
   `JobStore`），取到任务后调用 `rsi/controller.py` 跑完整循环：
   DISCOVER → BASELINE → FIX → VERIFY → 裁决 → NEXT。
2. Fixer 是 `service/fixer_api.py` 的 `APIFixer`：向
   `{fixer.base_url}/chat/completions` 发请求，从返回里提取
   ` ```diff ` 代码块，用 `git apply --check` 校验后才用；遇到
   `429` / `5xx` 最多重试 2 次（间隔 2 秒、8 秒）；费用受
   `budgets.max_usd_per_job` 约束。
3. 裁决（`rsi/selection.py` + `rsi/feedback.py`）：
   `ADOPT` / `REJECT` / `NEEDS_HUMAN` / `STUCK` / `GAMING-DETECTED` /
   `SKIPPED`。**ADOPT 只创建分支 `rsi/<task-id>`，永不自动合并**；
   合入主分支是人工审 PR 的事。
4. `github.create_pr` 为 `true` 时，ADOPT 后由 `service/github_pr.py`
   创建 PR：正文含裁决、基线/候选分数、分层结果、成本收据、证据说明，
   并醒目标注"永不自动合并：请人工审阅后手动合并"。有 `gh` CLI 时用
   `gh pr create`，否则走 GitHub REST API（token 只从环境变量取）。
   无 origin、无 token、remote 不是 GitHub 时不建 PR（返回空，不报错）。
5. 人工反馈（`/v1/jobs/{id}/feedback`）直接进 `rsi/feedback.py`：
   三票裁决（验证器 + 用户 + 成本，两票才采纳）、NEEDS_HUMAN 路由、
   反馈转学习草案。反馈永不改写 `reward.json`。
6. 每任务一张成本收据：
   `<data_dir>/receipts/<job_id>.json`，含 `job_id`、`tokens_in`、
   `tokens_out`、`usd`（未配置价格时为 `null`）、`wall_seconds`、
   `verdict`、`branch`、`pr_url`。

## 安全说明

- **Bearer token 必需**：除 `/health` 外所有 `/v1/*` 都要
  `Authorization: Bearer <RSI_API_TOKEN>`；token 只走环境变量
  `RSI_API_TOKEN`，不要写进 `config.json`，不要提交进仓库。
- **密钥只走环境变量**：模型 key（`DEEPSEEK_API_KEY`）、GitHub token
  （`GITHUB_TOKEN`）都只从环境变量读；配置文件里只放"环境变量名"，
  不放密钥本身。
- **默认只监听本机**：`host` 默认 `127.0.0.1`，外网到不了；需要远程
  访问时再改，并确保 token 足够强。
- **隔离是 v1 级别**：每任务用独立的 git worktree（Fixer 工作区与干净
  Judge 工作区分离），与本地循环一致；**基于 Docker 的 Judge 隔离是
  已记录的后续工作，当前没有**——不要把服务暴露给不可信的任务来源。
- **永不自动合并**：ADOPT 只产分支，PR 只是给人审的；服务里没有任何
  合并代码。

## 成本与计价

- 价格只从配置来：`fixer.price_per_1m_input_usd` /
  `fixer.price_per_1m_output_usd`。两项任一未配（`null`），收据的
  `usd` 就是 `null`，PR 正文写"未配置价格，未计费"——**不编造费用数字**。
- `budgets.max_usd_per_job` 是硬上限：任务累计费用达到上限即停，
  未完成的轮次记失败；`null` 表示不限。
- 每次模型调用的 tokens 与费用都记进收据
  `<data_dir>/receipts/<job_id>.json`，PR 正文也会贴一份。

## Docker

```bash
docker build -t morde-rsi-service .
docker run -p 8765:8765 \
  -e RSI_API_TOKEN='改成一个长随机串' \
  -e DEEPSEEK_API_KEY='你的 DeepSeek API key' \
  morde-rsi-service
```

镜像基于 `python:3.12-slim`，启动命令是
`python3 service.py serve`，暴露端口 `8765`。

注意：morde domain 的仓库路径（`domains.morde.repo`，默认
`/home/hatch/workspace/morde-check`）必须在容器里存在，否则 morde
任务跑不起来。办法二选一：启动时挂载该仓库目录，或在容器内改
`service/config.json` 把 `domains.morde.repo` 指到容器内的实际路径。
self domain 同理（默认指向本仓库自身）。

## 排障

- **端口被占**：`serve` 启动失败报地址已被使用时，改 `config.json`
  的 `port`，或先停掉已在跑的服务再启动。
- **401**：检查请求头是否带了 `Authorization: Bearer <token>`，
  token 是否与 `RSI_API_TOKEN`（或 `config.json` 的 `api_token`）一致。
- **模型 key 缺失**：任务失败且日志报模型调用失败时，检查
  `DEEPSEEK_API_KEY` 是否已 export、以及 `fixer.base_url` /
  `fixer.model` 是否正确。
- **任务失败报 `Author identity unknown`**：采纳时 Controller 要
  `git commit`，目标仓库（或全局）没有配 user.name/user.email 就会
  失败。给目标仓库设好再重跑：
  `git -C <仓库> config user.name "名字"`、
  `git -C <仓库> config user.email "邮箱"`；或在启动服务的环境里
  export `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` /
  `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL`。
- **同一任务第二次跑报 `a branch named 'rsi/<task-id>' already exists`**：
  上一轮在 commit 时失败过，Controller 漏清理了 judge worktree，分支
  被残留 worktree 占着。清掉再重跑：
  `git -C <仓库> worktree remove --force <残留 worktree 路径>`、
  `git -C <仓库> branch -D rsi/<task-id>`。
- **任务失败了去哪看**：先看任务详情
  `GET /v1/jobs/<job_id>`（状态与裁决），再看落盘证据
  `<data_dir>/jobs/<job_id>/runs/` 下的验证日志与 `reward.json`，
  以及收据 `<data_dir>/receipts/<job_id>.json`。
- **配置不对**：`python3 service.py check-config` 会逐项报错，
  先跑它再启动服务。
