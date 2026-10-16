# RPS 看板升级路线：复盘 · 模拟持仓 · 金融工具

> 目标：把现在的「RPS 选股看板」升级成能**每日复盘**、能**长期模拟持仓**、并能当**一套研究/风控工具**用的系统。
> 本文只列**有证据的现状**与**可落地的改动**；每条结论都带 `文件:行`，可直接核对。
>
> **行号基准**：`broker/sim.py` 的行号对应当前工作树（已含 §2.1 的续跑改动）；其余文件对应当前 `main`（`7e2386c`）。行号只是线索，改之前请以符号名重新定位。

---

## 0. 结论摘要

| 主线 | 完成度 | 最大缺口（一句话） |
|---|---|---|
| **复盘** | ~85%（本轮 +） | 模拟盘/个股按日回看 + **当日复盘页**都有了；仍缺**自选按日**、回测逐日与多参数对比 |
| **模拟持仓** | ~92%（本轮 +） | 撮合、续跑、复盘、**策略对照（归档）**都齐了；缺多账户**并行**、成交量约束 |
| **金融工具** | ~45%（本轮 +） | 回测侧（逐日净值/调仓记录/参数对比）已补；仍缺**导出中心**、风险工具、年化/夏普口径统一；数据源只有行情（无基本面） |

**本轮已落地的四件事**（P0-1～P0-4，全部带回归测试）：

1. ✅ **模拟盘续跑 + 每日推进**：再跑一次 `cli trade run`（可省 `--from`）只推进新交易日，历史净值不重算；换参数/换资金会直接报错而不是静默混算。
2. ✅ **顺延委托重试**：停牌/涨停顺延的委托会在后续每个交易日继续尝试，直到成交或撤销（原先永久卡 `pending`）。
3. ✅ **成交落费用与已实现盈亏**：`trade_fill` 新增 `fee`/`realized_pnl`/`avg_cost`（老库幂等补列）。**这一步顺手抓到并修掉一个更严重的缺陷**：部分卖出不减仓 → 账户凭空多出现金（真实库实测虚增约 2.97 万元）。
4. ✅ **复盘面板 + 个股按日锚定**的第一半：模拟盘绩效复盘（逐笔回合/胜率/盈亏比/持有天数/费用/沪深300 超额）+ `?date=` 回看历史某日；页面已有完整面板。

---

## 1. 现状能力盘点

### 1.1 复盘

| 能力 | 状态 | 证据 |
|---|---|---|
| 任选历史交易日（就近回退，不 400） | **有** | `api/__init__.py:53-90` `resolve_date`；日期清单 `api/meta.py:138-153`（取自 `rps_daily`，≤2000 天） |
| 排行榜按日看 RPS + 当日涨跌 + Δ名次 | **有** | `frontend/src/views/Rankings.vue:84-88`（日期下拉）、`:29-33`（排序键）、`:60-67`（Δ名次 0/1/5/20/60）；后端 `api/rps.py:343-445` |
| 板块按日看（行业/概念 + 热力图，可切日） | **有** | `Sectors.vue:20-23`、`api/rps.py:631-680` |
| 板块**当日**涨跌 | ✅ **已有**（本轮新增） | `/api/review/day` 的 `sectors`：按 `industry_map` 等权聚合 `daily_bar.pct_chg`（板块页本身仍只给区间 `ret{p}`） |
| 个股按日回看 | ✅ **已有**（本轮新增） | `api/stock.py::detail/curve/patterns` 都接受 `date` 并先过 `resolve_date`；形态的「此后最低价」也以该日为上界（不偷看未来）；页面头部有「回看」下拉 |
| 自选按日回看 | **无** | `api/watchlist.py:96-115` 硬编码 `r.date = (SELECT MAX(date) FROM rps_daily)` |
| 模拟盘按日查看 | ✅ **已有**（本轮新增） | `/api/trade/paper?date=`（`api/trade.py::_paper_date` 就近回退）+ 页面「回看」下拉；`paper_nav`/`paper_position` 本身按日留档（`db.py:292-313`） |
| 当日市场宽度（涨跌家数 / 涨停家数 / 情绪） | ✅ **已有**（本轮新增） | `/api/review/day` 的 `breadth`：有行情/上涨/下跌/平盘/涨停/跌停/中位涨跌；分母是当日有 K 线且未停牌 |
| 涨停板清单 | ✅ **已有**（含连板） | `/review` 页「涨停板」表；判据来自 `analytics/limits.daily_limit_lists()`，与 `limit_up` 预警规则**同一个函数** |
| 历史某日的策略信号名单 | ◑ **部分**（本轮新增） | `/api/review/day` 给「RPS 阈值 + ST 过滤」的名单；**entry_mask 的完整口径（共振/择时）**仍只在回测页，`trade_plan` 的 items 也依旧只能取最新一份 |
| 回测逐日对照 | ✅ **已有**（本轮新增） | `daily_curve` 逐日盯市 + `metrics.max_drawdown_daily`；页面同时给出两个口径 |
| 回测/模拟盘导出 | **无** | 全仓唯一导出是排行榜 CSV（`api/rps.py:450-515`）；`backtestApi` 只有 `run`（`api/index.js:173-177`） |
| 多次回测参数对比 | ✅ **已有**（本轮新增） | 前端内存表（最多 6 次并排）+ JSON 导出/导入；**未落库**，跨会话留存需导出文件 |
| 交易日志 / 复盘笔记 | **无** | 全仓无相应表 |

### 1.2 模拟持仓

| 能力 | 状态 | 证据 |
|---|---|---|
| 撮合引擎（限价判定、涨跌停、整手、逐笔费用） | **有**（有回归测试） | `broker/sim.py` 模块 docstring；`settle_day` `:158-224`；费用走 `analytics/costs.py` 同一套模型；测试 `test_paper_sim.py` |
| 与回测共用选股口径 | **有** | `sim.run` 用 `analytics/backtest.entry_mask`（`broker/sim.py:530`），计划走 `plan.build_plan`（`plan.py:317-320`） |
| 账户/净值/持仓按日留档 | **有** | `db.py:285-317`（`paper_account` 单行 + `paper_position`/`paper_nav` 按日） |
| 停牌/涨停顺延 | ✅ **已修**（原先名义有、实际坏） | 见 §2.3；`settle_day` 现在捞"到期未成交的全部挂单" |
| 成交明细（费用 / 已实现盈亏） | ✅ **已修** | `trade_fill.fee/realized_pnl/avg_cost`（`db.ensure_fill_columns` 幂等迁移），见 §2.4 |
| **安全续跑 / 推进到今天** | ✅ **已修** | 见 §2.1；`trade run` 幂等推进，`--from` 可省 |
| 多账户 / 多策略并行 | ◑ **可对照**（本轮新增） | 仍是单活动账户，但 `paper_archive` 可以在 `--reset` 前把曲线存下来叠加比较（P1-6）；真正的并行运行仍需破坏性迁移 |
| 重置 | **有** | `sim.reset_account` `:112-123`，CLI 需 `--reset --yes-reset` |
| 回滚 / 截断到某日 | **无** | 只有整表 DELETE（`sim.py:116-120`） |
| 从任意起始日重跑 | **不支持（有意）** | 曲线起点固定，改起点要 `--reset`；起点随 `paper_meta.run_start_date` 落库 |
| 逐笔回合盈亏 / 胜率 / 持有天数 | ✅ **有** | `sim.review()`；页面「绩效复盘」+ 逐笔回合表；测试 `test_paper_review.py` |
| 相对基准超额 | ✅ **有** | `review()` 读 `benchmark_bar`（与回测同一张表） |
| 行业暴露 / 集中度 | ✅ **有**（行业分布 + 权重） | `review()["industry"]` / `positions[].weight` |
| 年化 / 波动 / 夏普 / 卡玛 | ⏳ 无 | `compute_metrics` 只被回测调用（`analytics/backtest.py:716-761`）；可复用，见 P2 |
| 成交量 / 流动性约束 | ⏳ 无 | 成交价恒等于限价、可无限量成交（`sim.py`）；见 P2 |
| 界面入口 | ✅ **有**（含历史某日回看） | `Trade.vue` 绩效复盘卡 / 逐笔回合 / 行业分布 / 回看日期下拉；`/api/trade/paper?date=` |
| 每日自动推进 | ✅ **有**（可选） | `cli update --paper`（阶段名 `paper`，`App.vue` 已加标签）；页面按钮刻意不带此步 |

### 1.3 金融工具

| 能力 | 状态 | 证据 |
|---|---|---|
| 13 因子体检（IC/IR/分层/相关矩阵） | **有** | `analytics/factors.py`、`cli factors`、`/api/factors`、`Factors.vue` |
| 合成分作为策略信号 | **有** | `backtest.SIGNALS`（`analytics/backtest.py:36-38`）、`composite_score`（`factors.py:348-369`） |
| 回测（成本、涨跌停、择时、分档前瞻收益） | **有** | `analytics/backtest.py`、`Backtest.vue` |
| 交易成本模型（佣金/印花税/过户费/滑点） | **有** | `analytics/costs.py`；影子盘可校准（`cli trade calibrate`） |
| 涨跌停规则（实测 + 推算兜底） | **有** | `analytics/limits.py:33-45,133-166` |
| 数据质量诊断（复权异常、覆盖度） | **有** | `analytics/quality.py`、`cli quality`、`/api/meta/status` 的 `quality` 块 |
| 性能监测（前后端耗时对照） | **有** | `api/perf.py`、`App.vue` 顶栏 |
| 截面筛选保存 / 自定义选股器 | **无** | 筛选条件只在 URL/控件里，无法保存或复用 |
| 组合风险工具（相关性、集中度、回撤预警） | **无** | — |
| 基本面 / 财务数据 | **无** | 唯一数据源是 eltdx 行情协议（`config.py:111-120`） |
| 分钟级 / Level2 | **无** | 同上；本项目刻意用日K |

### 1.4 已经做过的事（本轮会话，已推送 `7e2386c`）

六处已复现的功能/显示错误已修复并推送：`read_sql` 缺表 500、交易页账号被快照清空、涨停预警误报且旧命中残留、`cli factors --train-days` 空转参数、个股页错误条被 `v-else` 吞掉、模拟盘收益配色反向。
验证：后端 311 passed / 覆盖率 67.04%，前端 6/6，ruff 干净，真实浏览器冒烟通过。

---

## 2. 已确认缺陷（本轮实跑复现）

> 状态标记：**✅ 已修复并有回归测试** / ⏳ 待做。
> P0-1～P0-4 在本文档写成之后已全部落地（见 §3 各项的"落地状态"）。

### 2.1 【严重·数据完整性】重跑模拟盘会改写全部历史净值

**复现**（合成库，30 只票 × 120 交易日，先跑到 day40~day80，再"推进到今天"跑到 day110）：

```
run1: days 41 orders 57 filled 56 final 1154509.53
run2: days 71 orders 94 filled 65 final 1096593.65
settled_days_rewritten 41 of 41        ← 所有已结算日都被改写
position_rows_differ 475
```

**机理**：`sim.run` 每次都用 `from_date` 重走整段（`broker/sim.py:535`），而账户是从**当前**状态载入的 —— `init_account` → `load_account(conn)` 取 `paper_nav` **最后一天**的现金与持仓（`sim.py:92-107`）。于是重跑时，第 1 天会被写成"用今天的持仓去估那天的价"：

```
2025-02-26  cash_1=1,000,000  n_positions_1=0   →   cash_2=56,629.6  n_positions_2=5   ← 被改写成期末状态
```

**为什么一定会踩**：`cli trade run --from X` 的 `--to` 默认是"至今"（`cli.py:1512`）。用户**第二次执行同一条命令**（哪怕只是想推进一天）就会触发。

**当前处理**：已在 `broker/sim.py` 实现续跑语义（见 §3.1），现有 46 项相关测试通过；**专门的"单次长跑 ≡ 截断+续跑"等价性回归测试尚未补**（未提交）。

兼容性已处理：真实库里的曲线 `paper_meta.params` 是**旧格式**（`{"period":60,"threshold":80.0,"hold":20,"max_positions":20,"exclude_st":true,"resonance":false}`，**没有 `signal` 键**）。若按"完全相等"校验，用户现有的 91 天曲线会被判成"参数不一致"而只能 `--reset` 毁掉；因此续跑校验对缺失键按默认值补齐后再比。

### 2.2 【中】`mode="local"` 仍被实测涨跌停行卡住

`plan.build_plan` 里 `no_limit_row` 分支没有用 `check_limits` 门控（`broker/plan.py:236-241`）：

```python
elif sym not in limits:
    reason = "no_limit_row"     # ← 对 local 也生效
```

而自建模拟盘**根本不用** `price_limit_daily`：`check_limits = not local`（`plan.py:224`），涨跌停由撮合引擎按收益率比处理（`sim.py:227-236`）。合成库验证：没有 `price_limit_daily` 行时，20 只候选**全部** `reason=no_limit_row`、`orders=0`（模拟盘一笔都不成交）。

真实库影响有限：`rps_daily` 的 1285 个交易日**全部**有涨跌停行（实测 `rps_days_without_limit_rows = 0`），所以只在缺数据的新装机上暴露。

### 2.3 【严重】文档承诺的"停牌/涨停顺延"从不重试，委托永久卡死

`settle_day` 只捞**当天日期**的挂单：

```python
pending = conn.execute(
    "SELECT * FROM trade_order WHERE trade_date = ? AND status IN ('pending','submitted','part')"
    " AND plan_id IS NOT NULL AND broker_order_id >= ? ORDER BY id",
    (trade_date, ORDER_ID_BASE)).fetchall()      # broker/sim.py:181-184
```

顺延时**只 `deferred += 1`，状态保持 `pending`、`trade_date` 不变**（`sim.py:187-192` 停牌 / `:193-195` 涨跌停）。下一天再调 `settle_day` 时，查询按新日期过滤，这条旧日期的挂单**永远不会再被看到**。

**复现**（D1 停牌 → D2 恢复交易且价格可成交）：

```
day1(D1 suspended): {'filled': 0, 'deferred': 1, 'canceled': 0} status pending
day2(D2 tradable):  {'filled': 0, 'deferred': 0, 'canceled': 0} status pending  positions {}
expected: day2 filled=1（顺延委托应在恢复交易后被撮合）
```

**后果**：模块 docstring 与 README 都写明"订单顺延到下一个交易日，不撤单"（`broker/sim.py` 模块文档、`README.md:1139`），但实际是**既不成交也不撤销**——目标仓位永远差这一笔，且页面上只看到一条永久 `pending` 的委托。README 的实测表记录"0 笔因涨停或停牌顺延"，所以这个缺陷一直没被触发暴露。

### 2.4 【高】成交明细不落费用与已实现盈亏 —— 复盘在物理上无法还现

`trade_fill` 的列是 `(trade_date, broker_order_id, broker_trade_id, symbol, side, volume, price, traded_at)`（`db.py:256-267`），**没有 `fee`**；`_apply` 算出的费用只累加到 `totals["fee"]` 打屏一次（`sim.py:171,240-262`；`cli.py:892`）。平仓时 `avg_cost` 被直接 `pop` 抛弃（`sim.py:264-269`），**已实现盈亏没有任何落库**。

**后果**：事后无法回答"这笔赚了多少、扣了多少费"；只能靠重放——而重放正是 §2.1 会污染历史的操作。所以 P0-2 的复盘面板**必须先补落库**，否则只是把算不准的数字画出来。

### 2.5 【中】若干"看起来有、其实没有"的实现

| 项 | 现状 | 证据 |
|---|---|---|
| `paper_nav.filled_orders` | **死列**，永远 0（无任何写入方） | `db.py:311` vs 写入语句 `sim.py:349-353` |
| `MAX_ORDERS_PER_DAY = 200` | **定义了从不使用**（全仓唯一引用就是它自己） | `sim.py:40` |
| `summary()` 的曲线长度 | `nav_curve(limit=2000)` **静默截断**：超过 2000 个交易日后，`to`/收益/最大回撤都只基于**前 2000 行**，而 `curve` 数组也随之截断 | `sim.py:361-363` → `summary` `:377-401` |
| 成交量/流动性约束 | **无**：成交价恒等于限价、可无限量成交 | `sim.py:198-209` |
| 年化/波动/夏普/卡玛 | **无**：`compute_metrics` 只被回测调用 | `analytics/backtest.py:716-761` |
| 单账户硬约束 | `CHECK (id = 1)` + 处处 `WHERE id = 1`，**一个库只能有一条曲线**；对照 RPS vs 合成分必须毁掉前一条 | `db.py:286`、`sim.py:82,93`；`tmp/review/paper_runs.log` 自述"会清掉上面那次" |
| 批次可复现性 | `collect_log` 只存文案"模拟盘 N 次调仓，收益 X%"，**不存参数** | `cli.py:878-880` |
| 回滚/截断到某日 | **无**：只有整表 DELETE | `sim.py:116-120` |
| 从任意起始日重跑 | **不支持**：现金是当前单值，`load_account(date)` 只还原持仓 | `sim.py:91-107` |

### 2.6 【低】废弃缓存目录

`data/backtest_cache/` 为空目录，全仓无代码引用（`grep backtest_cache` 只命中 `api/backtest.py` 的进程内 `_CACHE` 与测试名）。可删。

### 2.7 ✅【严重 · 做 P0-3 时才暴露】部分卖出不减仓，账户凭空多出现金

`sim._apply` 的卖出分支只更新了 `avg_cost`，**没有把 `acc.positions[sym]` 改成剩余股数**
（只有"卖光"时才 `pop`）：

```python
    left = acc.positions.get(sym, 0) - got
    if left > 0:
        acc.avg_cost[sym] = (...)          # ← 只改成本，没改持仓
    else:
        acc.positions.pop(sym, None)
```

后果：账户仍以为自己握着卖掉的股数，下一次调仓照着虚高的数量**再卖一次**，
把并不存在的股票的现金记进账。**是先把"已实现盈亏落库"做出来、再用对账恒等式
（`Σ已实现 == 现金变动 + 期末持仓成本`）去卡，才把它抓出来的** —— 它不会让程序报错，
只会让净值悄悄变好看。

**实测（合成库）**：`000016` 买入 7800 → 卖 100 → 又卖 **7800**（多卖 100 股）。
修复前 41 个交易日的对账差异约 16.4 万元。

**真实库（只读重放，2026-10-16）**：126 笔成交里 **3 笔**超卖、共 **300 股**，
虚增现金约 **2.97 万元**（本金 100 万的 3.0%）。README 里那组 −24.46% 因此偏乐观，
**必须 `--reset` 重建才能得到真实数字**（重放旧成交无法还原：当时的目标股数本身就是
按错的持仓算出来的）。

修复：卖出时 `acc.positions[sym] = left`；回归测试
`test_partial_sell_reduces_held_position` + 对账恒等式
`test_realized_pnl_reconciles_with_cash_and_holdings`。

### 2.8 ⏳ 仍待做的（P1/P2 覆盖）

见 §3 的 P1/P2 —— 本节只列已确认的缺陷。

---

## 3. 升级清单

### P0-1 模拟盘续跑与每日推进（模拟持仓）

**目标**：`cli trade run` 可重复调用；每次只推进"上次结算日之后"的交易日；也可挂进一键更新。

**改动点**

| 位置 | 改动 |
|---|---|
| `broker/sim.py::run` | **已实现**：`run_start_date` 落 `paper_meta`；全局信号日序列的 `k % step` 决定调仓节奏（续跑才接得上）；`day <= last_settled` 一律跳过；续跑时补上"截断运行最后一天缺的那份计划"；`params`/`initial_cash` 与库中不一致则报 `SimError` 要求 `--reset` |
| `broker/sim.py` | **已实现**：新增 `meta_get` / `last_settled_date` / `_plan_exists` |
| `cli.py::cmd_trade_paper_run` | 打印"续跑自 X"，并把 `resumed_from`/`days` 报出来 |
| `cli.py::run_update_pipeline` | 新增可选的 `paper` 阶段（默认关，`cli update --paper` 打开），阶段名进 `App.vue` 的 `PHASES` |
| `broker/plan.py:236-241` | `no_limit_row` 改为仅 `check_limits` 时生效（顺带修 §2.2，已实现） |

**验收**
- 新增测试：同一合成库，`run(a→b)` + `run(a→c)` 的 `paper_nav`（cash/total/nav/n_positions）、`paper_position`、`trade_fill` 必须与**一次性** `run(a→c)` **逐值相等**。
- 新增测试：重复执行 `run(a→c)` 两次，第二次 `days == 0` 且曲线不变（幂等）。
- 新增测试：已有曲线时改 `period`/`threshold`/`cash` → 抛 `SimError`，提示 `--reset`。

**成本**：约半天（引擎已改完，主要是等价性测试与 CLI 收尾）。

**落地状态：✅ 已完成**（`broker/sim.py`：`run_start_date` / 全局 `k % step` 节奏 / 跳过已结算日 / 续跑补计划 / 参数与资金一致性校验 / 老库按默认值补齐 params / `from_date` 改为可选）。
测试 `backend/tests/test_paper_resume.py`（5 项：单次长跑 ≡ 截断+续跑、不改写已结算日、重复执行是空操作、参数漂移报错、老格式 params 可续跑）—— 已验证**对着修复前的 `sim.py` 会失败**。
另修 `broker/plan.py::build_plan` 的 `no_limit_row`（自建模拟盘不再要求实测涨跌停行），测试 `test_local_mode_does_not_require_limit_rows`。
CLI：`trade run` 打印"本次为续跑/新建"；新增 `cli update --paper`（阶段名 `paper`，已进 `App.vue` 的 `PHASES`）。

### P0-2 顺延委托必须真的重试（模拟持仓）

**目标**：停牌/涨跌停顺延的委托在下一个可交易日被撮合，而不是永久卡 `pending`。

**改动点**：`settle_day` 的挂单查询（`sim.py:187-190`）从"只看当天日期"改成"到期未成交的挂单"：

```sql
SELECT * FROM trade_order
WHERE status IN ('pending','submitted','part') AND plan_id IS NOT NULL
  AND broker_order_id >= ? AND trade_date <= ?
ORDER BY trade_date, id
```

并在顺延时**不改日期**（保持原 `trade_date`，靠 `<=` 捞出），成交/撤销时照旧改状态；`MAX_ORDERS_PER_DAY`（现在定义了从不使用）在循环里真正生效，防御性上限。

**验收**
- 新增测试（即 §2.3 的复现脚本）：D1 停牌顺延 → D2 恢复交易且价格可成交 → `filled == 1`、状态 `filled`、持仓到位。
- 新增测试：D1 涨停买不进 → D2、D3 仍拿不到 → D4 恢复后成交；全程不重复成交（幂等）。
- 新增测试：顺延跨过 `hold` 个信号日、期间发生调仓时，不产生重复或反向的重复委托。

**成本**：约半天。

**落地状态：✅ 已完成**（`settle_day` 改捞"到期未成交的全部挂单"，按原日期顺序补撮合；`MAX_ORDERS_PER_DAY` 真正生效作为防御上限）。
测试 `test_suspended_deferred_order_is_retried_next_day`、`test_limit_blocked_buy_keeps_retrying_until_tradable`（都已验证对着修复前会失败）。
真实库只读检查：**0 笔**卡住的挂单（7 撤销 / 126 成交），因此对既有数据无追溯影响。

### P0-3 成交落费用与已实现盈亏（复盘的前提）

**目标**：每笔成交都能回答"花了多少费"，每次平仓都能回答"这笔赚了多少、持有了几天"。这是 P0-4 复盘面板的**前置**，不先做这一步，面板上的数字就是错的。

**改动点**
- `trade_fill` 增列 `fee REAL NOT NULL DEFAULT 0`、`realized_pnl REAL`（平仓行填，买入行为 NULL）、`avg_cost REAL`（该笔平仓对应成本）。**用 `ALTER TABLE ADD COLUMN` 做幂等迁移**，挂进 `db.ensure_period_columns` 同款的迁移入口（`db.py:370-388` 是现成范式）。
- `sim._apply` 增列时把费用与平仓盈亏一并算出来（现在 `sim.py:240-269` 已经算好了 `fee` 与 `basis`，只是丢掉）；`settle_day` 写入时带上。
- `sim.review(conn)` 直接读这些列做 FIFO 汇总，不再重放。

**验收**：合成库构造 2 盈 1 亏三个回合，断言：每笔 `fee` 与 `costs.default_cost_model` 手算一致；三个回合的 `realized_pnl` 之和 == 期末现金 − 期初现金 + 期末持仓成本 − 期内买入金额（**对账恒等式**，任一列算错都会破）。

**成本**：1 天。

**落地状态：✅ 已完成**（`trade_fill` 增 `fee` / `realized_pnl` / `avg_cost`；`db.ensure_fill_columns` 幂等补列并挂进 `init_db`；`sim._apply` 返回并落库这三项）。
**这一步顺手抓到并修掉了 §2.7 的部分卖出不减仓缺陷** —— 对账恒等式 `Σ已实现 == 现金变动 + 期末持仓成本` 就是为它写的。
测试：`test_fill_persists_fee_and_realized_pnl`、`test_realized_pnl_reconciles_with_cash_and_holdings`、`test_partial_sell_reduces_held_position`、`test_ensure_fill_columns_backfills_old_db`、`test_init_db_adds_fill_columns_to_existing_db`。

### P0-4 模拟盘复盘面板（复盘 + 模拟持仓）

**目标**：除了净值曲线，还能回答"这笔到底怎么赚/亏的"。

**新增 `broker/sim.py::review(conn)`**

- **逐笔回合**：按 `trade_fill` 的 `traded_at`/`trade_date` 顺序，对每只票做 FIFO 买卖配对（买→卖为一个回合），产出：`symbol`、买入日、卖出日、股数、买入均价、卖出均价、毛盈亏、费用、**持有交易日数**、收益率。
- **汇总**：回合数、胜率、盈亏比（平均盈利/平均亏损）、平均持有天数、最大单笔盈利/亏损、累计费用、换手。
- **当前持仓**：浮动盈亏、持有天数、占净值比。
- **基准对照**：读 `benchmark_bar` 的 `000300`（回测已用同一张表，`analytics/backtest.py:557-560`），给出同期基准涨跌与**超额**。
- **行业暴露**：join `industry_map` 汇总当前持仓的行业分布。

**接口**：扩展 `GET /api/trade/paper`（`api/trade.py:228-238`）增加 `review` 字段；`GET /api/trade/paper?date=` 支持指定日（直接复用 `sim.positions_at(conn, date)` 与 `paper_nav` 的对应行）。

**页面**：`Trade.vue` 自建模拟盘面板下增：绩效卡（胜率/盈亏比/平均持有/费用/超额）、回撤曲线、逐笔回合表、行业分布；顶部加**日期下拉**（用 `/api/meta/dates`），回看任意历史日。

**验收**：合成库构造 3 个明确回合（2 盈 1 亏），断言胜率=2/3、平均持有天数、费用合计与手算一致；浏览器冒烟确认表格渲染与日期切换。

**成本**：1~1.5 天。

**落地状态：✅ 已完成**
- `broker/sim.py::review()`：FIFO 配对出逐笔回合（买入均价、持有交易日数、卖出费用、已实现盈亏）、绩效汇总（胜率/盈亏比/平均持有/累计费用/已实现/浮动/最大回撤）、当前持仓（含权重）、行业分布、基准对照（读 `benchmark_bar`，与回测同一张表）。
- 老库退路：迁移前的成交没有费用列时，按成交价重算并在 `notes` 里明确写"不含费用"。
- 接口：`GET /api/trade/paper?date=` 增加 `review` 与历史某日回看（`_paper_date` 就近回退、回显生效日）。
- 页面：`Trade.vue` 自建模拟盘面板新增「绩效复盘」卡、逐笔回合表、行业分布表、回看日期下拉（选项直接取曲线日期），并在净值曲线上标出回看日。
- 测试：`backend/tests/test_paper_review.py`（5 项，断言与 `trade_fill` 落库值一致）、`test_trade_paper_endpoint_supports_date_and_review`；浏览器冒烟已确认面板与日期回看（含 `?date=` 请求与生效日提示）。

### P1-3 个股按日锚定（复盘）

**目标**：`/stock/600519?date=2024-05-06` 看那天的 RPS、行业内名次、形态、当时窗口曲线。

**改动点**：`api/stock.py::detail(symbol, date=None)` 走 `resolve_date`（复用 `api/__init__.py:53-90`）；`curve(symbol, days, date=None)` 把 `ORDER BY date DESC LIMIT ?` 换成"以 `date` 为右端、向过去取 N 条"（`api/stock.py:216-222`）；`patterns` 同理。前端 `StockDetail.vue` 加日期下拉（与 `Rankings.vue:84-88` 同一套），并把 `days` 窗口与日期一起带进三个请求。

**验收**：`date` 落周末 → 就近回退并回显实际日期；`date` 早于上市日 → 明确空态；曲线右端 == 请求日。

**成本**：约 1 天。

**落地状态：✅ 已完成**
- `api/stock.py`：`detail(symbol, date=None)` / `curve(symbol, days, date=None)` / `patterns(symbol, days, date=None)` 全部先过 `resolve_date`（就近回退、回显生效日）。
- 曲线右端 = 指定日（不再继续画到最新）；形态表与**「此后最低价」**都以指定日为上界 —— 否则会用今天才知道的低点去判一周前的形态是否失效（偷看未来）；`current` 的判定基准日也跟着变。
- 曲线在指定日之前无数据时返回 404 且说明原因（不是空图）。
- 页面：个股页头部加「回看」下拉（选项来自 `/api/meta/dates`，与排行榜同一套），改动即重载三份数据；回看时明确提示"只到这一天，不掺未来数据"。
- 测试：`test_stock_detail_and_curve_follow_date`、`test_stock_patterns_do_not_look_ahead_when_reviewing`；浏览器冒烟已确认三个请求都带 `date=` 且头部显示生效日。

### P1-4 当日复盘页（复盘）

**目标**：一个页面对"某一天"做全局扫描：涨跌家数、涨停/跌停家数、涨停板清单（含连板）、行业/概念当日涨跌排行、当日策略信号名单（`entry_mask` 前 M 只）、模拟盘当日动作。

**改动点**：新增 `api/review.py`（`GET /api/review/day?date=`）；涨停数据直接从 `price_limit_daily` + `daily_bar.pct_chg` 算（口径与 `analytics/limits.limit_mask` 一致）；新增 `Review.vue` + 路由。板块当日涨跌 = `industry_rps_daily.ret{p}` 之外的**单日**涨幅，需要按 `industry_map` 聚合 `daily_bar.pct_chg`（等权），新写一个小函数即可。

**验收**：指定日的涨跌家数之和 == 当日有行情股票数；涨停家数与预警规则 `limit_up` 的命中数一致。

**成本**：2~3 天（新页面）。

**落地状态：✅ 已完成**
- 新增 `api/review.py`（`GET /api/review/day?date=&period=&threshold=&exclude_st=`）：市场宽度（分母=当日有 K 线且未停牌）、涨停板（含**连板数**）/跌停板、行业当日等权涨跌、当日信号名单。
- **涨停判据抽成 `analytics/limits.daily_limit_lists()`**，预警规则 `limit_up` 改为调用它 —— 复盘页与预警不可能再各写一套判据（`test_review_limit_list_matches_alert_rule` 锁住）。
- 信号名单只做"RPS 阈值 + ST 过滤"，响应里 `signals_note` 与页面都写明**不含**共振/择时（那两层在回测页）。
- 前端新增 `/review` 页 + 导航入口；日期/周期/阈值可切。
- 测试：`test_review_day_breadth_limits_and_signals`（宽度自洽、连板=2、行业排序、信号名单、非交易日回退）、`test_review_day_on_empty_db`；浏览器冒烟确认五个区块与切换日期发出的 `?date=`。

### P1-5 回测逐日持仓落库 + 多参数对比（工具）

**改动点**：`analytics/backtest.py` 的 `equity_curve` 补 `date` 逐日点与每期 `holdings_list`；新增 `backtest_run` 表按参数指纹存结果；`Backtest.vue` 支持"保存本次 / 叠加对比"。

**说明**：这是本轮唯一**需要新表**的项。若只想先要"导入导出对比"，可先做 JSON 导出（省一张表）。

**成本**：1~2 天。

**落地状态：◑ 主件已完成（按"省一张表"的路线）**
- ✅ **逐日净值**：`BacktestResult.daily_curve` —— 期间持仓按每日收盘价盯市，整段**锚定**在该期期末净值上（建仓值由期末值反推；直接乘 `(1-cost)` 会与公布口径差一个 `成本×收益` 交叉项，实测 0.015%）。新增指标 `max_drawdown_daily`，与调仓点口径**并存**且页面标注差别，**没有**替换 README 里已公布的数字。
- ✅ **每期持仓名单**：`equity_curve[].holdings_top`（上限 `HOLDINGS_TOP=50`；等权全市场模式下真实持仓有几千只）。
- ✅ **多参数对比**：前端内存表（最多 6 次并排）+ JSON 导出/导入，**不新增表**；跨会话留存仍需导出文件。
- ⏳ **未做**：`backtest_run` 落库（跨会话对比）。真需要时再按上面的设计加表。
- 顺带修掉一个**页面级缺陷**：新增的「调仓点」散点系列没在 `main.js` 注册 `ScatterChart`，ECharts 抛错会打断整个视图更新（连后面的对比表都不刷新）。在浏览器冒烟里发现并修复。

### P1-6 批次与多策略对照（模拟持仓 + 工具）

**目标**：不再"跑第二种策略就得毁掉第一种"。

**改动点**：`paper_account` 去掉 `CHECK (id = 1)` 语义改为 `account_id`（新表 `paper_run`：`id`、`name`、`run_start_date`、`params`、`initial_cash`、`created_at`），`paper_nav`/`paper_position`/`trade_order`/`trade_fill` 加 `run_id`。**这是破坏性迁移**，需要一次性把现有单账户数据归到 `run_id=1`；建议放在 P0 全部稳定之后再做。

**替代的最小方案**：不动表结构，只做"导出/导入 JSON"（把现有 four 张表按 `trade_date` 导出），先支持对照，不引入迁移风险。

**成本**：2~3 天（含迁移与回滚演练）。

**落地状态：◑ 已用"最小方案"落地（不迁移、不动既有表）**
- 新增**表** `paper_archive`（纯 `CREATE TABLE IF NOT EXISTS`，既有库自动获得）：存归档那一刻的净值曲线、`paper_meta.params`、期末权益/收益/回撤/天数、以及 `review().summary` + 基准超额。
- `sim.archive/list_archives/delete_archive`；CLI `trade archive [--save --label|--rm id]`；接口 `GET/POST /api/trade/paper/archive[s]`、`DELETE /api/trade/paper/archives/{id}`。
- 页面「交易 → 策略对照」：归档列表（参数摘要/收益/回撤/天数/区间）+ 勾选后与当前曲线叠加的净值图 + 页面上归档/删除。
- 测试 `test_paper_archive.py`（空账户报错、归档↔复盘指标一致、曲线原样保存、**reset 之后归档仍在**、自动命名含信号源）+ `test_paper_archive_endpoints`；浏览器冒烟确认表格、勾选联动与删除请求。
- ⏳ **仍然未做**：把四张表加 `run_id` 的破坏性迁移（多账户**并行**运行）。归档已经覆盖了"对照"这个真实需求，迁移留到确实要同时跑两条曲线时再做。

### P2 其余（工具）

- **涨停板 / 板块当日涨跌**：并入 P1-4。
- **导出中心**：`/api/trade/paper/export.csv`（净值+委托+成交）、回测结果 CSV/JSON —— 复用 `api/rps.py::_csv_bytes` 的 BOM/公式注入防护写法。
- **自选按日**：`api/watchlist.py:96-115` 加 `date` 参数。
- **风险工具**：持仓相关性矩阵、单票/行业集中度上限告警、回撤阈值推送（复用 `notify.py`）。
- **年化 / 波动 / 夏普 / 卡玛**：直接复用回测已有的 `analytics/backtest.py::compute_metrics`（`716-761`），别另写一套。
- **横截面筛选保存**：把筛选条件存一张小表 + 页面下拉。
- ✅ **小清理**（已完成）：`summary()` 现在给 `curve_limit` + `curve_truncated`，页面明确提示"只基于前 2000 天"（原先静默截断）；`paper_nav.filled_orders` 由 `mark_to_market` 真的写入当日成交笔数（原先永远是 0，等于假的"当日无成交"）；空的 `data/backtest_cache/` 已删除。（`MAX_ORDERS_PER_DAY` 已在 P0-2 启用。）
- ✅ **导出中心（模拟盘侧）**：`GET /api/trade/paper/export.csv?kind=nav|orders|fills` + 页面三个按钮；转义逻辑抽成 `api/__init__.py` 的 `csv_cell/csv_bytes/csv_response`，排行榜导出改为复用同一份。
- ✅ **自选按日**：`GET /api/watchlist?date=` + 页面日期下拉。
- ✅ **模拟盘年化/波动/夏普/卡玛**：`review().summary` 新增（逐日净值口径）+ 页面绩效卡。
- ✅ **风险工具（持仓体检）**：`sim.portfolio_risk()` —— 集中度（最大单票 / 前 5 合计 / 最大行业权重）与**两两相关性**（近 60 日收益，含最像的一对），已进 `/api/trade/paper` 的 `review.risk` 与页面「持仓体检」块；相关性用纯标准库实现（`_pearson`），失败/样本不足一律 `None`，不编造。
- ✅ **回测结果导出 + 跨会话保存**（本轮新增）：前端导出 CSV（逐日净值）/ JSON（完整结果）；新增表 `backtest_run`（纯新增）与 `POST /api/backtest/save`、`GET /api/backtest/saved[/{id}]`、`DELETE`，页面「已保存的回测」可保存/载入/删除，自动名带关键参数。
- ✅ **风险主动提醒**（本轮新增）：`broker/risk_alerts.py` —— 单票权重 / 行业权重 / 平均两两相关 / **当前**回撤四条阈值；`cli trade risk [--push|--dry-run|--max-*]`，页面顶部提示条 + 只读接口 `GET /api/trade/risk`。默认**不推送**（显式 `--push` 才发微信），阈值走 `RPSBOARD_RISK_*` 或命令行覆盖。
  顺带修一个口径不一致：`portfolio_risk()` 的权重分母原本是"持仓市值合计"，与它自己的文档、以及 `review().positions[].weight` 的"总权益"不一致 —— 半仓时会把每只票权重放大一倍。已改为总权益（新测试抓到的）。
- ⏳ **仍未做**：横截面筛选保存、撮合的成交量约束、多账户并行。
- **成交量/流动性约束**：给撮合加"参与率上限"（如不超过当日成交额的 X%），否则对流动性差的票会高估可成交量。这是**保守性原则**的延伸，不是新功能。
- **接更多数据源**：`sources/base.py` 已是适配器模式，但基本面数据（财务/股本）需要新源；且 README 已记录多个源被评估后删除（`config.py:113-119`），引入前要先确认可达性。

---

## 4. 关键设计决策（实现时不要再重新讨论）

1. **续跑的语义**：`run()` 是**幂等推进**，不是"重算区间"。曲线的起点由 `paper_meta.run_start_date` 固定；调仓节奏按**全局信号日序号**取模，绝不按本次运行的局部序号。参数或初始资金与已有曲线不一致 → 报错要求 `--reset`，**不允许静默混算**。
2. **复盘指标的配对规则**：按**进阶先出（FIFO）**配对同一 symbol 的买卖成交；跨调仓周期不清零 —— 因为模拟盘的持仓是"绝对目标股数"，同一只票可能被加仓/减仓多轮。费用按成交笔逐笔归集，不摊到回合外。
3. **日期锚定的统一约定**：所有"按日"接口一律先过 `resolve_date`（非交易日在**就近**回退，而不是回退到最新日，`api/__init__.py:56-63` 已写明理由），响应里必须回显**实际生效日期**。
4. **涨跌停口径唯一真源**是 `analytics/limits.py`（`NO_LIMIT_STATUSES` + `limit_mask`）；任何新功能算涨跌停都必须复用它，不能各自写阈值。
5. **界面上的"日"必须可见**：任何锁死在"最新交易日"的接口都要在页面上明说，不能让人以为看到的是所选日期。

---

## 5. 明确不做

- **实盘交易**：项目刻意只有 `paper` / `local` / `sim`（`config.py:180-184`），没有 `live` 分支。这条边界不改。
- **基本面/财务数据**：唯一数据源 eltdx 是行情协议；引入需要新数据源与合规确认。
- **分钟级 / Level2 行情**：现有全部统计口径建立在日K上。
- **把模拟盘做成"真实成交价"**：撮合刻意保守（限价不成交就真的不成交），这是**设计目标**不是缺陷（`broker/sim.py` docstring）。

---

## 6. 建议执行顺序

```
P0-1 续跑 + 每日推进                      ✅ 已完成（含等价性/幂等回归测试）
P0-2 顺延委托重试                          ✅ 已完成
P0-3 成交落费用与已实现盈亏                ✅ 已完成（并修掉「部分卖出不减仓」）
P0-4 模拟盘复盘面板（review/接口/页面/回看）✅ 已完成
P1-3 个股按日锚定                          ✅ 已完成（详情/曲线/形态都支持 ?date=）
P1-4 当日复盘页                            ✅ 已完成
P1-5 回测逐日 + 对比                       ◑ 已完成主体（落库留待需要时做）
P1-6 批次与多策略对照                      ◑ 已用归档表落地（不做破坏性迁移）
P2   小清理 / 导出中心 / 自选按日 / 风险工具 / 筛选保存 / 流动性约束 / 年化夏普 ⏳
```

每项完成后跑：`pytest backend/tests -q --cov-fail-under=50`、`ruff check backend`、`npm --prefix frontend test`、`npm --prefix frontend run build`，并对改动的页面做真实浏览器冒烟。
