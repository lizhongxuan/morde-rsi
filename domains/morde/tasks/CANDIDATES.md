# RSI 试点 bug 候选（CANDIDATES.md）

- 仓库：`~/workspace/morde-check`，HEAD `273d300`（约 3597 个文件）
- 挑选标准：`domains/morde/README.md` + `domains/morde/tasks/TEMPLATE.json`（A 级 = 小范围：文案/UI/配置/小逻辑，diff 小，有测试能验证；Fixer 不碰测试文件、守卫文件、migrations、凭证、DSH pins）
- 写入时间：2026-11-13
- 状态：**待用户挑选，未写入 `evolve.json`**

## 诚实结论：只找到 2 个过硬候选，不是 5 个

本轮把能扫的都扫了：Go 全量测试（`go test -json ./internal/... ./cmd/... -count=1`，/tmp 克隆跑）、Portal 全量 vitest（51 文件 / 529 测试，全部通过）、TODO/FIXME 全仓 grep、审计文档（iOS 22 项、PC 对话页 review、daily-check state）逐条过。结论是这版代码的测试套件基本是绿的，**过硬的 A 级候选只有 2 个，都是 Portal 前端**。不凑数。

- 找不到 5 个的原因，不是没找：Go 侧除已知环境失败外全部通过；Portal 侧 529 个测试全过；TODO 只有 6 处且全在已停更的 nanoMuse iOS 客户端；审计文档里其余条目都是缺失功能/设计差距，不是 A 级小 bug。
- 建议：试点先用这 2 个跑（先 CAND-001，再 CAND-002）。如果用户想要第 3–5 个，有两个正经办法：(1) 把 morde-check 更新到 origin/main 新 HEAD（273d300 之后的新代码可能有新 bug）；(2) 放宽标准，把“缺失功能”类条目降级为试点任务——但那会违反 A 级定义，不推荐。

## 候选 1：CAND-001（推荐先跑）

- **标题**：已分派任务的消息在聊天里不显示路由状态（`routingStatusText` 缺 `routed` 分支）
- **位置**：`web/portal/src/app/message-status.ts:3-9`（`routingStatusText`）
- **证据**（已验证，真实存在）：
  - 后端 schema 的 `routing_status` 枚举有 6 个值：`pending | processing | routed | waiting_user | waiting_adjustment | failed`（`web/portal/src/api/schema.d.ts:7579`）。
  - 服务端确实会把消息置为 `routed`：`server/internal/store/postgres/conversation_route_actions.go:472,484,590`（`finishConversationRoute(..., "routed", ...)`）；`server/internal/domain/conversation/types.go:64`（`RoutingRouted = "routed"`）。
  - `routingStatusText` 只处理了 5 个值，`routed` 走 default 返回 `null`（`message-status.ts:3-9`）。
  - 可观察后果：`routes/Assistant.tsx:1236,1278`——`routed` 且 `delivery_status` 为 `applied` 时，聊天里不渲染任何状态行；`routes/Projects.tsx:836`——回退到 delivery 文案，用户看不到“已分派”。也就是说，一条已经分派到任务的消息，在聊天页看起来像没有任何状态。
  - 邻居函数已验证无问题（排除误报）：`deliveryStatusText`（pending/applied/unknown 全覆盖）、`routingFailureText`（4 个 reason 全覆盖）、k2-task-projection 的 TaskStatus 9/9、TaskStepStatus 6/6、`hostStateLabel` 7/7、`ERROR_MESSAGES` 25/25。
- **复现方法**（已在 scratch 拷贝跑过，测试失败，确认是 bug）：
  - 新建 `src/app/message-status.test.ts`：`expect(routingStatusText('routed')).toBeTruthy()` → vitest 1 failed（基线失败已确认）。
  - UI 复现：发一条被分派到任务的消息，等 `delivery_status` 变为 `applied`，聊天里无状态行。
- **为什么是 A 级**：纯 UI 文案，一个 `if` 分支；行为明确（给 `routed` 一个文案，如“已分派任务”——服务端没有现成的中文标签，文案由 Fixer 定）；不碰守卫路径；有测试能直接证明。
- **建议 tier A 复现测试**：`web/portal` 下 `npm run typecheck` + `vitest run src/app/message-status.test.ts`（新测试，基线失败；预期通过后 `routingStatusText('routed')` 返回非空字符串）。
- **tier B 回归包**：`vitest run` 全量（51 文件 / 529 测试，基线全过，约 100 秒）+ `npm run typecheck`。
- **tier C 留出测试思路**：对 `routing_status` 全部 6 个枚举值断言 `routingStatusText` 返回非空且互不相同的文案（防止 Fixer 只给 `routed` 硬编码一个值、或顺手改坏其他分支）；另加 `deliveryStatusText` / `routingFailureText` 的枚举穷尽断言（防止改坏邻居）。
- **预估 diff 大小**：+1~3 行（一个分支），component: `ui-text`。

## 候选 2：CAND-002

- **标题**：601–920px 宽度下 AI 模型选择框被压扁截断，看不清当前选了哪个模型
- **位置**：`web/portal/src/routes/Assistant.tsx:1342`（模型 `<select>`，无 `title` 属性）；`web/portal/src/styles.css:340`（基础规则 `max-width: 250px`）、`:852`（601–920px 媒体查询内 `width: 0; min-width: 0; flex: 1 1 0;`）
- **证据**（已验证，真实存在）：
  - PC 对话页 review（2026-11-10）已把“fix truncated model select”列为 P0。
  - CSS 规则证明：在 601–920px 区间，模型选择框 `width: 0` + `flex: 1 1 0` + `min-width: 0`，会随 flex 容器收缩且无最小宽度；原生 `<select>` 的显示值会被省略号截断。
  - 后果：两个同前缀模型（如 deepseek-v4-flash / deepseek-v4-pro）截断后无法区分；用户在中宽屏上看不到当前到底选了哪个模型。
  - 复现测试已在 scratch 拷贝跑过：在 `Assistant.test.tsx` 临时加用例（按现有 153–154 行模式取 composer 内 `选择 AI 模型`），断言 `title` 属性等于当前选中项完整文案 `'团队模型 · morde-test'` → vitest 1 failed（基线失败已确认）。
- **复现方法**：vitest，在 `Assistant.test.tsx` 加用例，断言模型 `<select>` 的 `title` 属性等于当前选中项的完整文案（`{name} · {model_name}`）。
- **为什么是 A 级**：一行属性（给 select 加 `title={选中项完整文案}`），行为明确，不碰守卫路径。**注意**：本候选验证的是修复的可观测契约（title 暴露全名），而非布局像素——jsdom 测不了截断像素；如用户坚持要修布局本身，可改 tier A 为 Playwright e2e（dev server + mock API，见 `assistant.e2e.ts` 的跑法），但 e2e 更重。
- **建议 tier A 复现测试**：`web/portal` 下 `npm run typecheck` + `vitest run src/routes/Assistant.test.tsx`（新用例基线失败；全文件 89+1 测试约 10 秒）。
- **tier B 回归包**：`vitest run` 全量 + `npm run typecheck`。
- **tier C 留出测试思路**：切换到另一个模型（测试里已有 `otherModel`“备用模型”）后，`title` 跟随更新为新模型的完整文案；断言 `title` 不等于旧值（防止 Fixer 写死第一个模型的 title）。
- **预估 diff 大小**：+1 行（select 加 `title` 属性），component: `ui-text`（如选 CSS 加宽方案则是 `ui-style`）。

## 备选（不推荐进试点，原因写明）

### ALT-A：workspacefs 包测试无法编译（import cycle）——真实，但修不了

- 位置：`server/internal/storage/workspacefs/store_enospc_linux_test.go` → import `internal/app` → `workspacecurrent` → `workspacefs`，构成 `import cycle not allowed in test`；整个包的测试 `[setup failed]`，无法运行。
- 为什么不进试点：修复在测试文件（移走/改写测试）或生产代码大重构，两条都违反试点约束（Fixer 不碰测试文件；A 级小范围）。建议人工修，不走 RSI。
- 证据：`/tmp/cand-gotest.json`（Go 全量扫掠原始输出）中有该包 `[setup failed]`。

### ALT-B：3 个 umask 敏感测试（app 1 个 + sitefs 2 个）——测试脆弱，不是产品 bug

- 位置：`server/internal/app` 的 `TestQuotaSettlePersistsOwnerStoragePauseBeforeAborting`；`server/internal/storage/sitefs` 的 `TestOpenRejectsNonPrivateRootWithoutChangingIt`、`TestOpenChecksUnknownEntriesBeforeChangingStorageDirectories`。
- 证据：本机 umask 是 0007。Go 的 `testing` 创建 TempDir 子目录用 `os.Mkdir(dir, 0777)`，0007 下得到模式 0770；`workspacecurrent.Open` 故意拒绝组/其他可写的根目录（`Perm()&0022 != 0` → `ErrUnsafePath`，workspace 物化的安全不变量）。实测：sitefs 两个测试在 umask 022 下通过；app 那个在 umask 077 下通过。**两个组在任何一个 umask 下不能同时通过**，套件是 umask 脆弱的。
- 为什么不进试点：修复在测试文件（给测试加 chmod），Fixer 不碰测试文件。且根因是测试假设与产品安全不变量冲突，不是产品行为错。
- **试点前注意**：跑试点那台机器的 umask 会影响这 3 个测试的基线。tier B 是逐测试 P2P（基线失败的不计入），所以不影响采纳，但跑基线时要记录当前 umask。

### ALT-C：worker 包 46 个 + app 包其余环境失败——环境问题，不是 bug

- worker 包 46 个失败：43 个 `account Agent Runtime handshake is invalid`（缺 DSH 运行时握手）、3 个浏览器超时（20–45s）；app 包另有 1 个 workspace 路径假设（见 ALT-B 同一测试）。骨架搭建时已在 `docs/rrsi-mapping.md` 记录。不是产品 bug，不进试点。

## 排查方法与范围（供复核）

- Go 全量：`go test -json ./internal/... ./cmd/... -count=1`（/tmp 克隆，HEAD 273d300）。除上述外全部通过；扫掠中 api、cmd/portal、store/postgres、syncnodequota 曾出现 `[build failed]`，单独重跑后通过（扫掠期 /tmp 压力所致的偶发）。原始输出：`/tmp/cand-gotest.json`。
- Portal 全量：`vitest run` 51 文件 / 529 测试全部通过（scratch 拷贝 + `npm ci` + 把 `server/openapi/` 补进 scratch；第一次 2 文件失败是 scratch 缺 fixture 的假象，补齐后全过）。原始日志：`/tmp/cand-vitest.log`。
- TODO/FIXME/XXX/HACK 全仓 grep：只有 6 处，且全在已停更的 nanoMuse iOS 客户端（`clients/nanoMuse`），多为功能愿望而非 bug；无候选。
- git log：本地是浅克隆，只有 4 个提交，无 bug 修复记录可参考。
- 审计文档逐条过：iOS 22 项、PC 对话页 review、daily-check state——其余条目都是缺失功能/设计差距，不是 A 级小 bug；唯一落到 A 级的是 CAND-002（PC review P0）。
- 已查清、无 bug 的地方（避免重复劳动）：配置校验（`server/internal/config/config.go`）、schedule 的 Validate/FirstOccurrence/NextOccurrence/LatestDue、ID 校验（`server/internal/domain/id.go`）、错误码→HTTP 映射（25 个全映射）、分页参数、容量数学（防溢出）、manifest 摘要限制、文件投递意图分类（启发式、有显式“shortcut”注释，属判断问题）、项目工作项 Create 校验、`format.ts` 助手函数（`formatDuration` 是未使用的死代码）、`message-upload.ts`、`message-receipt-order.ts`、`desktopDownloads.ts`、`sitefs.Open`（正确拒绝非私有根目录）、`ExecutionEpoch: 1` 占位符（store 会用 `gate.epoch` 替换）、工作项分类校验、消息路由与重试逻辑（`conversation_message_routing.go`、`conversation_message_retry.go`、`conversation_route_actions.go` 的状态机与前序链检测，含 128 层防环）。

## 试点顺序建议

1. **CAND-001 先跑**（最干净：纯函数、复现测试精确、1 行修复，适合第一次手动 Fixer 试点）。
2. **CAND-002 第二**（同样 1 行，但验收是契约式而非像素式，适合验证 tier C 的“跟随更新”思路）。
3. 两个跑完、DSHFixer 接线后，再从 origin/main 新 HEAD 或新发现里补第 3–5 个。

## 清理说明

验证用的临时克隆已删除：`/tmp/morde-cand-clone`、Go 扫掠输出以外的临时目录、`~/workspace/tmp-cand-portal`（Portal scratch 拷贝 + node_modules）。`~/workspace/morde-check` 本体未动（只读），`evolve.json` 未动（仍是 0 个真实任务）。
