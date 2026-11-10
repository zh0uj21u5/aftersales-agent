# 售后管家 · aftersales-agent

在平台规则内执行改地址、取消订单、价保、退货、换货和重新下单的电商售后 Agent。Python 3.11 + SQLite，自研模型适配、Agent 循环、工具、Guard、确认门和评测，不使用 Agent 编排框架。支付、物流和审批均为本地模拟，不接真实商家系统。

v2.0.0 在冻结的 v1.0 基线上新增服务端Proposal绑定授权、动作级幂等恢复与双栏审计工作台。无需模型凭据即可运行 `aftersales serve --offline` 演示三条完整链路，启动说明见下方工作台小节。

核心问题是：**代码层 Guard + 确认门，能否比仅靠 Prompt 更可靠地约束执行？** 项目以双库最终状态判分、用户模拟器和 `pass^k` 检验这个假设，方法借鉴 [tau-bench](https://github.com/sierra-research/tau-bench)。已有具体越权读取拦截与确认合同执行证据；小样本尚不足以宣称违规率、注入成功率普遍或统计显著下降。

## 架构

```mermaid
flowchart TD
    Web["Web双栏审计台: 对话 / 确认 / 证据"] --> API["FastAPI / Proposal Decision"]
    API --> Loop
    API --> Execution["幂等动作事务 / Session恢复"]
    Execution --> DB
    CLI["CLI: seed / chat / trace / eval"] --> Loop["Agent Loop ↔ LLMClient"]
    Loop <--> Context["Context: 规则 / Todo / 消息投影"]
    Loop --> Judge["GoalJudge: 独立完整性核对"]
    Judge -. "仅反馈遗漏，不授予权限" .-> Loop
    Loop --> Hooks["PolicyHooks: Guard + Confirm Gate"]
    Hooks --> Registry["Tool Registry: Schema / 权限 / 异常"]
    Hooks --> Checks["共享 check_* 纯函数"]
    Registry --> Services["Domain Services: check_* + 事务 + P15永久底线"]
    Services --> Checks
    Services --> DB[(SQLite)]
    Loop --> Trace["TraceWriter → JSONL / Rich时间线"]
    Hooks --> Trace
    CLI --> Runner["Eval Runner: 每trial隔离"]
    User["Scripted / LLM Simulator: 不见gold与DB"] --> Runner
    Runner --> Audit["AuditHooks: 非侵入影子审计"]
    Audit --> Loop
    Runner --> A[("A: Agent运行库")]
    Runner --> Replay["B: 直接Services重放gold"]
    A --> Grader["Dual-DB Grader: 状态多重集合 + 金额/禁用/升级检查"]
    Replay --> Grader
    Audit --> Grader
```

普通写入先校验身份、业务资格和本轮确认。所有金额是整数分；业务时间固定为 `2026-11-15T12:00:00+08:00`。Guard 与服务共用规则，不复制公式；即使消融关闭 Guard，P15 原支付账户和数据完整性约束仍生效。GoalJudge、Todo 和模型自称“完成”均不能代替执行证据。

## 核心结果

主模型为 `deepseek-v4.1-flash`，P7 另比较 `qwen-plus`。所有失败、API 错误和模拟器错误保留在分母，未挑选重跑。完整数据、trace索引、费用与源码哈希见 [RESULTS](docs/RESULTS.md)。设计与版本边界见 [ARCHITECTURE](docs/ARCHITECTURE.md)。

| 实验 | 用户 / 任务×n | pass^1 | pass^3 | 成功执行P02合同违规 |
|---|---|---:|---:|---:|
| P4 Baseline | scripted / 30×1 | 83.33% | — | 1 |
| P5 Baseline | LLM / 40×1 | 95.00% | — | 11 |
| P5 Guard+确认门 | LLM / 40×1 | 85.00% | — | 0 |
| P6 Full完整集 | LLM / 40×1 | 85.00% | — | 0 |
| P7-A Flash固定子集 | LLM / 8×3 | 83.33% | 62.50% | 0 |
| P7-A Qwen固定子集 | LLM / 8×1 | 37.50% | — | 0 |
| P7-A Flash新增抽检 | LLM / 4×1 | 75.00% | — | 0 |

M3 七行矩阵使用同一固定8任务，每项 `n=k=3`；Baseline/Guard两行引用P5，同一批旧数据不算新试验。

| 配置 | 来源 | pass^1 | pass^3 | P02合同违规 | 直接注入成功 |
|---|---|---:|---:|---:|---:|
| baseline | P5引用 | 83.33% | 75.00% | 13 | 1/9 |
| guard_only | P6 | 83.33% | 75.00% | 13 | 0/9 |
| guard | P5引用 | 62.50% | 50.00% | 0 | 0/9 |
| on_demand | P6 | 70.83% | 50.00% | 0 | 0/9 |
| todo | P6 | 66.67% | 50.00% | 0 | 0/9 |
| goal_judge | P6修复版 | 62.50% | 25.00% | 0 | 0/9 |
| full | P6修复版 | 75.00% | 50.00% | 0 | 0/9 |

P7-A联合优化相对P6 Full固定子集：`pass^1` 75%→83.33%，`pass^3` 50%→62.5%，每会话Agent Token 71,379.96→65,319.58（-8.49%），GoalJudge平均输入6,191.60→3,192.56（-48.44%）。确认、STOP、自由文本合同也发生了事前授权变更，且不是逐trial配对实验，不能将差异单独归因某一修复。Qwen仅8个样本且有故障，不足以作普遍模型排名。

v1.0基线为 **P7-B后续发布版**：`c24a7db` 中最后两个有限正则补漏在P7仅做离线验证，未重跑固定8子集；上表全部仍为P7-A。P1～P6历史提交依次为 `b648385`/`ccd1932`、`fb3bd04`、`7c10b7f`、`e0aff73`、`0c51299`、`ac50fea`。M4另用1个chat+5个简单scripted任务验收，5/5通过，共USD0.0565131参考费用；不与P7研究结果合并。命令、耗时和范围见 [RESULTS的M4小节](docs/RESULTS.md#m4-v10--p8-端到端验收2026-11-03)。

P02数字衡量版本化词表/批次合同，不直接等于用户真实语义授权。60任务均经离线重放，只有规定子集有真实模型结果；0次注入成功、金额命中和GoalJudge通过均不证明普遍安全或所有解释正确。P6主上下文压缩未触发，不能宣称已测到其收益。

M7独立8题专项（每组n=1、scripted）：旧确认门7/8，新Proposal绑定门8/8；两组未授权成功写入均0，正常对照完成2/2且误拒0/4合法调用。新门总Token增加36.18%，合计16会话参考USD0.3357549。旧组唯一失败是模型漏读已给出的姓名，不能将完成差异归因为安全收益，也没有测得注入/违规率下降。详见[专项结果、失败与局限](docs/RESULTS.md#m7--step-4proposal-绑定确认门专项对照实测2026-11-03收官2026-11-04)。

## 10分钟快速上手 · Windows PowerShell

前提：仓库已在本地、已安装Python3.11，可访问依赖镜像与支持tool calling的模型端点。人工开通账号、填写凭据和慢网络耗时受环境影响。每条命令直接指定解释器，无需activate。

```powershell
$env:PYTHONUTF8 = "1"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
python -c "import sys; assert sys.version_info[:2] == (3, 11), '需要 Python 3.11，请改用该解释器的绝对路径'"
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

多版本环境可将上面两条`python`换成Python3.11解释器的绝对路径。

在 `.env` 选择供应商、端点、模型并填写密钥。OpenAI兼容端点使用 `LLM_PROVIDER=deepseek`（或openai/qwen/glm）、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_ID`；Anthropic使用对应 `ANTHROPIC_*`。模型名以自己的端点支持为准；本项目实验默认为 `deepseek-v4.1-flash`。不要输出或提交密钥。

先运行无API消耗的演示，再执行最小真实链路。输出目录用随机ID，避免覆盖已有数据库。

```powershell
.venv\Scripts\aftersales.exe --help
.venv\Scripts\python.exe scripts/demo.py
$runRoot = "data/quickstart-$([guid]::NewGuid().ToString('N'))"
.venv\Scripts\aftersales.exe seed --db "$runRoot/shop.db" --seed 42
@("我是用户006，手机号13800000006。只查询我的O1006订单状态，不执行写操作。", "/exit") | .venv\Scripts\aftersales.exe chat --db "$runRoot/shop.db" --trace-dir "$runRoot/chat-traces"
$chatTrace = Get-ChildItem "$runRoot/chat-traces/*.jsonl" | Select-Object -First 1
.venv\Scripts\aftersales.exe trace $chatTrace.BaseName --trace-dir "$runRoot/chat-traces"
.venv\Scripts\aftersales.exe eval --config configs/guard.yaml --limit 5 -k 1 --user scripted --report-dir "$runRoot/reports" --trace-dir "$runRoot/eval-traces"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests evals scripts
.venv\Scripts\python.exe scripts/gen_expectations.py --check
```

这条真实链路最多选5个评测任务与1个chat会话；scripted用户不调用模型，Agent仍消耗Token，原任务有确认等多轮。默认`eval`配置是用于消融的baseline，会关闭Guard和确认门；上面显式选择guard配置。`chat`不传配置时默认启用Guard、确认门与试算。交互聊天可运行同一个chat命令并去掉管道，完成后输入`/exit`。

只想离线查看执行过程，运行`demo.py`即可：场景A验证确认前无写入及原路退差；场景B显示P02未确认写入、P01越权读取、P08大额直接退款被拒，转为待审批，并自动渲染红/黄时间线。该脚本不加载`.env`。

## 目录与阅读顺序

### M7 双栏审计工作台

```powershell
.venv\Scripts\aftersales.exe seed --db data/workbench.db --seed 42
.venv\Scripts\aftersales.exe serve --offline --db data/workbench.db --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

纯离线即可一键或分步演示三条链路：① O1006改址+价保¥57.00，篡改/夹带P02拦截，部分同意后P0002 v2只改址；② 真实事务提交后丢响应，同键重放保留原退款流水，异参冲突与刷新恢复；③ P01越权拦截，O1005退3件¥450.00触发P08并生成AP0001 pending。每次场景独立种子库，原shop库不受影响。默认“下一步”会展示预设调用；确认卡按钮也可直接批准/拒绝当前方案。面试建议用下一步完整讲解预设链路。

左栏展示买家订单、对话、服务端确认卡与执行结果；右栏“实时交互审计”对齐提案/授权/执行键、显示事务快照字段变化与Trace。“评测回放与对比”只读加载本地 `evals/reports/*.json` 的指标和Agent/Gold双库终态差异；新克隆没有忽略的历史JSON时会显示空列表。自由聊天不伪造Gold库。

移除 `--offline` 可用 `.env` 模型发送自由消息，会消耗Token；离线模式不加载凭据，自由输入仅保存并提示使用剧本。服务只监听127.0.0.1，浏览器保存session_id，刷新后从SQLite恢复。工作台实现见 [ARCHITECTURE](docs/ARCHITECTURE.md)，专项实测见 [RESULTS](docs/RESULTS.md)。真实实验不随启动或演示自动运行。

```text
src/aftersales/
  cli.py             seed / chat / trace / eval / serve
  core/              llm / loop / hooks / context / goal_judge / tracing
  policy/            POLICY.md / guard.py
  tools/             registry / auth / query / write / escalate
  domain/            clock / schema.sql / seed / db / models / services / calculator
  prompts/           system.md
  web/               app / demos / static（原生HTML/CSS/JS）
configs/             baseline → guard_only/guard/on_demand/todo/goal_judge/full
evals/               60条基准任务 / 8条专项任务 / runner / simulator / replay / audit / grader
scripts/             demo.py / gen_expectations.py / eval_proposals.py
tests/               默认离线；llm标记需单独授权运行
docs/                产品、架构与实验结果
```

- [PRD：产品、规则、验收](docs/PRD.md) · [ARCHITECTURE：接口、执行与双库评分](docs/ARCHITECTURE.md)
- [POLICY：P01～P15规则](src/aftersales/policy/POLICY.md)
- [RESULTS：历史实验与 M7 专项对照](docs/RESULTS.md)

`.env`、`data/`、`traces/`、报告JSON不提交；Markdown保留实验来源。`seed`拒绝覆盖已有库。历史 JSON、trace 与缓存保留在本地，公开的 Markdown 报告可直接阅读。

文中的历史提交号用于标识本地实验版本，公开发布分支有独立的提交历史。
