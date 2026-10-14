# A股 RPS 看板

基于**欧奈尔相对价格强度（RPS, Relative Price Strength）**的 A 股选股与研究看板。

RPS 把全市场个股按区间涨幅做**横截面百分位排名**：RPS=90 表示这只票跑赢了全市场 90% 的股票。
欧奈尔在《笑傲股市》中指出，历年表现最好的股票在启动主升浪**之前**，RPS 就已经高于 80~90。

## 功能

| 模块 | 说明 |
|---|---|
| **RPS 排行榜** | 全市场 RPS15/20/60/120/250 排名，可按阈值、行业、成交额、ST 组合筛选，支持分页与**全量 CSV 导出** |
| **历史回看** | 所有页面均可选择任意历史交易日回看；非交易日自动**就近**回退到最近的有数据交易日 |
| **Δ名次** | 与 N 个交易日前的名次对比，看「这只票是最近在走强还是掉队」 |
| **板块强弱** | **证监会行业 / 概念题材**两套口径，等权涨幅的横截面 RPS + 领涨股 + 行业×周期热力图（**行按当前周期独立排序**） |
| **个股详情** | 单只股票的多周期 RPS 曲线 vs 后复权价格双轴图、跑赢全市场分位、所属行业强弱 |
| **策略回测** | 净值 vs 沪深300、风险指标、**RPS 分档前瞻收益**（自动解读单调方向）、**大盘择时**、**交易成本**、**涨跌停可交易性** |
| **自选预警** | 自选股池（行首 ☆ 即加入）+ 条件预警规则（RPS 阈值 / 三升底 / 涨停）与命中记录；命中可**手动**推送到微信 |
| **性能监测** | 后端 ASGI 中间件逐请求计时（`x-perf-ms`）+ 前端上报，两端在同一接口里对照；详见「性能监测点」 |
| **一键更新** | 页面按钮触发后台线程串起「日K → 涨跌停 → 行业分类（仅空表）→ RPS → 形态 → 预警」，实时显示阶段与失败原因；命令行入口 `update.bat` |

## 技术栈

- **后端**：Python 3.12 · FastAPI · pandas · SQLite · eltdx（通达信行情协议，唯一数据源）
- **前端**：Vue 3 · Vite · ECharts · vue-router
- **数据源**：eltdx（通达信行情协议，TCP 直连，全市场 209 秒）——其余候选源已评估后删除

## 快速开始

### 1. 环境

需要 [uv](https://docs.astral.sh/uv/)（本机已装 0.12.24）和 Node.js 18+。
Python 需要 **3.12**（`pyproject.toml` 的 `requires-python` 与 `.python-version` 一致）。

```powershell
cd <克隆到本地的目录>
uv sync --extra dev            # 按 uv.lock 装出可复现的环境（推荐）
# 等价写法：uv venv --python 3.12 && uv pip install -e ".[dev]"
```

### 2. 初始化与采集

```powershell
# 建库
.venv\Scripts\python.exe -m rpsboard.cli init

# 回填历史日K（eltdx 全市场 5580 只 × 800 根，实测 209 秒）
.venv\Scripts\python.exe -m rpsboard.cli backfill --workers 8

# 计算个股 + 板块 RPS
.venv\Scripts\python.exe -m rpsboard.cli compute-rps

# 查看数据状态
.venv\Scripts\python.exe -m rpsboard.cli status
```

下列步骤按需执行（`cli update` 会自动接上后面几步；概念板块需单独采集一次）：

```powershell
# 概念/题材板块：采集成分股（约 31 分钟，只需一次）+ 概念 RPS
.venv\Scripts\python.exe -m rpsboard.cli concept
.venv\Scripts\python.exe -m rpsboard.cli concept --compute-only   # 之后只重算 RPS

# 三升底形态
.venv\Scripts\python.exe -m rpsboard.cli patterns

# 复权因子异常检测（只报告不改口径；--offline 可跳过联网）
.venv\Scripts\python.exe -m rpsboard.cli quality

# 自选与预警（详见「自选股池与条件预警」一节）
.venv\Scripts\python.exe -m rpsboard.cli watch ls
.venv\Scripts\python.exe -m rpsboard.cli alert run
```

命令行回测：

```powershell
.venv\Scripts\python.exe -m rpsboard.cli backtest --period 60 --threshold 80 --hold 20
```

### 3. 启动

```powershell
# 终端 1：后端
.venv\Scripts\python.exe -m uvicorn rpsboard.api:app --app-dir backend --port 8000

# 终端 2：前端
cd frontend
npm install
npm run dev
```

或直接双击 **`start.bat`** 一键拉起。

- 前端：http://localhost:5173
- 接口文档：http://localhost:8000/docs

#### 非 Windows（Linux / macOS）

命令相同，只是解释器路径与脚本后缀不同：

```bash
# 终端 1：后端
.venv/bin/python -m uvicorn rpsboard.api:app --app-dir backend --port 8000

# 终端 2：前端
cd frontend && npm install && npm run dev
```

或 `./start.sh`（一键启动）、`./update.sh`（一键更新数据）。

> 想让后端单端口托管前端（不必再起 vite dev）：`cd frontend && npm run build`，
> 之后只开后端，直接访问 http://127.0.0.1:8000/ 即可。

### 4. 已有库升级（拉取新版本后跑一次）

**只跑 `cli init` 不会补出新周期列**：三张表的 DDL 都是 `CREATE TABLE IF NOT EXISTS`，
对已存在的表什么都不做。因此 `init_db()` 里挂了一个幂等迁移
（`db.ensure_period_columns()`）：按 `config.PERIODS` 缺哪列补哪列，并重建
`rps_daily` 的覆盖索引（`CREATE INDEX IF NOT EXISTS` 不会更新同名旧索引，必须
先 drop），**只做 `ALTER TABLE ADD COLUMN`，不重建表、不动数据**。实测 1.45 GB 的库
**7 秒**完成。

```powershell
.venv\Scripts\python.exe -m rpsboard.cli init                  # 补列 + 重建索引，会打印新增了哪些列
.venv\Scripts\python.exe -m rpsboard.cli tradestatus           # 一次性：修长假误判（只写真正变化的行）
.venv\Scripts\python.exe -m rpsboard.cli compute-rps --full     # 给新列填值（必须全量，增量只补新交易日）
.venv\Scripts\python.exe -m rpsboard.cli concept --compute-only --full   # 概念表同理
.venv\Scripts\python.exe -m rpsboard.cli quality               # 复权因子异常检测（官方因子接口约 20 秒）
.venv\Scripts\python.exe -m rpsboard.cli price-limits --fill --workers 6  # 可选：后台续采涨跌停实测值
```

跑之前在迁移/重算前**停掉 8000 端口上的服务**：API 持有只读连接，迁移时容易被
`database is locked` 挡住。

前端构建产物是**内容哈希**命名的（`/assets/index-<hash>.js`），但 `index.html` 本身
没有 `Cache-Control`：重新构建后，已经打开着的页面可能仍在跑浏览器缓存里的旧包
（实测踩过一次：页面行为与代码不符，其实是在跑昨天的 bundle）。升级后若行为异常，
**先 Ctrl+F5 强制刷新**再排查。

## RPS 计算口径

```
ret_N(t) = close(t) / close(t - N) - 1
rps_N(t) = ret_N 在 t 日全部有效股票中的百分位排名 × 100     # 榜首 = 100
```

- **后复权价**计算（eltdx `bars.get(..., adjust="hfq")`），规避除权除息造成的假跌幅
- 周期：**15 / 20 / 60 / 120 / 250 日**（15 = 3 周短线；250 日即欧奈尔原定义的 52 周）
- **新股自动排除**：上市不足 N 日 → `shift(N)` 为 NaN → RPS 为 NaN
  （15 日周期是其中门槛最低的一档：上市满 16 个交易日的股票就会**首次进榜**，
  此前四周期全为 NaN 被丢弃）
- **停牌日没有 bar，因此没有行**；价格用前值填充，停牌期间的区间收益仍然照算。
  `tradestatus=0` 的语义是「这根 bar 之前缺了交易日（**复牌当天**）」：复牌日本身是
  真实成交日（有量、有涨跌），**照常参与排名**（实测 1,299 个复牌股票日，其中 987 个
  因此进入了 RPS）。停牌判定按**交易日**间隔，长假不算停牌 —— 历史实现按自然日判定，
  把 21 个长假后首个交易日整天误判成停牌 → 那 21 天没有任何 RPS，2026-10-13 已修
  （见 `cli tradestatus`）。`tradestatus` 只保留作数据质量标记，不再参与 RPS 掩码。
- **异常端点不进入收益率（自动剔除）**：后复权价 `≤0`（穿零）或 `< 1 元`（门槛见
  `config.ANOMALY_MIN_PRICE`，环境变量 `RPSBOARD_ANOMALY_MIN_PRICE` 可调，0 = 只剔非正价）
  的端点一律不算。两个理由：① 负分母会翻转比值符号、0 分母得 ±inf，而排名把 +inf 当成
  最大值（实测 600076 的 `ret120` 曾是 −12725%）；② 接近零的分母会把**正常的原始价涨幅
  放大成几百个百分点** —— 同一只股票在 2024-12-18 的 `ret60` 曾是 **+998%**，而它当天的
  原始价只涨了约 8%。区间收益只取决于两个端点，所以按端点剔除，不是「跨过异常日整段作废」；
  规则由 `matrix.build_close_matrix` 统一执行，RPS、回测前瞻收益、涨跌停判定共用同一套端点。
  实测被剔除的端点：**394 行**（`0<close<1 元` 371 + 穿零 23，涉及 6 只股票；
  恰好等于 1.00 元整的行保留）。
- **横截面不足 2 个成员的日期不落库**：只有 1 只股票时百分位恒等于 100，那不是「强度」，
  只是没有可比对象（实测 41 个爬坡期日期因此被剔除，见「数据规模」）。
- **ST 默认剔除**，可在界面放开

### 无未来函数

RPS 只使用 ≤ t 的收盘价，由 `shift(N)` 的结构保证。测试用**扰动 t 之后全部行情、
断言 t 日 RPS 与选股结果逐值不变**来锁死这一点：

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/test_rps.py -k lookahead
.venv\Scripts\python.exe -m pytest backend/tests/test_backtest.py -k signal_stability
```

### 界面口径说明（2026-10-14 修订）

- **排行榜排序与分页**：分页由**排序键本身**决定。`sort=rps|ret` 时能在 rps_daily
  的覆盖索引上先取 Top-N（快路径），其余排序键（成交额/收盘价/涨跌幅）必须先 JOIN
  再排序分页。历史实现让内层子查询固定带 `LIMIT`，于是非 rps 排序取到的是
  「索引扫描顺序那一批」——实测 `sort=amount&limit=200` 的集合里
  `MAX(rps15)=3.75`（全市场倒数 3.75%），页面与 CSV 导出互相矛盾。现在页面第 1 页
  与导出的前 N 行逐行一致（`tests/test_api.py` 里两者互相断言）。
  页面的「排序」控件给出 **RPS / 区间涨幅 / 当日涨跌 / 成交额 / 收盘价 × 降序/升序**，
  按 `sort` + `direction` 两个参数传给后端（`direction` 缺省时按排序键的默认方向：
  涨跌幅/收盘价默认**升序**，所以想直接看涨幅榜要选降序）；导出与页面同参，
  可以直接用 `?sort=amount&direction=desc` 这样的链接分享一个榜单。
- **`market_percentile`（个股页「跑赢全市场」）**：= 该周期 RPS ≤ 我的股票占比，
  即全市场第一显示 100.0%。旧实现算的是「≥ 我」的占比，语义正好反过来
  （第一名显示 0.0%）。某个周期为 NULL 时该字段返回 `null`，不再恒为 0.0。
- **`pattern_daily.strength`（三升底）**：= **第三根收盘 / 第一根开盘 − 1**（三根阳线的
  合计涨幅）。历史实现的分母误用了第三根自己的开盘，于是 strength 恒等于第三根的
  自身涨幅（系统性偏小）。`cli patterns` 重算后全量回填（实测逐值核对：
  随机抽 30 条与 `close(T)/open(T-2)-1` 完全一致）。
- **形态「是否仍然有效」**：只看**截至查询日**的 K 线最低价有没有跌破第一根的
  `base_low`。旧实现的历史查询会读到查询日之后的行情，实测 2026-06-24~30 窗口
  364 条命中里 196 条（54%）的有效性标记是用了未来数据的错值。
- **CSV 导出**：「代码」列写成 `="000001"` 形式，Excel 打开时前导零不会丢；纯文本
  消费方会看到这层公式包装。以 `= + - @` 开头的文本（股票名等）会被加上 `'` 前缀，
  防 CSV 公式注入。字段用 `csv.writer`（`QUOTE_MINIMAL` + CRLF）输出，名称里带
  逗号/引号也不会串列。

## 重要限制

- **采集需要联网**：唯一数据源 eltdx 走通达信行情协议 TCP 直连（不走网页接口），
  离线时只能看已有数据。
- **首次初始化较慢**：`cli backfill` 拉全市场约 5200 只 × 800 根日 K，实测约 12~20 分钟；
  之后日常 `update` 是增量，约 4~5 分钟。
- **本机单人工具、无鉴权**：接口不校验身份，默认只监听 `127.0.0.1`，**不要暴露到公网**。
- **一键脚本分平台**：Windows 用 `start.bat` / `update.bat`，Linux / macOS 用
  `./start.sh` / `./update.sh`。

## 数据源

### 唯一数据源：eltdx（通达信行情协议）—— TCP 直连，不走网页

```bash
.venv\Scripts\python.exe -m rpsboard.cli update     # 日K + 涨跌停 + RPS
```

> eltdx 已声明在依赖里（`eltdx>=3.2.3`，见 `pyproject.toml`），
> `uv pip install -e ".[dev]"` 会一起装上，无需单独安装。

2026-10-12 实测曾评估五个源，最终**只保留 eltdx**：

| 源 | 全市场耗时 | 反爬风险 | 处置 |
|---|---|---|---|
| **eltdx** | **209 秒** | 无（TCP 协议） | **唯一保留** |
| 腾讯 | 约 2 分钟 | 12 线程并发直接触发 `waf.tencent.com` 封禁 | 已删除 |
| baostock | 约 70 分钟 | 多进程并发登录返回 `10001001 用户未登录` | 已删除 |
| 本地 .day | 22 秒 | 无 | 已删除（不复权、数据滞后一月，eltdx 自带成交额） |
| akshare/东财 | — | 本机网络阻断 | 已删除 |

> **代价是没有退路了**。eltdx 接口若变更，需从 git 历史取回适配器：
> `git show <commit>:backend/rpsboard/sources/baostock_src.py`。
> 这个取舍是明确知道的，不是疏漏。

删掉的适配器连带清掉了一批**永远为空的字段**：akshare 快照（量比/总市值/流通市值）
与通达信不提供的换手率（实测 `daily_bar.turn` 非空行数 0 / 427 万）。
按「**宁可没有，不要恒为 `—` 的假数据**」的原则，这些列已从接口与界面移除，
`spot_snapshot` 表也一并移除。

**为什么是 eltdx**：走通达信行情协议 TCP 直连，不是网页爬虫，
因此**没有 IP 限流、没有 WAF、没有代理问题**，且单次 `bars.get` 的 count 上限 800
（约 3.3 年，足够 RPS250 预热）。

除日 K 外，还用同一协议提供：

| 能力 | 接口 | 实测耗时 |
|---|---|---|
| 涨跌停价 | `daily_price_limits(trade_date=)` | 31~35 秒 / 5580 条，**服务端无缓存** |
| 当日停牌 | `latest_suspended()` | 0.0 秒 |
| 板块清单 | `board_quotes()` | 79 秒 |
| 板块成分股 | `board_member_quotes(code)` | 3.7 秒 / 板块 |


数据源在 `backend/rpsboard/config.py` 的 `DATA_SOURCE` 指定；当前唯一实现是 eltdx
（其余适配器已删除，可从 git 历史取回）。

### 各源实测对比（本机，2026-10）

| 方案 | 全市场耗时 | 复权 | 数据新鲜度 | 状态 |
|---|---|---|---|---|
| **eltdx（通达信协议）** | **209 秒** | ✅ 后复权 | 2026-10-12 ✅ | 主源 |
| 腾讯财经日 K | ~2 分钟 | ✅ 后复权 | 2026-10-12 | 已删除：**高并发触发 WAF 封禁** |
| baostock | ~70 分钟 | ✅ 后复权 | 2026-10-09 | 已删除：**多进程并发登录返回 10001001「用户未登录」** |
| 本地通达信（`RPSBOARD_TDX_DIR`，默认 `D:\tdx`）`.day` | 22 秒 | ❌ 不复权 | 2026-09-04 ⚠️ | 仅用于补成交额 |
| akshare / 东方财富 | — | — | — | 已删除：本机 **网络阻断**（ProxyError / RemoteDisconnected） |

### 成交额从哪来

eltdx 的日 K 自带成交额（`amount`），无需额外处理。
（早期版本曾用**本地通达信 `.day` 文件**给不提供成交额的源补全，那条路径随
`tdx_local_src` 适配器一起删除了，可从 git 历史取回。）

### 数据规模（本机实测）

```
证券清单   5580 只（沪深北）
日 K       4,282,855 行（后复权，800 根/只，2023-02-13 ~ 2026-10-13，888 个交易日）
RPS        4,041,317 行（15/20/60/120/250 五周期，832 个交易日）
板块 RPS   66,154 行（83 个证监会行业）
概念 RPS   157,678 行（202 个概念）
涨跌停     92 万+ 行（实测 165/888 个交易日，持续用 --fill 续采）
复权异常   9,413 行（internal 527 + official 8,886，见「复权因子异常」）
三升底     130,257 条（strength = 第三根收盘 / 第一根开盘 − 1）
基准指数   沪深300 / 上证 / 深证成指，各 800 根
```

> **2026-10-14 的口径修复**（`cli compute-rps --full` 重算，逐值核对后落库）：
> 1. **复牌日参与排名**：`tradestatus=0` 只是「这根 bar 之前缺了交易日」，复牌日本身
>    是真实成交日。实测 1,299 个复牌股票日里 **987 个**因此进入 RPS（另 302 个是
>    ST、被默认剔除的 `exclude_st` 挡掉，10 个落在下面被剔除的爬坡期日期上）。
> 2. **非正后复权价置空**：3 只股票 23 行 `close<=0` 不再进入收益率。库内
>    `ret15 < -1` 的行从 **4 → 0**；行业表 `|ret|>3` 从 **11 → 7 行**，且剩下这 7 行
>    全部是 `member_count=1` 的**单成员行业**（那个行业指数就等于这一只股票的真实
>    涨幅，不是脏数据；被污染的 `C20木材加工` 已经干净）。同一原因下 600076 的
>    `ret120` 从 **−12725% → NULL**。
> 3. **横截面不足 2 个成员的日期不落库**：交易日 **873 → 832 个**，少掉的 41 个是
>    行情窗口爬坡期（每天只有 1~2 只股票有 RPS，百分位恒等于 100）。行业表同步
>    从 66,193 行降到 66,154 行，概念表 41 个单概念日期同样剔除
>    （157,719 → 157,678 行）。
> 4. `RET15<-1`/±inf 的清洗同时覆盖 `industry_rps_daily`、`concept_rps_daily`。
>
> **2026-10-13 的两次数据变更**（保留备查）：① 加入 15 日周期 **+26,701 行**；
> ② 修 `tradestatus` 的长假误判：按 (date, symbol) 计 +103,113 / −193 行。

### 复权因子异常（`cli quality`）

后复权价 = 原始价 × scale + offset，而 **offset 可以是负数**。当原始价接近
「变换零点」（`-offset / scale`）时，后复权价会被压成极小值甚至**穿零**，
而百分比收益在接近零的价位上会被放大到失真：

- 实测 **6 只**股票的库内后复权价 < 1 元（371 行），其中 **23 行 `close <= 0`**
  （RPS 已按无效置空，见「RPS 计算口径」）；
- 600076 在 2024-12-18 的 `ret60 = +998%` 就是这么来的：同样的原始价涨幅
  （3.7 → 4.0，**+8%**）在这个价位上被放大成 +189%；它的变换零点≈原始价 1.27 元。

`cli quality` 把这件事摆到台面上（**只报告，不改 RPS 口径**）：

| source | kind | 判据（全部可复算） |
|---|---|---|
| internal | `hfq_jump` | 库内单日后复权收益超出任何板块的涨跌停上限，且不是上市前 5 日、不是复牌日 |
| internal | `tiny_price` / `nonpositive_price` | 库内 `0<close<1` / `close<=0` 的每一行 |
| official | `factor_jump` | 官方复权因子相邻档位 `hfq_scale` 变化 > 30%（除权/送转） |
| official | `negative_offset` | 负偏移档位，**且该股在库内价格确实被压低**（否则 789 只里绝大多数根本没受影响） |

实测一次全市场检测（2026-10-14）：**可疑跳变 106 处 / 极小价 371 行 / 非正价 23 行 /
负偏移 5 只 / 因子跳变 8,875 处**，官方因子接口整轮 5,574 只只需约 6 秒
（75 只/批）。结果进 `adjust_event` 表，`cli status` 显示一行汇总，
`cli compute-rps` 结束时也会提醒一句。

```powershell
.venv\Scripts\python.exe -m rpsboard.cli quality              # 离线扫描 + 官方因子，写库
.venv\Scripts\python.exe -m rpsboard.cli quality --offline    # 只做离线扫描（不联网）
.venv\Scripts\python.exe -m rpsboard.cli quality --dry-run    # 只打印，不写库
```

> **自动剔除的边界（2026-10-14 起）**：
> - **已自动**：端点价低于 `config.ANOMALY_MIN_PRICE`（默认 1 元）时该区间收益直接不算
>   （见「RPS 计算口径」的「异常端点」一条，全库 394 行）。600076 在 2024-12-18 的
>   `ret60 = +998%` 因此在重算后变成 NULL。
> - **仍未自动**：端点价都正常、只是窗口**跨过**了负偏移档位或因子跳变日的收益。
>   这类窗口是「真实价格涨幅 + 送转成分」的混合，按什么比例裁剪是**判断**而不是事实，
>   自动处理会静默改变已公布的回测结论。要按符号/日期排查，直接查 `adjust_event`。
>
> 门槛可用环境变量调保守一些（例如 `RPSBOARD_ANOMALY_MIN_PRICE=2.0`），
> 改完需要重跑一次 `cli compute-rps --full` 才会生效。

### 其它限制

- **不要用「超时后放弃的线程」测数据源速度**：被放弃的线程仍持有连接，
  会把后续查询一起拖死，产生"查询极慢"的假象。要测量就用独立进程单发探测。
- **回测存在幸存者偏差**：用当前证券池回溯历史，中途退市的股票缺失，结果偏乐观。
  回测页面已常驻标注，**不可直接用于实盘决策**。
- **仅供研究学习，不构成投资建议**。

## 回测结论（实测，务必先读）

在 **2023-07-11 ~ 2026-08-27（39 个调仓点 × 持有 20 个交易日，5574 只全市场）** 上跑
「RPS60 达标即等权买入、持有 20 个交易日」（零成本、涨跌停过滤**全区间生效**）：

| 策略 | 总收益 | 年化 | 最大回撤 | 夏普 |
|---|---|---|---|---|
| RPS60 ≥ 80 | +5.49% | +1.72% | −33.46% | 0.28 |
| RPS60 ≥ 90 | −6.96% | −2.28% | −39.55% | −0.33 |
| 沪深300 基准 | +11.30% | +3.28% | — | — |

分档前瞻收益（369 万样本，**单调向下**）：

| RPS 档 | 样本 | 平均 20 日前瞻收益 | 上涨概率 |
|---|---|---|---|
| 90–100 | 370,058 | **+0.71%** | 43.2% |
| 80–90 | 369,511 | +1.07% | 46.3% |
| 70–80 | 369,454 | +1.16% | 47.5% |
| 50–70 | 738,998 | +1.34% | 49.5% |
| 0–50 | 1,846,808 | **+1.82%** | 51.0% |

**这不是 bug，是真实的短期反转效应**（样本百万级、单调性明确）。要正确理解：

- 欧奈尔的 RPS 是**选股工具**而非**买卖时点**，他用的是 **52 周（年）**相对强度，
  配合 CANSLIM 的基本面（C/A/N/S）与流动性（L）条件，且**只在强势市场做多**。
- 本项目回测是**纯价格**的 60 日 RPS、等权买入、20 日固定持有、
  **全年无择时、无止损、无基本面过滤**——两者不是同一个东西。
- 因此正确结论是：**60 日 RPS 在 20 日持有期上表现为反转，不宜单独作为买入信号**，
  而不是「RPS 无效」。想复现欧奈尔的思路，应改用 RPS250 窗口、
  加入市场择时（如仅在指数站上均线时开仓）与基本面过滤。

> 本节数字在 **2026-10-14** 第三次重测。口径改动：
> ① **数据侧**：复牌日参与排名 + 横截面不足 2 个成员的日期剔除（见「数据规模」）；
> ② **回测引擎侧**：年化改为按**首末调仓点的真实日期跨度**折算（旧实现按
> 「点数 × 调仓周期 ÷ 252」，被跳过的前几个调仓点会让年化系统性偏高约 7%）；
> `rebalance < hold` 的重叠持仓不再逐段复利（那等于把同一段行情重复计入，现在回落为
> `hold` 并在 `warnings` 里写明）；`period` 必须在 `config.PERIODS` 内、`hold` 夹在
> `[1, 250]`；成本口径说明按**实际持仓数的中位数**生成（不再写默认的 50 只）。
> ③ **涨跌停过滤改为全区间生效**：实测覆盖的交易日之外，按板块规则推算比例
> （与实测逐行一致率 100%，见「真实可交易性」）。这一条把零成本口径从 +4.60% 抬到
> **+5.48%**：以前未覆盖的日子**整段不筛涨停**，等于允许买入当天已经封板的股票。
> ④ **复权异常端点自动剔除**：端点价 `< 1 元`（含穿零）的区间收益不算
> （全库 394 行、6 只股票，见「RPS 计算口径」）—— 上表是这一步之后的数字，
> 分档样本因此各少 100~200 条，聚合指标只动了小数点后第二位（6 只股票，
> 占比可忽略），但口径是干净的。

**多周期共振口径**：回测页勾选「多周期共振」后，入池条件是「除所选周期外的
**全部**周期同时 ≥ 阈值」。周期清单直接取自 `config.PERIODS`，因此 **15 日短线
也在共振判定里**，新增周期会自动进入共振，不需要改代码（`analytics/backtest.py`
的 `resonance_mask()`）。

回测页面会自动识别单调方向并给出上述解读，不会把负面结果藏起来。

### 计入交易成本后（2026-10-14 实测）

上一节的 +5.48% 是**零摩擦**结果。本项目原先的回测里
佣金 / 印花税 / 过户费 / 滑点**全部为零**——一个每 20 个交易日全仓换手、
3 年 39 次调仓的策略，真实执行里摩擦远不止于此。

| 口径 | 总收益 | 年化 | 最大回撤 | 夏普 |
|---|---|---|---|---|
| 零成本 | +5.49% | +1.72% | −33.46% | 0.28 |
| **含交易成本** | **−31.03%** | **−11.19%** | **−38.00%** | **−1.82** |
| 含成本 + 沪深300 > MA50 | −5.10% | −1.44% | −28.59% | −0.48 |
| 含成本 + 沪深300 > MA200 | −37.68% | −12.28% | −37.68% | −2.85 |

**加上成本，结论直接翻负。** 这不是新发现的坏消息，而是把一个
「本来就不成立」的结果修正到它本来的样子——零成本回测给出 +5.49%，
但你实际执行它会亏掉三成。

> 零成本口径从早期（涨停过滤还没生效时）的 +5.92% 到现在的 +5.49%，中间经历五次修正：
> ① **涨停过滤现在真的生效了**（旧实现把 `pct_chg` 小数去比硬编码阈值 `9.8` 百分数，
> 量纲错两个数量级，实测从未命中过任何一只股票）；② 2026-10-13 修掉 tradestatus
> 长假误判后多出 23 个可交易日；③ 2026-10-14 复牌日参与排名 + 单成员日期剔除
> + 年化按真实日期跨度折算；④ 同日的涨跌停过滤全区间生效（+4.60% → +5.48%）；
> ⑤ 复权异常端点自动剔除（+5.49%）。

**成本口径**（`backend/rpsboard/analytics/costs.py`）：

| 项 | 费率 | 方向 |
|---|---|---|
| 佣金 | 0.025%，**单笔最低 5 元** | 买 + 卖 |
| 印花税 | 2023-08-28 前 0.1%，之后 0.05% | 仅卖 |
| 过户费 | 0.001% | 买 + 卖 |
| 滑点 | 0.05% | 买 + 卖 |

**最容易被低估的是最低佣金**：等权分散持有上千只时，100 万本金摊到每只
才 1000 元，按比例算佣金只有 0.25 元，但券商按 5 元起收——**实际费率是名义值的 20 倍**。
成本因此按「组合名义金额 × 实际持仓只数」逐笔计算，而不是套一个常数比例。
本机实测这一次回测（RPS60 ≥ 80 / 持有 20 日）的**持仓数中位数是 1016 只**，
页面 `params.cost.assume_positions` 就是用这个实测值生成的（不再固定写 50 只）。

**分档前瞻收益那张图仍然不含成本**：它衡量的是「RPS 信号本身有没有预测力」，
混进执行摩擦会把信号质量与交易成本两件事搅在一起。页面已显式标注两者口径不同。

### 真实可交易性：涨停买不进、跌停卖不掉

| 情形 | 处理 |
|---|---|
| 买入日涨停 | 从当日候选池**剔除**（涨停买不进） |
| 卖出日跌停 / 停牌 | 该持仓收益**顺延**到下一可卖出调仓点（保守，可能低估回撤） |

实测一次完整回测（RPS60 ≥ 80 / 持有 20 日 / 含成本）有 **384 个持仓**触发顺延，
占 **36,188 个持仓的 1.06%**（2026-10-14 重测）。这个数比上一版（0.38%）大是**好事**：
以前只在实测覆盖的 128 天上判得出跌停，其余 760 天**整段不判**，现在全区间都判。

**涨跌停判定必须比「收益率」而不是「价格」**：`daily_bar.close` 存的是**后复权**价，
而 eltdx 的 `limit_up_price` 基于**不复权**前收盘价。直接比会算出当日 4303 家
「涨停」（占全市场 77%）；改比 `close(t)/close(t-1)-1` 与 `limit_ratio_pct` 后，
得到 8 涨停 / 14 跌停，符合 A 股实况。容差 0.2 个百分点，用于吸收交易所
价位四舍五入与后复权比率之间的偏差。

**数据来源分两层（实测优先，规则兜底）**：

| 层 | 来源 | 覆盖 |
|---|---|---|
| 实测 | `price_limit_daily`（eltdx `daily_price_limits`，含交易所公布的规则标签） | 2026-10-14 实测 **165/888** 个交易日，仍在用 `--fill` 续采 |
| 推算 | 板块规则：主板（含 ST，2024 新规后 ST 也是 10%）10% / 创业板·科创板 20% / 科创板 CDR(689) 10% / 北交所 30% / **上市前 5 个交易日无限制** | 全区间 888 天 |

推算层**用已采集的 135 个交易日、743,959 行逐行校验过：比例完全一致（0 处不一致）**，
新股前 5 日的 508 行也全部落在「无涨跌幅限制」。两层合并后回测对**每一天**都做过滤，
`warnings` 里会写明当次用的覆盖天数与一致率 —— 而不是像以前那样在未覆盖的日子
静默地少一层过滤。实测数据来自交易所口径（六种规则标签：主板 / 创业板 / 科创板 /
北交所 / ST / 新股前五日无限制），实测永远优先于推算。

> **采集成本与并发**：该接口**服务端不缓存**，每交易日一次调用（本机实测 31~239 秒，
> 与时段/服务端负载有关）。819 个未覆盖交易日按 35 秒/天串行要 8 小时；
> `--workers 6` 实测 6 天 186 秒（≈31 秒/天，服务端按连接排队，**并发只快约 15%**），
> 因此全量补齐仍需约 7 小时 —— 用 `cli price-limits --fill --workers 6
> --max-minutes N` 分批续采（跳过已覆盖日期、可反复运行；`--workers` 默认 1，
> 不显式指定不会加大采集压力）。

### 大盘择时的实测影响（2026-10-14，零成本口径）

上面「全年满仓、无择时」的结论，加上择时后会变成什么样：

| 择时条件 | 总收益 | 年化 | 最大回撤 | 夏普 | 空仓调仓点 |
|---|---|---|---|---|---|
| 不择时（满仓） | +5.49% | +1.72% | −33.46% | 0.28 | 0 / 39 |
| 沪深300 > 200 日均线 | −18.19% | −5.41% | −33.46% | −1.26 | 22 / 45 |
| **沪深300 > 50 日均线** | **+16.77%** | **+4.39%** | **−25.38%** | **1.46** | 27 / 45 |
| 沪深300 > MA200 且 MA50 > MA200 | −25.12% | −7.70% | −33.46% | −1.93 | 24 / 45 |

结论同样如实：**只有 50 日均线这一档同时改善了收益和回撤**，另外两档反而更差。
这说明「择时」本身不是银弹，具体用哪条均线必须实测，不能想当然。
选择过于迟钝的 200 日均线在这个区间里反而错过了反弹、却没躲开下跌。

> 「空仓调仓点」= 该点 `holdings == 0` 的个数（这些点照记净值，曲线上才看得出空仓期）。
> 带择时的三行有 45 个点、从 2023-02-13 开始，不带择时的只有 39 个点、从 2023-07-11
> 才开始——后者前一批日期里没有一只股票达到 RPS60 ≥ 80，**这些点不会被记录**。
> 最早那 5 个空仓点（2023-02-13 ~ 2023-06-09）的原因不是择时信号，而是
> `benchmark_bar` 只有 800 根、起点晚于日 K：均线未成形时按「数据不足就不下注」不开仓。

择时逻辑已用无未来函数测试守住（`backend/tests/test_market_filter.py`）：
扰动 T 日之后的行情，T 日的开仓判定必须逐值不变。

### 行业分类从哪来

板块强弱页的「行业」用的是 `industry_map`，由 `cli industry-map` 写入：

```powershell
.venv\Scripts\python.exe -m rpsboard.cli industry-map                       # 表为空时写入
.venv\Scripts\python.exe -m rpsboard.cli industry-map --replace             # 重建
.venv\Scripts\python.exe -m rpsboard.cli industry-map --replace --from-csv 我的行业.csv
```

数据来源按优先级自动选择（本机 2026-10-13 实测）：

| 来源 | 状态 | 行业口径 |
|---|---|---|
| eltdx 具名行业板块（`一级行业`/`二级行业`/`三级行业`/`880行业`） | 本机四个分类**都是 0 个板块**：板块定义文件 `tdxzs.cfg`/`tdxzs3.cfg` 不在本地、服务器 `download_file` 也返回 0 字节 | `通达信<分类>`（有名字时用板块名） |
| `tdxhy.cfg`（服务器可下，5668 行 / 每票一条） | ✅ 可用 | `通达信行业代码`：键是层级代码（`X` + 2 位 = 一级，**30~31 组**），该文件**不含行业名称** |
| `--from-csv` 自带映射（`symbol,industry[,industry_cls]`） | ✅ 可用 | `外部导入`（在该列里给出你自己的口径名） |

- **默认不覆盖已有数据**：作者机器上原有的 5224 行是「证监会行业分类」口径（83 个具名行业），
  与上面两套口径的行业数量、名单**都不通用**，混在一张表里会让行业 RPS 失去可比性；
  要换口径只能显式 `--replace`。想恢复具名口径就用 `--from-csv`（把现有库导出成 CSV 即可，
  仓库里没有可复现的名称来源——原先那份来自已删除的一次性脚本）。
- 一只股票只归一个行业（多归属时取成员数最少的那个），避免同一只票的涨幅被重复计入多个行业。
- 空库（刚 clone）时 `cli update` 会自动补这一步，所以新机器的板块强弱页不会空着。

### 概念/题材板块

证监会行业只有 83 个，粒度太粗（`C39 计算机、通信…` 里塞了 619 只），
而 A 股实盘看的是概念。本项目另有一套概念板块 RPS：

- 采集：79 秒拿全部板块清单 + 3.7 秒/板块取成分股，全量约 **31 分钟**
- 规模：**267 个概念、46,590 条成分关系、覆盖 5,443 只股票**，
  概念 RPS 157,707 行 / 202 个有数据的概念
- 计算：**完全复用行业 RPS 的向量化实现**，只换一份分组映射表

> 一只股票常同时属于多个概念（AI 算力 / CPO 共享个股）。为了让概念 RPS
> 仍然可比，每只票**只归入一个主分组**——否则同一只股票的涨幅会被重复计入
> 多个概念，成分数也会虚高。界面上概念与行业可一键切换，热力图的各周期
> 独立排序逻辑原样复用。

### 一键更新

页面上「更新数据」按钮触发的是**后台线程**，不是同步请求：

```
[按钮] → POST /api/admin/update      → 立即返回（不同步等待）
           ↓ 后台线程
          ① 拉日K      约 209 秒
          ② 采涨跌停    约 35 秒
          ③ 重算 RPS    约 10 秒
           ↓
[轮询] → GET /api/admin/update/status → 阶段 / 耗时 / 失败原因
```

早期实现是**同步**跑完整流程的，219 秒必然超时，而且会阻塞请求线程。
现在页面上能看到当前阶段（准备中 / 拉取日K / 采集涨跌停 / 重算 RPS），
失败时**展示原始错误原因**而不是只弹一个 alert。同一时刻只允许一个任务
（避免两个 eltdx 连接池同时打行情服务器）。

命令行等价入口：`update.bat` 或 `python -m rpsboard.cli update`。

## 测试与检查

```powershell
.venv\Scripts\python.exe -m pytest backend/tests     # 全套（含覆盖率）
.venv\Scripts\python.exe -m ruff check backend       # Python lint
cd frontend; npm run lint; npm run build             # 前端 lint + 构建
```

全部测试离线运行，使用合成数据，不触网。

- **覆盖率**：`pytest` 默认带 `--cov=rpsboard --cov-report=term-missing`；**门槛
  `--cov-fail-under=50` 只写在 CI 命令里**（全套 **196 项测试、实测 TOTAL 66%**，门槛按实测值
  向下取整）——放进 `addopts` 会让「只跑一个测试文件」也因总覆盖率不足而失败。`cli.py` 与
  `sources/*` 目前覆盖较少（本轮补了 tradestatus / price-limits / backfill 退出码的用例），
  门槛按实测值设，不虚设高线。
- **lint**：ruff（`select = E,F,W,I,UP,B`，`ignore` 里三条都在 pyproject 注明理由）；
  一次性调研脚本（`probe_*` / `debug_*` / `diag_*` / `verify_*`）用
  `extend-exclude` 排除在外 —— 它们是现场临时脚本，重排风格只会淹没真实改动。
- **CI**：`.github/workflows/ci.yml`，push 与 PR 各跑一遍 backend（ruff + pytest，
  用 `uv sync --frozen` 按 `uv.lock` 装）与 frontend（`npm ci` + lint + build）两个 job。
- **依赖锁定**：`uv.lock` 入库，CI 用 `--frozen` 保证可复现。

### 开发脚本

`backend/tests/` 下除 `test_*.py`（pytest 收集）外，还有一批**一次性调研与诊断脚本**
（`probe_*` / `debug_*` / `diag_*` / `verify_*` / `bench_api.py` / `inspect_*.mjs`），
保留下来是为了让当时的结论可复现。约定：

- 必须**从仓库根目录**运行（脚本里 `sys.path.insert(0, "backend")`，相对路径也按根解析）。
- **不参与 lint**（ruff 的 `extend-exclude` 排除了这批）：它们是现场跑的一次性脚本，
  风格不统一，重排只会淹没真实改动。
- 依赖装在 optional extra 里：`uv pip install -e ".[dev,probe]"`（`requests` / `akshare` / `baostock`）。
- 多数需要联网；`verify_*` 还需要已 `backfill` 过的库；`verify_api.py` / `verify_sector_sort.py` /
  `bench_api.py` / `build_sort_check.mjs` 需要后端已在 8000 端口运行。
- 读本机通达信安装目录的脚本用 `RPSBOARD_TDX_DIR`（默认 `D:\tdx`）指定，例如
  `set RPSBOARD_TDX_DIR=E:\new_tdx && .venv\Scripts\python.exe backend\tests\probe_tdx.py`。

## 热力图分周期独立排序 · 验证页

板块强弱页的热力图**行按当前选中周期独立排序**，每个周期各维护一套强弱次序
（15 日强的行业未必 250 日也强）。生成五张自包含的静态验证页用于核对：

```powershell
node backend/tests/build_sort_check.mjs
```

产物在 `docs/sector-sort-rps{15,20,60,120,250}.html`（echarts 已内联，双击即可打开），
脚本同时会断言每个周期都给出**互不相同的排序**，否则直接退出码 1。
这几个 HTML 每个约 1 MB 且会随行情过期，已加进 `.gitignore`（不入库，随时可重新生成）。

2026-10-12 实测：83 个证监会行业，RPS20 与 RPS250 的名次几乎不相关
（Spearman ≈ 0.05）。典型例子——`科技推广和应用服务业` RPS20 排第 3 但 RPS250 只有 17；
`广播、电视、电影和录音制作业` RPS20 排第 9，RPS250 却是 90。
这正是分周期维护强弱的意义：短中期强势与长期强势完全是两批行业。

### 排查提示

若在本对话右侧的**内置浏览器**里打开页面，一直停在「连接中…」或热力图空白，
多半不是应用的问题：该环境的渲染进程在页面加载完成后会冻结，
定时器、Promise 回调和点击事件都不再执行，但**加载期的同步渲染正常**。
判断办法是看后端 access log——如果 `/api/...` 全都返回 200，
说明请求已正常往返，卡的是浏览器侧。用自己的 Chrome / Edge 打开即可。

## 性能基线（本机实测）

428 万行日K / 404 万行 RPS / 5574 只股票 / 83 个行业。

> 下面各表的「改造后」是四周期（20/60/120/250）时代（2026-10-12）的基线。
> 2026-10-13 加入 15 日周期、并把全量重算改成「先删覆盖索引、写完再重建、顺带删掉
> 冗余的 `idx_rps_date`」之后，全量重算实测 **115 s（普通计时）/ 119 s（cProfile）**，
> 而这时的落库行数还多了 2.5%（404 万 vs 394 万）。分解见「compute-rps」一节的注。

### compute-rps

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 全量重算 | 132 s | 67 s（其中板块计算 38.9s → 0.52s） |
| 日常增量（新增 3 个交易日） | — | **10.3 s**，且与全量**逐值一致** |
| 无新增（已是最新） | 132 s | **1.1 s** |

```powershell
.venv\Scripts\python.exe -m rpsboard.cli compute-rps          # 默认增量
.venv\Scripts\python.exe -m rpsboard.cli compute-rps --full    # 历史被订正时全量重算
.venv\Scripts\python.exe -m backend.tests.verify_incremental    # 验证增量==全量（合成数据，离线）
```

> 增量路径只补「晚于最后一个已算出的交易日」的日子。某个日期算不出任何行时
> （如窗口起点那几天只有 1 只股票、都不足 16 根前置 K 线，或长假后首日全市场被标成
> 停牌）不会推进水位线，因此数据不全的新交易日会在次日自动重试。
> **中间出现真正的空洞增量补不上，用 `--full`。**

**落库是全量重算的瓶颈（2026-10-13 实测）**。全量写 404 万行时按 `tottime` 排序：

| 阶段 | 改造前（394 万行，索引在线） | 改造后（404 万行，先删后建） |
|---|---|---|
| `executemany` 写入 | **91.0 s** | **78.7 s** |
| 建索引 / DELETE / DDL | ~1.5 s | ~9.5 s（其中 CREATE INDEX ≈ 8 s） |
| 读 daily_bar（`fetchall`） | 7.5 s | 6.5 s |
| commit | 4.3 s | 2.4 s |
| 周期数学（`rank` + 转 DataFrame） | ~3 s | ~3 s |

**结论要说清楚**：把覆盖索引从写入路径上摘下来，单行插入成本降了约 16%（23.1 µs → 19.5 µs），
但写完之后 `CREATE INDEX` 要补回约 8 秒，所以**净收益只有几秒（约 4%）**，不是数量级的提速。
真正确定拿到的收益是**删掉冗余的 `idx_rps_date`**：少维护一棵 81 MB 的 B 树，库文件
VACUUM 后从 1567 MB 降到 1430 MB，且 `executemany` 的 91 s → 78.7 s 里有一部分来自它。
其余时间仍在 IO 与 pandas 落库路径上，要再快只能减少写入量（例如只重写变化的日期）。
上表其余数字都是四周期（2026-10-12）时代的，做性能对比时注意口径 —— 15 日周期后
只重测了全量重算。

### 接口耗时（直接调用处理函数，剔除 ASGI 开销）

| 接口 | 改造前 | 改造后 |
|---|---|---|
| 排行榜 第 1 页 | 467 ms | 30 ms → **38 ms** |
| 排行榜 第 30 页 | 469 ms | **41 ms** |
| 状态 `/api/meta/status` | 733 ms | **3.4 ms** |
| 板块排行 / 热力图 | ~280 ms | **4 ms** |
| 排行榜 + 成交额过滤（慢路径） | 705 ms | 424 ms |

三个关键改动：

1. **`COUNT(*)` 不能用来判空**。对 427 万行的 `daily_bar`，`COUNT(*)` 要 **235 ms**，
   而 `MAX(date)` / `SELECT 1 ... LIMIT 1` 都是 **0.0 ms**。原来每个请求都要付一次。
2. **延迟连接**：先在 `rps_daily` 上按 RPS 取 Top-N，再只对这 N 行做 JOIN。
3. **ST 过滤必须用 `NOT EXISTS`**，不能用 `symbol IN (SELECT ...)`：
   IN 子查询会让优化器放弃日期索引改走主键逐只查找，实测 72.4 ms → 4.9 ms。

### 回测

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 冷启动（读库 + 建矩阵 + 算 RPS） | 34.8 s | 33.7 s |
| 改一个参数（阈值/周期/持有期） | 34.8 s | **1.4 s** |
| 同参数重复请求 | 34.8 s | **1.8 ms** |

> 原计划「回测复用已落库的 RPS」实测**不成立**：`SELECT ... FROM rps_daily` 要读回
> 400 万行再 pivot，和从缓存矩阵重算一样贵（36.1s vs 31.6s）。
> 真正的收益来自**矩阵缓存 + 结果缓存**，已按实测结果调整。

## 性能监测点

上面的基线是**一次性手测**，会随行情与改动漂移。现在运行时自带监测：
每个 HTTP 请求由纯 ASGI 中间件计时（`backend/rpsboard/api/perf.py`），
进内存环形缓冲 + 追加一行 `logs/perf.jsonl`；前端在唯一网络出口
（`frontend/src/api/index.js`）计时并批量上报，两端在同一个接口里对照。

```powershell
curl -s http://127.0.0.1:8000/api/meta/perf      # 前后端耗时快照
```

响应里 `server.paths[].p50_ms / p95_ms` 是**最近 N 条请求**（默认 1000 条）的移动窗口值，
不是全量累计；要跨重启的统计直接聚合 `logs/perf.jsonl`（每行一个 JSON）。
中间件同时下发 `x-perf-ms` 响应头（服务端处理耗时），前端的
`net_ms = ms - server_ms` 即网络 + 解析那一段。

浏览器控制台：`window.__perf()` 看本页请求，`await window.__perf.server()` 看两端对照，
`window.__perf.enable()` 后刷新即逐请求打点（默认静默，不在界面上暴露调试数字）。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `RPSBOARD_PERF_SLOW_MS` | 300 | 超过该毫秒数记为慢请求 |
| `RPSBOARD_PERF_LOG` | `logs/perf.jsonl` | 逐请求日志路径（不轮转） |
| `RPSBOARD_PERF_RING_SERVER` / `_CLIENT` | 1000 / 500 | 内存窗口条数 |

## 未完成项

- **P3 · 交易日历覆盖**：`/api/meta/dates` 取自 `rps_daily`（不是 `trade_calendar`，
  后者已删除）。实测 RPS 覆盖 832 个交易日（`daily_bar` 有 888 个）：
  差的 56 天里 **41 天是「横截面只有 1 只股票」**（百分位恒等于 100，2026-10-14 起
  按新口径不落库），其余是行情窗口起点（前置 K 线不足 16 根，达不到最短的 15 日周期
  门槛），**都不是缺口**。同理，越早的日期有值的周期越少（250 日要 250 根前置 K 线）。
- **P2 · 数据库体积**：1430 MB / 约 935 万行（daily_bar 428 万 + rps_daily 404 万 +
  涨跌停 67.5 万 + 概念 15.8 万）。2026-10-13 删掉冗余的 `idx_rps_date` 并 VACUUM，
  从 1567 MB 降到 1430 MB（`freelist_count` 归零）。**`open/high/low` 不能删**：三升底形态
  （`analytics/patterns.py`）、排行榜的形态失效价（`api/rps.py` 的 `pattern_state()`
  按 `date <= 查询日` 聚合 `MIN(low)`）、个股页失效判定（`api/stock.py` 一次取全量
  `(date, low)` 后用反向累计最小值，不再逐条查 `MIN()`）都在用；`volume` 被个股曲线用。
  继续瘦身只能走「历史裁剪」这类有损方案，本轮不动。
- **复权因子异常（剩余部分）**：端点价 `< 1 元`（含穿零）的区间收益**已自动剔除**
  （`config.ANOMALY_MIN_PRICE`，全库 394 行，见「RPS 计算口径」的「异常端点」一条）；
  仍未自动的是「端点价正常、窗口只是跨过了负偏移档位或因子跳变日」的收益 ——
  这类窗口是真实涨幅与送转成分的混合，要先定口径（按变换零点距离加权裁剪？
  还是按送转比例还原成原始价口径？）才能动手，当前只报告（`cli quality` 的
  `negative_offset` / `factor_jump`）。实测 6 只股票的库内后复权价 < 1 元
  （371 行），其中 5 只的变换零点可被官方因子解释。
- **涨跌停历史覆盖**：接口**服务端不缓存**，实测 31~239 秒/天（见「真实可交易性」的
  采集成本）。2026-10-14 已补到 **165/888** 天，且**回测不再依赖它**：未覆盖的日子
  按板块规则推算（与实测 743,959 行逐行一致率 100%），因此过滤是全区间生效的。
  继续补实测只需后台跑 `cli price-limits --fill --workers 6 --max-minutes N`
  （可反复运行，服务端按连接排队，6 并发约 31 秒/天）。
  回测 `warnings` 会写明当次「实测覆盖 X/Y + 推算」的比例。

## 目录结构

```
backend/rpsboard/
  config.py              周期（PERIODS / period_columns）、路径、数据源选择、交易所判定
  db.py                  SQLite schema、旧库迁移（ensure_period_columns / ensure_indexes）与幂等写入
  cli.py                 init / status / backfill(=sync) / update / tradestatus / quality
                         / price-limits / concept / patterns / compute-rps / backtest
                         / watch / alert / notify
  sources/
    tdxnet_src.py        【唯一数据源】eltdx 通达信协议，TCP 直连，209 秒全市场
    board_src.py         概念/题材板块清单与成分股
    limit_src.py         涨跌停价（eltdx daily_price_limits）+ 并发续采
    base.py              SourceError 等公共定义
  analytics/
    matrix.py            daily_bar → 日期×股票 宽矩阵
    rps.py               RPS 引擎 + 板块/概念 RPS
    backtest.py          横截面动量回测（共振掩码 resonance_mask）
    costs.py             交易成本模型
    limits.py            涨跌停规则 + 板块比例推算（实测缺失时兜底）
    quality.py           复权因子异常检测（官方因子 + 库内跳变，只报告）
    alerts.py            预警规则评估
    patterns.py          三升底等形态
  api/                   FastAPI 路由（除 watchlist.py 外只读 SQLite，不联网）+ perf.py 性能监测
    watchlist.py         **全 API 唯一的写入口**（watchlist / alert_rule / alert_hit）
frontend/src/
  api/index.js           唯一网络出口（顺带计时上报、超时中止）
  views/                 Rankings / Sectors / StockDetail / Backtest / Watchlist
  components/RpsTable.vue  排行榜表格（RPS 列随 period 参数变化）
  composables/           usePeriods.js（周期清单唯一来源）/ useLatestRequest.js（竞态守卫）
                         / useResizableColumns.js
start.bat / update.bat   Windows 一键启动 / 更新
start.sh  / update.sh    Linux / macOS 一键启动 / 更新
```

## 自选股池与条件预警

在「排行榜」点行首 ☆ 即加入自选，顶栏「自选预警」页可以增删自选、管理规则、
查看命中记录。命令行为等价入口（同样只落库 + 终端输出，**不做消息推送**）：

```powershell
.venv\Scripts\python.exe -m rpsboard.cli watch add 600519 --note 白马   # 加入自选
.venv\Scripts\python.exe -m rpsboard.cli watch ls                       # 自选 + 当日 RPS
.venv\Scripts\python.exe -m rpsboard.cli watch rm 600519

.venv\Scripts\python.exe -m rpsboard.cli alert add --name RPS90 --kind rps_threshold `
    --params '{"period":60,"min":90,"max":100}'
.venv\Scripts\python.exe -m rpsboard.cli alert ls                       # 规则 + 累计命中
.venv\Scripts\python.exe -m rpsboard.cli alert run                      # 评估并落库
```

规则类型（`kind` 决定 `params` 的 JSON 结构）：

| kind | params | 命中条件 |
|---|---|---|
| `rps_threshold` | `{"period":60,"min":90,"max":100}` | 当日 `rps{period}` 落在区间内 |
| `sanxingdi` | `{"window":5}` | 近 window 个交易日内出现过三升底 |
| `limit_up` | `{}` | 当日按 `limit_ratio` + 容差判为涨停 |

- 命中写进 `alert_hit`，主键 `(规则, 日期, 股票)` → **重复 run 幂等**（实测真实库
  533 条命中，连跑两次行数不变）。
- `cli update` 与页面「更新数据」的最后一步会自动跑一次 `alert run`，返回里带
  `alert_hits`。
- 数据结构：`watchlist(symbol, note, added_at)`、`alert_rule(id, name, kind, params,
  enabled, created_at)`、`alert_hit(rule_id, date, symbol, detail)`。这三张表是**全 API
  唯一的写入口**（`backend/rpsboard/api/watchlist.py`），其余 handler 仍只读 SQLite。
- 已有库升级后需要跑一次 `cli init` 建这三张表（脚本全是 `CREATE TABLE IF NOT
  EXISTS`，幂等、不动数据）。

### 推送到微信（ShowDoc 推送服务）

命中可以推到手机：用 [ShowDoc 推送服务](https://www.showdoc.com.cn/push)（免费、不限请求数，
POST `title`/`content` 到你的专属地址，消息进微信）。**只手动触发**——`cli update`
与页面「更新数据」不会自动推送。

```powershell
# 1) 打开 https://push.showdoc.com.cn（建议电脑端）→ 右上角「推送」→ 复制专属推送地址
# 2) 写进仓库根的 .env.local（已在 .gitignore 里；环境变量优先，不会覆盖）：
#    SHOWDOC_PUSH_URL=https://push.showdoc.com.cn/server/api/push/<你的token>

.venv\Scripts\python.exe -m rpsboard.cli notify test          # 自检：发一条测试消息
.venv\Scripts\python.exe -m rpsboard.cli alert push --run      # 先评估规则，再推当天命中汇总
.venv\Scripts\python.exe -m rpsboard.cli alert push --dry-run  # 只看消息内容，不发送
```

- 推送内容是一条 **Markdown 汇总**（标题 `RPS 预警命中 N 条（日期）`，正文按规则分组、
  每条规则最多列 30 条明细，超出折叠成「还有 M 条」）。
- 专属地址里的 token 属凭据：只从 `.env.local` / 环境变量读，**代码与输出都不回显**
  （`cli notify test` 打印成功/失败但不打印 URL；失败只给 ShowDoc 的 `error_code` 与原因）。
- 推送失败**不影响任何数据链路**：`alert push` / `notify test` 以退出码 2 报错，
  `alert run` 与 `cli update` 照常完成。
- `.env.local` 由 `backend/rpsboard/config.py` 在导入时读取（已存在的环境变量优先），
  其它 `RPSBOARD_*` 变量也可以放这个文件里。

## 接口一览

全部挂在 FastAPI 上，`http://127.0.0.1:8000/docs` 有可调试的 Swagger。除
`/api/watchlist*`、`/api/alerts/rules*`（`api/watchlist.py`）与 `/api/admin/update`
（触发后台采集）外都是**只读**。

| 端点 | 说明 |
|---|---|
| `GET /api/meta/status` | 数据状态：行数、最后交易日、周期清单、未分类股票数 |
| `GET /api/meta/dates` · `/industries` · `/periods` | 可选交易日 / 行业清单 / 周期清单（前端周期按钮的唯一来源） |
| `GET /api/meta/perf` · `POST /api/meta/perf/client` | 前后端耗时快照 / 前端批量上报 |
| `GET /api/rps/ranking` | 排行榜：`period`、`rps_min`/`rps_max`、`industries`、`exclude_st`、`min_amount`、`sort` + `direction`、`delta_days`、`sanxingdi`、`page`/`limit` |
| `GET /api/rps/ranking/export` | 同一组筛选条件（含 `sort`/`direction`）的**全量 CSV** |
| `GET /api/rps/sector` · `/heatmap` | 行业排行+领涨股 / 行业 × 周期矩阵 |
| `GET /api/rps/concepts` · `/concept-ranking` · `/concept-heatmap` | 概念口径的同三件套 |
| `GET /api/stock/{symbol}` | 个股详情：多周期 RPS、全市场分位、所属行业 RPS |
| `GET /api/stock/{symbol}/curve` · `/patterns` | 多周期曲线 + 后复权价 / 三升底历史 |
| `GET /api/backtest/run` | 回测：`period`、`threshold`、`hold`、`resonance`、`market_filter`、涨跌停与成本开关 |
| `POST /api/admin/update` · `GET /api/admin/update/status` | 页面「更新数据」：后台线程 + 轮询阶段 |
| `GET/POST/DELETE /api/watchlist[/{symbol}]` | 自选股（写库） |
| `GET/POST/DELETE /api/alerts/rules[/{id}]`、`GET /api/alerts/hits`、`/kinds` | 预警规则与命中（写库） |

## 许可证

[GPL-3.0-or-later](LICENSE) · Copyright (C) 2026 KongValley

本项目以 GNU 通用公共许可证第 3 版（或更新版本）发布：可自由使用、修改、分发，
但**衍生作品必须以同一许可证开源**，且不提供任何担保。
