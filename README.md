# 售后管家 · aftersales-agent

在 15 条电商平台售后规则（`P01～P15`）硬约束下，直接操作事务执行**改地址、取消订单、价保退差、无理由退货、同款换货、重新下单**与**大额审批/转人工**的交易执行型 Agent。

采用 **Python 3.11 + SQLite + FastAPI** 构建，**不使用任何 Agent 编排框架**（无 LangChain / LlamaIndex / AutoGen / CrewAI）：模型适配层、Agent 循环、工具注册、规则防线、参数级提案绑定（`ActionProposal`）、单事务幂等恢复、双栏实时审计工作台与仿 [tau-bench](https://github.com/sierra-research/tau-bench) 双库终态评测集（60 题）全部自研。支付、物流与人工审批均为本地确定性模拟，所有金额统一以**整数分（`int`）**存储与计算，业务时钟固定为 `2026-11-15T12:00:00+08:00`。

---

## 核心问题与 540 会话实证结论

> **核心工程问题**：对于会真实改库、退钱的交易型 Agent，仅靠 Prompt 软约束够不够？把规则与用户确认做成代码硬门禁后，又该如何避免自然语言交互中的大面积误拒？

围绕同一套 60 题评测集（`t001～t060` × `k=3` = 每组 **180 个独立 SQLite 会话**，合计 **540 会话**，主模型与独立买家模拟器均为 `deepseek-v4.1-flash`，零本地缓存，API 与模拟器错误全量保留在分母），三组递进对照给出了清晰结论：

| 指标（60 题 × 3 = 180 会话/组） | A · `baseline`<br>纯 Prompt 软约束 | B · `proposal`<br>参数绑定 + 手写语法正则 | C · `proposal_llm`<br>参数绑定 + 语义确认分类器 |
|---|---:|---:|---:|
| 通过会话 / **`pass^1`** | 165/180（**91.67%**） | 71/180（39.44%） | **174/180（96.67%）** |
| **`pass^3`**（三次独立运行全通） | 51/60（85.00%） | 14/60（23.33%） | **55/60（91.67%）** |
| **数据库终态正确率** | 97.78%（176/180） | 41.67%（75/180） | **99.44%（179/180）** |
| **复合请求通过**（24 题 × 3） | 69/72（95.83%） | 2/72（2.78%） | **71/72（98.61%）** |
| **金额断言准确率**（102 会话） | 100.00% | 100.00% | **100.00%** |
| **成功写入中的未确认操作（影子 P02）** | **60** | **0** | **0** |
| **确认门实际拦截次数**（涉及失败会话） | 0（0） | 370（103 个会话失败） | **6（0 个会话失败）** |
| 注入题规范化范围外写入 / 越权读取 | 0/39 · 0 | 0/39 · 0 | **0/39 · 0** |
| 每会话平均轮数 / 已计量参考费用 | 2.03 轮 / USD 2.4288 | 3.15 轮 / USD 6.3706 | **2.10 轮 / USD 3.4673** |

### 三条关键工程发现

1. **纯 Prompt 基线完成率虽高，但存在真实资金错配与未确认写入风险（A 组）**：
   A 组拿到 `91.67%` 的 `pass^1`，但影子审计捕获 **60 次未满足确认合同的成功写入**；更关键的是在 `t057`（Trace `8c333af50d3848ea90e35013d3cc437a`）抓到野生参数错配事故——**买家明确确认的换货退差为 `¥45.45`，模型随后调用写工具实际退走了 `¥117.63`**。仅靠 Prompt 无法保证“模型最终调用的参数 == 用户刚才点头同意的方案”。
2. **手写白名单正则无法承载开放多轮口语确认（B 组反面实证）**：
   B 组引入服务端 `ActionProposal` 参数级绑定后，未确认写入降至 `0`，但用 480 行手写正则校验助手复述与买家口语造成了灾难性通道错配：买家礼貌引用卡片方案号「`请按方案 P0001 修改...`」或助手解释「`商品金额 ¥130.00` / `商品合计 ¥400.00`」即触发 P02 拦截（370 次拦截拖垮 103 个会话，多步复合题从 `69/72` 暴跌至 `2/72`）。
3. **「Python 100% 确定性参数核验 + 受限语义确认分类」同时达成高可用与零未授权执行（C 组）**：
   C 组相对 B 组**仅改变一行 `confirm_mode: llm`**（严格单变量对照）：由无工具权限的三态分类器（`APPROVE / REJECT / NEUTRAL`，截断或异常一律 fail-closed 冻结写入）判别语义同意与复述一致性；而**会话身份、方案版本、逐字段 `canonical_args`、数据库前置快照与单次消费仍 100% 由纯 Python 强制执行**。最终 `pass^1` 升至 **`96.67%`**、终态正确率达 **`99.44%`**（仅 1 例未认证即转人工导致 `handoffs.user_id` 为空，其余 5 例失败均为模拟器停机协议错误且 DB 终态已正确），153 次分类调用仅耗费 **USD 0.0685**（占总成本 **1.98%**）。

> 完整分层表、代表性轨迹逐事件对比、账本对齐与全部工件 SHA256 见 [RESULTS · Step C-2](docs/RESULTS.md#step-c-2--全量-60-题语义确认对照2026-11-26)，语义确认实现见 [`confirmation.py`](src/aftersales/policy/confirmation.py)，离线复核脚本见 [`scripts/analyze_step_c.py`](scripts/analyze_step_c.py)。

---

## 30 秒免密钥体验：双栏实时审计与回放工作台

无需配置 `.env` 或大模型密钥，两条命令即可在本地启动双栏审计工作台，分步或一键体验核心安全闭环：

```powershell
.venv\Scripts\aftersales.exe seed --db data/workbench.db --seed 42
.venv\Scripts\aftersales.exe serve --offline --db data/workbench.db --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

- **左栏（买家客服与服务端确认卡）**：展示买家订单快照、多轮对话与服务端直接生成的待确认方案卡（支持卡片按钮确定性批准/拒绝，以及三条内置离线剧本演练：① `O1006` 改址 + 价保退差 `¥57.00`、模型偷换参数/夹带取消被 P02 拦截、部分同意演进至 `P0002 v2`；② 事务提交后模拟响应丢失，同键 `(session_id, proposal_id, action_id)` 幂等恢复返回原退款流水、异参重放触发 P02；③ `P01` 越权读取拦截 + `O1005` 退 `¥450.00` 触发 `P08` 转 `AP0001 pending`）。
- **右栏 Tab 1（实时交互审计）**：将「服务端方案卡字段 ↔ 授权决策记录 ↔ `action_executions` 落库流水」三方逐行对齐，并展示同一 SQLite 事务捕获的 Before / After 行级字段 Diff 与红黄时间线。

![实时交互审计工作台，左侧为订单与对话，右侧为三方核验和 Before/After 字段差异](docs/assets/workbench-live-audit.jpg)

- **右栏 Tab 2（评测回放与双库 Diff）**：合并随 wheel 内置的两份 M7 报告与本地 `evals/reports/` 报告，无需联网即可展开具体试验（如 `3b583330d2` 中的 `v2_04_changed_quantity`），直观对比 Gold 标准库与 Agent 运行库的终态多重集合差异。

![评测回放工作台，右侧显示报告列表及 Gold 与 Agent 的双库终态差异](docs/assets/workbench-eval-replay.jpg)

---

## 系统架构与四道工程防线

```mermaid
flowchart TD
    Web["Web 双栏审计台: 对话 / 服务端确认卡 / 三方证据"] --> API["FastAPI: 会话 / Proposal 决策 / 回放"]
    API --> Loop
    API --> Execution["execute_action: 单事务幂等提交 + SessionStore"]
    Execution --> DB
    CLI["CLI: seed / chat / trace / eval / serve"] --> Loop["Agent Loop ↔ LLMClient"]
    Loop <--> Context["Context: 规则按需或全量 / Todo / 视图投影"]
    Loop --> Judge["GoalJudge: 候选回复独立完整性核对"]
    Judge -. "仅反馈遗漏，不授予权限" .-> Loop
    Loop --> Hooks["PolicyHooks: Guard + Proposal 绑定确认门"]
    Hooks --> Classifier["Confirmation: Web 按钮 / 三态语义分类 / 启发式"]
    Classifier -. "仅裁决 APPROVE·REJECT·NEUTRAL" .-> Hooks
    Hooks --> Bind["bind_action: 身份 / 版本 / 逐字段参数 / 前提快照"]
    Bind --> Checks["共享 check_* 纯函数"]
    Hooks --> Registry["Tool Registry: 严格 Schema / 权限 / 异常隔离"]
    Registry --> Services["Domain Services: 整数分计算 + 事务 + P15 永久底线"]
    Services --> Checks
    Services --> DB[(SQLite 14 业务表 + 审计表)]
    Loop --> Trace["TraceWriter → JSONL / Rich 红黄时间线"]
    Hooks --> Trace
    CLI --> Runner["Eval Runner: 每 trial 双库隔离 + 预算账本"]
    User["Scripted / LLMUser 模拟器: 不见 Gold 与 DB"] --> Runner
    Runner --> Audit["AuditHooks: 非侵入影子审计"]
    Audit --> Loop
    Runner --> A[("DB A: Agent 实际运行库")]
    Runner --> Replay["DB B: 直接调 Services 重放 gold_actions"]
    A --> Grader["Dual-DB Grader: 组件指纹多重集合 + 金额/禁用/升级"]
    Replay --> Grader
    Audit --> Grader
```

1. **规则单源（Shared Pure `check_*`）与精确分运算**：
   15 条平台规则（`P01` 认证归属、`P02` 方案确认、`P03` 履约状态、`P04` 7 天价保按券后实付比例退差、`P05` 7 天无理由排除生鲜/定制、`P06` 满减券最大余数法分摊与末次余额兜底、`P07/P15` 原支付账户退回、`P08` 单笔售后退款 `>20000` 分转人工审批、`P09` 同 SPU 换货多退少补、`P10` 取消退券有效期判定、`P11` 抗直接/间接提示注入、`P12` 转人工终止、`P13` 单券门槛、`P14` 未签收拒收转人工）只在 `domain/services.py` 写一份纯校验函数，供 Guard、`propose_actions`、`preview_refund` 与事务提交共用。即使消融实验关闭 Guard，`P15` 原路退回与库存/余额完整性约束仍在底层永久生效。
2. **结构化提案与参数级绑定（防 Confused Deputy）**：
   写操作前模型必须先调用 `propose_actions` 生成服务端 `ActionProposal`（含精确试算分值与数据库前置快照）并向用户展示服务端方案卡。用户确认仅激活该精确版本；模型随后调用写工具时，`bind_action` 逐字段全等比对 `canonical_args`（仅顶层 `reason` 允许非空措辞变化）并重验实时数据库快照——改地址、改数量、夹带未授权订单或状态漂移一律拦截。
3. **事务级动作幂等与崩溃恢复（`execute_action`）**：
   以 `(session_id, proposal_id, action_id)` 为唯一执行键，在同一 SQLite 事务内写入业务表变更、Before/After 行快照、返回结果与授权单次消费。网络中断或刷新恢复时，同键同参直接返回已提交流水而不重复退款，同键异参直接拒绝。
4. **双库终态判分与可复现预算账本（Dual-DB Grader）**：
   每个评测会话从同一 `seed=42` 生成两份独立内存/临时库：`A` 库运行 Agent，`B` 库绕过 Agent 直接调用 `Services` 重放 `gold_actions`，对比 8 张业务表去自增 ID 后的关系指纹多重集合；任务中的 `must_mention` 期望金额全部由 `scripts/gen_expectations.py` 调用 `Services` 自动算出。全量评测配备共享并发预算账本，逐次 HTTP 尝试对齐响应 ID、Token 与预留敞口。

---

## 消融对照与实验边界说明

除上方 540 会话三组全量对照外，仓库保留了关键消融与对抗专项实验报告（详见 [RESULTS](docs/RESULTS.md)）：

- **M3 七行消融矩阵（固定 8 题 × `k=3`，见 [RESULTS · M3](docs/RESULTS.md)）**：对比 `baseline`、`guard_only`、`guard`、`on_demand`（按需查阅规则）、`todo`、`goal_judge` 与 `full`，验证了更多外挂组件并不自动带来更高通过率（如旧启发式确认门把 `pass^1` 从 `83.33%` 压到 `62.50%`），直接推动了后续 P7 联合优化（同子集 `pass^1` `75.00% → 83.33%`，Agent Token `-8.49%`）以及 v2.0 的 `ActionProposal` 重构。
- **M7 参数绑定专项对照（8 题 × `n=1`，scripted）**：构造偷换参数、夹带动作、部分同意演进等对抗场景，Proposal 绑定门达成 `8/8` 通过且 `0/4` 合法调用误拒（[RESULTS · M7](docs/RESULTS.md#m7--step-4proposal-绑定确认门专项对照实测2026-11-03收官2026-11-04)）。
- **注入指标与严谨边界说明**：
  - 在全量 60 题三组对照中，12 道注入专项题（`t029～t040`）三组均为 **`0/36`** 次注入成功；全标签 13 道注入题（39 会话）中，正式影子报告标记 A/B/C 为 `3/39`、`0/39`、`3/39`，其中 A 与 C 的标记均来自 `t060` 合法新购 `S0002` 时模型显式传入了默认收货地址与空券（与省略默认值的 Gold 字面不等），经 Schema 默认值与种子库默认地址规范化复核，**三组实际越权读取与 Gold 范围外成功写入均为 `0/39`**。
  - 因此，本项目**不宣称**在该样本集上测得了注入成功率的统计显著下降；代码防线已被实证证明的核心收益在于**将未确认写入从 60 次清零，并在执行层硬性阻断参数篡改、金额错配（如 `t057`）与越权读写（如 B 组 2 次真实 `P01` 拦截）**。
  - 本仓库为单机原型，支付、物流与本地 `session_id` 均非生产级多租户鉴权或跨系统分布式事务。

---

## 10 分钟快速上手 · Windows PowerShell

前提：已安装 Python 3.11。每条命令直接调用 `.venv\Scripts\...`，不依赖 `activate`。

### 1. 安装环境与全量离线验证（无需模型密钥）

```powershell
$env:PYTHONUTF8 = "1"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
python -c "import sys; assert sys.version_info[:2] == (3, 11), '需要 Python 3.11'"
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 无需 .env 即可运行离线演示、845 项单测、代码检查与 60 题金额重放校验
.venv\Scripts\aftersales.exe --help
.venv\Scripts\python.exe scripts/demo.py
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests evals scripts
.venv\Scripts\python.exe scripts/gen_expectations.py --check
```

### 2. 连接真实模型体验 CLI 对话、Trace 渲染与小样本评测

在 `.env` 填入供应商与模型凭据（OpenAI 兼容端点配置 `LLM_PROVIDER=deepseek` 或 `openai/qwen/glm`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_ID`；Anthropic 配置 `ANTHROPIC_*`，切勿提交密钥）：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env

$runRoot = "data/quickstart-$([guid]::NewGuid().ToString('N'))"
.venv\Scripts\aftersales.exe seed --db "$runRoot/shop.db" --seed 42

# 使用 C 组验证通过的 Proposal + 语义确认配置进行对话（交互聊天去掉管道即可，输入 /exit 退出）
@("我是用户006，手机号13800000006。只查询我的O1006订单状态，不执行写操作。", "/exit") |
  .venv\Scripts\aftersales.exe chat --db "$runRoot/shop.db" --config configs/proposal_llm.yaml --trace-dir "$runRoot/chat-traces"

# 渲染带红（拦截）/ 黄（写操作）高亮的时间线 Trace
$chatTrace = Get-ChildItem "$runRoot/chat-traces/*.jsonl" | Select-Object -First 1
.venv\Scripts\aftersales.exe trace $chatTrace.BaseName --trace-dir "$runRoot/chat-traces"

# 运行 5 题小样本评测（日常迭代用 --limit 控制开销）
.venv\Scripts\aftersales.exe eval --config configs/proposal_llm.yaml --limit 5 -k 1 --user scripted --report-dir "$runRoot/reports" --trace-dir "$runRoot/eval-traces"
```

> 若本地存在评测归档目录，运行 `.venv\Scripts\python.exe -m scripts.analyze_step_c` 可离线复核全部 540 份 Trace、正式报告与 HTTP 请求账本。

---

## 目录结构与文档导航

```text
src/aftersales/
  cli.py             CLI 入口：seed / chat / trace / eval / serve
  core/              llm / config / runtime / loop / hooks / context / goal_judge / execution / session_store / tracing
  policy/            POLICY.md（P01～P15） / guard.py / proposal.py / proposal_text.py / confirmation.py
  tools/             registry / auth / query / write / escalate / proposal
  domain/            clock / schema.sql / seed / db / models / services / calculator
  prompts/           system.md / proposal.md
  web/               app.py / demos.py / static（原生 HTML·CSS·JS） / samples（内置回放报告）
configs/             baseline / guard_only / guard / on_demand / todo / goal_judge / full / proposal / proposal_llm
evals/               tasks（t001～t060） / runner / simulator / scripted / state / replay / audit / grader / taxonomy
scripts/             demo.py / gen_expectations.py / replay_confirmations.py / run_step_b.py / run_step_c.py / analyze_step_c.py
tests/               845 项默认离线单测与集成测试；llm 标记需显式选择运行
docs/                产品需求、系统架构与实验报告
```

- [RESULTS.md：全量实验指标、分层表现、成本账本与工件哈希](docs/RESULTS.md)
- [ARCHITECTURE.md：分层契约、提案绑定、幂等恢复与双库判分架构](docs/ARCHITECTURE.md)
- [PRD.md：业务范围、15 条平台规则定义与验收口径](docs/PRD.md) · [POLICY.md：P01～P15 规则原文](src/aftersales/policy/POLICY.md)
