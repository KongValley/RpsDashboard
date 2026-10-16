<template>
  <div class="trade">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>
    <div v-if="snapMsg" class="warnbar soft inset">{{ snapMsg }}</div>

    <div v-if="s?.halt" class="warnbar inset">
      发单开关已开启（halt）：`cli trade execute` 会被拒绝，直到执行
      `cli trade halt --off` 或点下面的按钮解除。
    </div>

    <!-- 状态卡 -->
    <div class="panel">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">QMT 账户</h3>
        <span class="badge mode">{{ modeLabel }}</span>
        <span class="badge" :class="s?.halt ? 'bad' : 'ok'">
          {{ s?.halt ? '已 halt' : '允许发单' }}
        </span>
        <span class="badge" :class="s?.connected ? 'ok' : 'dim'">
          {{ s?.connected ? '上次连接成功' : '尚未成功连接' }}
        </span>
        <div class="spacer" />
        <button class="btn" :disabled="snapshotting" @click="takeSnapshot">
          {{ snapshotting ? '取快照中…' : '重取持仓快照' }}
        </button>
        <button class="btn" @click="toggleHalt">{{ s?.halt ? '解除 halt' : '开启 halt' }}</button>
      </div>

      <div class="stats">
        <div class="stat">
          <span class="muted">账号</span>
          <strong class="mono">{{ s?.account_masked || '—' }}</strong>
        </div>
        <div class="stat">
          <span class="muted">可用现金</span>
          <strong>{{ money(s?.asset?.cash) }}</strong>
        </div>
        <div class="stat">
          <span class="muted">持仓市值</span>
          <strong>{{ money(s?.asset?.market_value) }}</strong>
        </div>
        <div class="stat">
          <span class="muted">账户状态时间</span>
          <strong>{{ s?.last_run?.finished_at || '—' }}</strong>
        </div>
      </div>
      <p class="muted tip">
        账号与资金来自最近一次<strong>成功</strong>的 <code>cli trade status</code>；重取持仓快照只更新持仓，不改动它们。
        生成计划与发单都在命令行：<code>cli trade plan</code> → <code>cli trade execute --plan-id N --i-know-this-is-sim</code>。
      </p>
    </div>

    <!-- 自建模拟盘（不连券商）-->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">自建模拟盘</h3>
        <span class="badge mode">local</span>
        <span class="muted">
          本项目自己撮合：T 日收盘按 close×(1±0.5%) 挂限价单，T+1 日内按区间判定成交。
          <b>不需要任何券商账号</b>，也不连 QMT。
        </span>
        <div class="spacer" />
        <label v-if="paper?.days" class="muted">回看
          <select v-model="paperDate" @change="reloadPaper" style="margin-left:6px">
            <option value="">最新</option>
            <option v-for="d in paperDays" :key="d" :value="d">{{ d }}</option>
          </select>
        </label>
      </div>

      <p v-if="!paper?.days" class="muted">
        还没跑过。在命令行执行
        <code>python -m rpsboard.cli trade run --from 2026-01-01 --cash 1000000</code>
        即可生成净值曲线；之后再跑同一条命令（可省 <code>--from</code>）就是<strong>增量推进</strong>，
        历史净值不会被重算。
      </p>
      <template v-else>
        <div v-if="paper.date && paper.date !== paper.to" class="warnbar soft inset">
          正在回看 {{ paper.date }}（曲线最新一天是 {{ paper.to }}）；下方「持仓」是那天的快照。
        </div>
        <div class="stats" style="margin-bottom:14px">
          <div class="stat">
            <span class="muted">初始资金</span>
            <strong>{{ money(paper.initial_cash) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">当前净值</span>
            <strong>{{ money(paper.total) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">区间收益</span>
            <strong :class="paper.return >= 0 ? 'up' : 'down'">
              {{ pct(paper.return) }}
            </strong>
          </div>
          <div class="stat">
            <span class="muted">最大回撤</span>
            <strong class="up">{{ pct(-paper.max_drawdown) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">持仓</span>
            <strong>{{ (paper.positions || []).length }} 只</strong>
          </div>
          <div class="stat">
            <span class="muted">区间</span>
            <strong>{{ paper.from }} ~ {{ paper.to }}</strong>
          </div>
        </div>
        <VChart class="chart" :option="navOption" autoresize />

        <!-- 复盘：逐笔回合 + 绩效（数字直接来自成交落库的费用/已实现盈亏）-->
        <template v-if="review.summary && review.summary.round_trips">
          <h4 style="margin:16px 0 8px">绩效复盘</h4>
          <div class="stats" style="margin-bottom:10px">
            <div class="stat">
              <span class="muted">回合数</span>
              <strong>{{ review.summary.round_trips }}</strong>
            </div>
            <div class="stat">
              <span class="muted">胜率</span>
              <strong>{{ pct(review.summary.win_rate) }}</strong>
              <span class="sub muted">{{ review.summary.wins }} 盈 / {{ review.summary.losses }} 亏</span>
            </div>
            <div class="stat">
              <span class="muted">盈亏比</span>
              <strong>{{ num2(review.summary.profit_factor) }}</strong>
              <span class="sub muted">总盈 {{ money(review.summary.gross_win) }}</span>
            </div>
            <div class="stat">
              <span class="muted">平均持有</span>
              <strong>{{ review.summary.avg_holding_days ?? '—' }} 日</strong>
            </div>
            <div class="stat">
              <span class="muted">已实现</span>
              <strong :class="review.summary.realized_total >= 0 ? 'up' : 'down'">
                {{ money(review.summary.realized_total) }}
              </strong>
            </div>
            <div class="stat">
              <span class="muted">浮动盈亏</span>
              <strong :class="review.summary.unrealized_total >= 0 ? 'up' : 'down'">
                {{ money(review.summary.unrealized_total) }}
              </strong>
            </div>
            <div class="stat">
              <span class="muted">累计费用</span>
              <strong>{{ money(review.summary.fees_total) }}</strong>
            </div>
            <div class="stat" v-if="review.benchmark">
              <span class="muted">超额（vs 沪深300）</span>
              <strong :class="review.benchmark.excess >= 0 ? 'up' : 'down'">
                {{ pct(review.benchmark.excess) }}
              </strong>
              <span class="sub muted">基准 {{ pct(review.benchmark.return) }}</span>
            </div>
          </div>
          <div class="note muted" v-for="(n, i) in review.notes" :key="i">· {{ n }}</div>

          <h4 style="margin:16px 0 8px">逐笔回合（{{ review.round_trips.length }} 笔）</h4>
          <table class="w-table">
            <thead>
              <tr>
                <th>代码</th><th>买入日</th><th>卖出日</th>
                <th class="num">股数</th><th class="num">买入价</th><th class="num">卖出价</th>
                <th class="num">持有</th><th class="num">费用</th><th class="num">已实现盈亏</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(t, i) in review.round_trips" :key="i">
                <td class="mono">{{ t.symbol }}</td>
                <td class="mono muted">{{ t.buy_date || '—' }}</td>
                <td class="mono">{{ t.sell_date }}</td>
                <td class="num">{{ num(t.volume) }}</td>
                <td class="num">{{ fmt2(t.buy_price) }}</td>
                <td class="num">{{ fmt2(t.sell_price) }}</td>
                <td class="num muted">{{ t.holding_days ?? '—' }}</td>
                <td class="num muted">{{ money(t.fee) }}</td>
                <td class="num" :class="t.realized_pnl >= 0 ? 'up' : 'down'">
                  {{ money(t.realized_pnl) }}
                </td>
              </tr>
            </tbody>
          </table>

          <template v-if="review.industry.length">
            <h4 style="margin:16px 0 8px">当前持仓行业分布</h4>
            <table class="w-table">
              <thead>
                <tr><th>行业</th><th class="num">只数</th><th class="num">市值</th><th class="num">占比</th></tr>
              </thead>
              <tbody>
                <tr v-for="g in review.industry" :key="g.industry">
                  <td>{{ g.industry }}</td>
                  <td class="num">{{ g.n }}</td>
                  <td class="num">{{ money(g.market_value) }}</td>
                  <td class="num">{{ pct(g.weight) }}</td>
                </tr>
              </tbody>
            </table>
          </template>
        </template>

        <h4 style="margin:16px 0 8px">
          持仓
          <span class="muted" style="font-weight:400;font-size:12px">
            {{ paper.date && paper.date !== paper.to ? `${paper.date} 当日快照` : '最新一天' }}
          </span>
        </h4>
        <table class="w-table">
          <thead>
            <tr><th>代码</th><th class="num">股数</th><th class="num">成本</th><th class="num">现价</th><th class="num">市值</th><th class="num">浮动盈亏</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in paper.positions" :key="p.symbol">
              <td class="mono">{{ p.symbol }}</td>
              <td class="num">{{ num(p.volume) }}</td>
              <td class="num">{{ money(p.avg_price) }}</td>
              <td class="num">{{ money(p.close) }}</td>
              <td class="num">{{ money(p.market_value) }}</td>
              <td class="num" :class="(p.close - p.avg_price) >= 0 ? 'buy' : 'sell'">
                {{ money((p.close - p.avg_price) * p.volume) }}
              </td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>

    <!-- 策略对照：模拟盘只有一个活动账户，换策略前先归档 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">策略对照</h3>
        <span class="muted">
          模拟盘只有一个活动账户（换策略要 <code>--reset</code>）—— 跑新策略前先归档，
          旧曲线才不会丢。对照此前只能手写进 README。
        </span>
        <div class="spacer" />
        <button class="btn" :disabled="!paper?.days" @click="archiveCurrent">
          归档当前曲线
        </button>
      </div>

      <p v-if="!archives.length" class="muted">
        还没有归档。当前曲线跑到想留下的时候点上面的按钮；归档后会在这里叠加比较。
      </p>
      <template v-else>
        <table class="w-table">
          <thead>
            <tr>
              <th style="width:36px">对比</th><th>名称</th><th>参数</th>
              <th class="num">收益</th><th class="num">回撤</th><th class="num">天</th>
              <th>区间</th><th>归档时间</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in archives" :key="a.id">
              <td>
                <input type="checkbox" :value="a.id" v-model="picked" />
              </td>
              <td>{{ a.label }}</td>
              <td class="muted" style="font-size:11px">{{ paramText(a.params) }}</td>
              <td class="num" :class="a.total_return >= 0 ? 'up' : 'down'">
                {{ pct(a.total_return) }}
              </td>
              <td class="num down">{{ pct(-a.max_drawdown) }}</td>
              <td class="num muted">{{ a.days }}</td>
              <td class="muted" style="font-size:11px">
                {{ a.from_date }} ~ {{ a.to_date }}
              </td>
              <td class="muted" style="font-size:11px">{{ (a.created_at || '').replace('T', ' ') }}</td>
              <td>
                <button class="btn" @click="removeArchive(a)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <VChart v-if="picked.length || paper?.days" class="chart"
                :option="compareOption" autoresize />
        <p class="muted note">
          纵轴是净值倍数（1.0 = 本金），与上面的模拟盘曲线同口径；
          「当前」是活动账户的曲线。归档保存的是归档那一刻的净值、持仓与复盘指标，
          归档之后重跑不会改动它。
        </p>
      </template>
    </div>

    <!-- 对账 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">对账</h3>
        <span class="muted">
          最新快照的现持 vs 最新计划的绝对目标（计划编号
          <span class="mono">{{ s?.plan?.id ?? '—' }}</span>）
        </span>
      </div>
      <p v-if="!reconcile.length" class="muted">一致：账户与计划目标没有差异。</p>
      <table v-else class="w-table">
        <thead><tr><th>代码</th><th class="num">现持</th><th class="num">目标</th><th class="num">差额</th><th>方向</th></tr></thead>
        <tbody>
          <tr v-for="d in reconcile" :key="d.symbol">
            <td class="mono">{{ d.symbol }}</td>
            <td class="num">{{ num(d.have) }}</td>
            <td class="num">{{ num(d.target) }}</td>
            <td class="num">{{ num(d.delta) }}</td>
            <td class="muted">{{ d.side }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 持仓 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">持仓</h3>
        <span class="muted">{{ positions.length }} 只（最近一次快照）</span>
      </div>
      <table v-if="positions.length" class="w-table">
        <thead>
          <tr><th>代码</th><th class="num">股数</th><th class="num">可用</th><th class="num">成本</th><th class="num">市值</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in positions" :key="p.symbol">
            <td class="mono">{{ p.symbol }}</td>
            <td class="num">{{ num(p.volume) }}</td>
            <td class="num">{{ num(p.can_use_volume) }}</td>
            <td class="num">{{ money(p.avg_price) }}</td>
            <td class="num">{{ money(p.market_value) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有持仓快照：先执行 <code>cli trade status</code>，或点上面的「重取持仓快照」。</p>
    </div>

    <!-- 委托 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">委托</h3>
        <span class="muted">最近 {{ orders.length }} 笔</span>
      </div>
      <table v-if="orders.length" class="w-table">
        <thead>
          <tr><th>日期</th><th>代码</th><th>方向</th><th class="num">数量</th><th class="num">价格</th><th>状态</th><th class="num">券商号</th><th>错误</th></tr>
        </thead>
        <tbody>
          <tr v-for="o in orders" :key="o.id">
            <td class="mono">{{ o.trade_date }}</td>
            <td class="mono">{{ o.symbol }}</td>
            <td :class="o.side === 'buy' ? 'buy' : 'sell'">{{ o.side }}</td>
            <td class="num">{{ num(o.volume) }}</td>
            <td class="num">{{ money(o.price) }}</td>
            <td><span class="badge" :class="statusClass(o.status)">{{ o.status }}</span></td>
            <td class="num mono">{{ o.broker_order_id ?? '—' }}</td>
            <td class="muted">{{ o.err_msg || '' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有委托记录。</p>
    </div>

    <!-- 成交 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">成交</h3>
        <span class="muted">最近 {{ fills.length }} 笔</span>
      </div>
      <table v-if="fills.length" class="w-table">
        <thead>
          <tr><th>日期</th><th>代码</th><th>方向</th><th class="num">数量</th><th class="num">价格</th><th class="num">券商委托号</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in fills" :key="`${f.broker_order_id}-${f.broker_trade_id}`">
            <td class="mono">{{ f.trade_date }}</td>
            <td class="mono">{{ f.symbol }}</td>
            <td :class="f.side === 'buy' ? 'buy' : 'sell'">{{ f.side }}</td>
            <td class="num">{{ num(f.volume) }}</td>
            <td class="num">{{ money(f.price) }}</td>
            <td class="num mono">{{ f.broker_order_id }}</td>
            <td class="muted">{{ f.traded_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有成交记录。</p>
    </div>

    <!-- 计划 -->
    <div v-if="s?.plan" class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">最新计划 #{{ s.plan.id }}</h3>
        <span class="badge mode">{{ s.plan.mode }}</span>
        <span class="badge" :class="statusClass(s.plan.status)">{{ s.plan.status }}</span>
        <span class="muted">{{ s.plan.plan_date }} · 预算 {{ money(s.plan.cash_budget) }}</span>
      </div>
      <p class="muted tip">{{ s.plan.note }}</p>
      <table class="w-table">
        <thead>
          <tr><th>代码</th><th class="num">参考价</th><th class="num">委托价</th><th class="num">目标股数</th><th>方向</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in s.plan.items" :key="it.symbol">
            <td class="mono">{{ it.symbol }}</td>
            <td class="num">{{ money(it.ref_price) }}</td>
            <td class="num">{{ money(it.plan_price) }}</td>
            <td class="num">{{ num(it.target_volume) }}</td>
            <td :class="it.side === 'buy' ? 'buy' : it.side === 'sell' ? 'sell' : 'muted'">{{ it.side }}</td>
            <td class="muted">{{ it.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { tradeApi } from '../api'

const s = ref(null)
const paper = ref(null)
const errMsg = ref('')
const snapMsg = ref('')
const snapshotting = ref(false)
const snapFinished = ref(false)

// summary 每 10 秒轮询一次（服务端另有 10 秒 TTL 缓存，轮询不会打穿库）
const POLL_MS = 10_000
let timer = null

const positions = computed(() => s.value?.positions ?? [])
const orders = computed(() => s.value?.orders ?? [])
const fills = computed(() => s.value?.fills ?? [])
const reconcile = computed(() => s.value?.reconcile ?? [])
const modeLabel = computed(() => ({
  paper: '影子模式 paper',
  sim: '券商模拟盘 sim',
}[s.value?.mode] ?? s.value?.mode ?? '—'))

const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN'))
/** 需要固定小数位的数字（盈亏比之类） */
const num2 = (v) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(2))
const fmt2 = (v) => (v == null ? '—' : Number(v).toFixed(2))
/** 自建模拟盘回看日期（空 = 最新一天） */
const paperDate = ref('')
/** 可选回看日 = 曲线自身的交易日（倒序），不必再拉一份日期清单 */
const paperDays = computed(() => (paper.value?.curve || [])
  .map((c) => c.trade_date).reverse())
/** 复盘指标（后端 review；旧接口无此字段时给空结构） */
const review = computed(() => paper.value?.review || {
  summary: {}, round_trips: [], industry: [], notes: [], benchmark: null,
})

/** 曲线归档列表（策略对照） */
const archives = ref([])
/** 参与对照的归档 id */
const picked = ref([])

const COMPARE_COLORS = ['#f6465d', '#4c9aff', '#f0a020', '#2ebd85', '#b06cf0', '#38bdf8']

/** 归档的参数摘要（对照表里要一眼看出两条曲线差在哪儿） */
function paramText(p) {
  if (!p || !Object.keys(p).length) return '—'
  const parts = [
    p.signal === 'composite' ? '合成分' : `RPS${p.period ?? ''}`,
    p.threshold != null ? `≥${p.threshold}` : null,
    p.hold != null ? `持有${p.hold}` : null,
    p.max_positions != null ? `最多${p.max_positions}只` : null,
  ]
  return parts.filter(Boolean).join(' · ')
}

/** 当前曲线 + 勾选的归档叠加（按交易日并集对齐；不同曲线区间可以不同） */
const compareOption = computed(() => {
  const cur = paper.value?.curve || []
  const chosen = archives.value.filter((a) => picked.value.includes(a.id))
  const toMap = (rows) => new Map((rows || []).map((r) => [r.trade_date, r.nav]))
  const entries = []
  if (cur.length) entries.push({ name: '当前', map: toMap(cur) })
  for (const a of chosen) entries.push({ name: a.label, map: toMap(a.curve) })

  const dateSet = new Set()
  for (const e of entries) for (const d of e.map.keys()) dateSet.add(d)
  const dates = [...dateSet].sort()
  const series = entries.map((e, i) => ({
    name: e.name,
    type: 'line',
    showSymbol: false,
    connectNulls: true,
    data: dates.map((d) => e.map.get(d) ?? null),
    lineStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length], width: i === 0 ? 2 : 1.5 },
  }))
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: series.map((s) => s.name), textStyle: { color: '#8b98a9' }, top: 0 },
    grid: { left: 60, right: 30, top: 34, bottom: 40 },
    xAxis: {
      type: 'category', data: dates,
      axisLabel: { color: '#8b98a9', fontSize: 10 },
      axisLine: { lineStyle: { color: '#2a323d' } },
    },
    yAxis: {
      type: 'value', scale: true, axisLabel: { color: '#8b98a9' },
      splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } },
    },
    series,
  }
})

async function loadArchives() {
  try {
    const d = await tradeApi.archives()
    archives.value = d.rows || []
    // 默认全选（对照页打开就想看到全部；取消勾选即可隐藏）
    picked.value = archives.value.map((a) => a.id)
  } catch (e) {
    console.warn('归档列表加载失败', e)
  }
}

async function archiveCurrent() {
  const label = window.prompt('归档名（留空按信号源 + 时间生成）', '')
  if (label === null) return
  try {
    const r = await tradeApi.archive(label || null)
    if (!r.ok) {
      errMsg.value = r.error || '归档失败'
      return
    }
    await loadArchives()
  } catch (e) {
    errMsg.value = `归档失败：${e?.message || e}`
  }
}

async function removeArchive(a) {
  if (!window.confirm(`删除归档「${a.label}」？删掉之后这条曲线就找不回来了。`)) return
  try {
    await tradeApi.removeArchive(a.id)
    await loadArchives()
  } catch (e) {
    errMsg.value = `删除归档失败：${e?.message || e}`
  }
}
const pct = (v) => (v == null || Number.isNaN(Number(v))
  ? '—'
  : `${(Number(v) * 100).toFixed(2)}%`)
const money = (v) => (v == null || v === '' ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))

/** 模拟盘净值曲线：净值倍数（1.0 = 本金），与回测页的净值口径一致 */
const navOption = computed(() => {
  const curve = paper.value?.curve || []
  const dates = curve.map((x) => x.trade_date)
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 30, top: 24, bottom: 36 },
    xAxis: { type: 'category', data: dates, axisLabel: { color: '#8b98a9', fontSize: 10 } },
    yAxis: {
      type: 'value', scale: true, axisLabel: { color: '#8b98a9', fontSize: 10 },
    },
    series: [{
      name: '模拟盘净值', type: 'line', data: curve.map((x) => x.nav),
      showSymbol: false, lineStyle: { color: '#4c9aff', width: 2 },
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: '#8b98a9', type: 'dashed', width: 1 },
        data: [
          { yAxis: 1 },
          // 回看某天时在曲线上标一条竖线，免得"看了哪一天"全靠记忆
          ...(paperDate.value ? [{ xAxis: paperDate.value }] : []),
        ],
      },
    }],
  }
})

const STATUS_CLASS = {
  filled: 'ok', executed: 'ok', submitted: 'dim', planned: 'dim',
  pending: 'dim', canceling: 'warn', part: 'warn', partial: 'warn',
  canceled: 'dim', rejected: 'bad', halted: 'bad', unknown: 'bad',
}
const statusClass = (s2) => STATUS_CLASS[s2] ?? 'dim'

async function reload() {
  try {
    s.value = await tradeApi.summary()
    errMsg.value = ''
  } catch (e) {
    errMsg.value = `交易状态加载失败：${e?.message || e}`
  }
  await reloadPaper()
}

async function reloadPaper() {
  try {
    paper.value = await tradeApi.paper(
      paperDate.value ? { date: paperDate.value } : undefined)
  } catch (e) {
    console.warn('模拟盘加载失败', e)
  }
}

async function toggleHalt() {
  // 二次确认：这是一个「拒绝一切发单」的总开关，误点会让人以为单子丢了
  const on = !s.value?.halt
  if (on && !window.confirm('开启后 `cli trade execute` 会被拒绝，直到手动解除。确认开启？')) return
  try {
    await tradeApi.setHalt(on)
    await reload()
  } catch (e) {
    errMsg.value = `切换发单开关失败：${e?.message || e}`
  }
}

async function takeSnapshot() {
  snapshotting.value = true
  snapFinished.value = false
  snapMsg.value = ''
  try {
    const r = await tradeApi.snapshot()
    if (!r.ok) {
      snapMsg.value = r.error || '快照未启动'
      return
    }
    // 轮询到结束：连 QMT 是子进程，客户端没开时会失败，失败原因要显示出来
    for (let i = 0; i < 30 && !snapFinished.value; i += 1) {
      await new Promise((r2) => setTimeout(r2, 2000))
      const st = await tradeApi.snapshotStatus()
      snapFinished.value = !st.running
      snapMsg.value = st.error
        ? `重取快照失败：${st.error}`
        : (st.running ? '正在连 QMT 客户端…' : `已更新 ${st.rows} 只持仓`)
    }
    await reload()
  } catch (e) {
    snapMsg.value = `重取快照失败：${e?.message || e}`
  } finally {
    snapshotting.value = false
  }
}

onMounted(async () => {
  await reload()
  await loadArchives()
  timer = setInterval(reload, POLL_MS)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.spacer { flex: 1; }
.chart { height: 280px; }
.stats { display: flex; gap: 28px; flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.tip { font-size: 12px; margin: 12px 0 0; line-height: 1.6; }
.tip code { background: var(--bg-panel-2); padding: 1px 5px; border-radius: 3px; }
.w-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.w-table th {
  text-align: left; font-weight: 500; color: var(--text-dim);
  border-bottom: 1px solid var(--border); padding: 6px 8px; font-size: 12px;
}
.w-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.w-table .num { text-align: right; }
.badge.ok { background: rgba(56, 189, 148, .18); color: var(--down); }
.badge.bad { background: rgba(246, 70, 93, .2); color: var(--up); }
.badge.warn { background: rgba(240, 160, 32, .18); color: var(--warn); }
.badge.dim { background: rgba(120, 132, 150, .16); color: var(--text-dim); }
.badge.mode { background: rgba(76, 154, 255, .18); color: var(--accent); }
.buy { color: var(--up); }
.sell { color: var(--down); }
.up { color: var(--up); }
.down { color: var(--down); }
</style>
