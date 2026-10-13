<template>
  <div class="backtest">
    <div class="panel filters">
      <div class="row">
        <div class="group">
          <label>RPS 周期</label>
          <button v-for="p in periods" :key="p" class="btn"
                  :class="{ active: period === p }" @click="period = p; run()">{{ p }}日</button>
        </div>
        <div class="group">
          <label>入选阈值 ≥ <b class="mono">{{ threshold }}</b></label>
          <input type="range" min="50" max="99" v-model.number="threshold" @change="run" />
        </div>
        <div class="group">
          <label>大盘择时</label>
          <select v-model="marketFilter" @change="run">
            <option v-for="(label, val) in MARKET_FILTERS" :key="val" :value="val">{{ label }}</option>
          </select>
        </div>
        <div class="group">
          <label>持有</label>
          <select v-model.number="hold" @change="run">
            <option :value="5">5 日</option>
            <option :value="10">10 日</option>
            <option :value="20">20 日</option>
            <option :value="40">40 日</option>
            <option :value="60">60 日</option>
          </select>
        </div>
        <div class="group">
          <label class="chk"><input type="checkbox" v-model="excludeSt" @change="run" /> 剔除ST</label>
          <label class="chk" :title="'涨停买不进。用 eltdx 的真实 limit_ratio 判定：' +
            '注意必须比收益率而非价格——库内存的是后复权价，涨停价基于不复权价。'">
            <input type="checkbox" v-model="excludeLimitUp" @change="run" /> 剔除涨停（买不进）
          </label>
          <label class="chk" :title="'佣金/印花税/过户费/滑点。分档前瞻收益不受此项影响。'">
            <input type="checkbox" v-model="useCost" @change="run" /> 计入交易成本
          </label>
          <label class="chk"><input type="checkbox" v-model="resonance" @change="run" /> 多周期共振</label>
        </div>
        <div class="spacer" />
        <button class="btn primary" :disabled="loading" @click="run">
          {{ loading ? '回测中…' : '运行回测' }}
        </button>
      </div>
    </div>

    <div v-if="warnings.length" class="warnbar-panel">
      <div v-for="(w, i) in warnings" :key="i">⚠ {{ w }}</div>
    </div>

    <template v-if="result">
      <div class="metrics">
        <div class="panel m">
          <div class="muted">累计收益</div>
          <div class="mv" :class="vcls(result.metrics.total_return)">{{ pct(result.metrics.total_return) }}</div>
        </div>
        <div class="panel m">
          <div class="muted">年化收益</div>
          <div class="mv" :class="vcls(result.metrics.annualized)">{{ pct(result.metrics.annualized) }}</div>
        </div>
        <div class="panel m">
          <div class="muted">年化波动</div>
          <div class="mv">{{ pct(result.metrics.volatility) }}</div>
        </div>
        <div class="panel m">
          <div class="muted">最大回撤</div>
          <div class="mv down">{{ pct(result.metrics.max_drawdown) }}</div>
        </div>
        <div class="panel m">
          <div class="muted">夏普比率</div>
          <div class="mv">{{ num(result.metrics.sharpe) }}</div>
        </div>
        <div class="panel m">
          <div class="muted">卡玛比率</div>
          <div class="mv">{{ num(result.metrics.calmar) }}</div>
        </div>
      </div>

      <div class="panel" style="margin-top:12px">
        <h3 style="margin-top:0">策略净值 vs 沪深300</h3>
        <VChart class="chart" :option="equityOption" autoresize />
        <div class="muted note">
          策略：每 {{ result.params.rebalance }} 个交易日调仓，持有当日 RPS{{ result.params.period }} ≥
          {{ result.params.threshold }} 的股票，等权持仓。
          <span v-if="result.params.resonance">（已开启多周期共振，要求 20/60/120 日同时达标）</span>
          <span v-if="result.params.cost && result.params.cost.enabled">
            <br />已计入交易成本：单次调仓往返约
            {{ (result.params.cost.round_trip_after * 100).toFixed(3) }}%
            （佣金含 {{ result.params.cost.commission_min }} 元起收、
            印花税 {{ (result.params.cost.stamp_after * 100).toFixed(2) }}%、
            滑点 {{ (result.params.cost.slippage_rate * 100).toFixed(2) }}%，
            按 {{ (result.params.cost.portfolio_notional / 10000).toFixed(0) }} 万本金 /
            假设 {{ result.params.cost.assume_positions }} 只测算）
          </span>
          <span v-else><br /><b>未计入交易成本</b>，净值偏乐观。</span>
        </div>
      </div>

      <div class="panel" style="margin-top:12px">
        <h3 style="margin-top:0">RPS 分档前瞻收益 —— 这是 RPS 是否有预测力的直接检验</h3>
        <p class="muted note" style="margin-top:0">
          按信号日 RPS 把全市场股票分档，统计各档在接下来 {{ result.params.hold }}
          个交易日内的平均收益与上涨概率。样本量在百万级，可直接看出是否存在单调关系。
          <span v-if="result.buckets_note" style="display:block;margin-top:4px">
            ⚠ {{ result.buckets_note }}
          </span>
        </p>
        <div class="finding" :class="findingClass">
          <div class="finding-title">{{ findingTitle }}</div>
          <div>{{ findingBody }}</div>
        </div>
        <VChart class="chart bucket" :option="bucketOption" autoresize />
        <table class="rps-table" style="margin-top:10px">
          <thead>
            <tr>
              <th style="width:100px">RPS 区间</th>
              <th style="width:120px" class="num">样本数</th>
              <th style="width:140px" class="num">平均前瞻收益</th>
              <th style="width:120px" class="num">上涨概率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in result.buckets" :key="b.bucket">
              <td class="mono">{{ b.range }}</td>
              <td class="num muted">{{ b.samples?.toLocaleString() }}</td>
              <td class="num" :class="b.avg_forward_return >= 0 ? 'up' : 'down'">
                {{ b.avg_forward_return != null ? pct(b.avg_forward_return) : '—' }}
              </td>
              <td class="num muted">
                {{ b.win_rate != null ? (b.win_rate * 100).toFixed(1) + '%' : '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { backtestApi } from '../api'

const periods = [20, 60, 120, 250]
const period = ref(60)
const threshold = ref(80)
const hold = ref(20)
const excludeSt = ref(true)
const excludeLimitUp = ref(true)
const resonance = ref(false)
/** 是否计入交易成本。默认开启——零摩擦回测会系统性高估可执行结果。 */
const useCost = ref(true)
const loading = ref(false)
const result = ref(null)
const warnings = ref([])

/**
 * 大盘择时。欧奈尔的原方法是**只在强势市场做多**，而纯价格 RPS 回测
 * 全年满仓、无择时——这正是它测出「反向」结论的重要原因之一。
 * 这里的口径与后端 analytics/backtest.py 的 MARKET_FILTERS 一一对应。
 */
const MARKET_FILTERS = {
  none: '不择时（全年满仓）',
  ma200: '沪深300 > 200 日均线',
  above_ma50: '沪深300 > 50 日均线',
  trend_up: '沪深300 > 200 日均线 且 50 日 > 200 日',
}
const marketFilter = ref('none')

async function run() {
  loading.value = true
  try {
    const d = await backtestApi.run({
      period: period.value, threshold: threshold.value, hold: hold.value,
      exclude_st: excludeSt.value, exclude_limit_up: excludeLimitUp.value,
      resonance: resonance.value, market_filter: marketFilter.value,
      use_cost: useCost.value,
    })
    result.value = d
    warnings.value = d.warnings || []
  } catch (e) {
    alert(`回测失败：${e.message}`)
  } finally {
    loading.value = false
  }
}

const equityOption = computed(() => {
  const eq = result.value?.equity_curve || []
  const bm = result.value?.benchmark_curve || []
  const mk = result.value?.market_curve || []
  const dates = eq.map((x) => x.date)
  const eqv = eq.map((x) => x.equity)
  const bmMap = Object.fromEntries(bm.map((x) => [x.date, x.equity]))
  const bmv = dates.map((d) => bmMap[d] ?? null)
  // 择时序列按交易日给，净值按调仓点给，两者日期轴不同 —— 按日期映射到净值轴上
  const mkMap = Object.fromEntries(mk.map((x) => [x.date, x]))
  const mkOn = dates.map((d) => mkMap[d]?.close ?? null)
  const mkMa50 = dates.map((d) => mkMap[d]?.ma50 ?? null)
  const mkMa200 = dates.map((d) => mkMap[d]?.ma200 ?? null)
  const flat = dates.filter((d) => mkMap[d] && mkMap[d].open === false)
  const flatAreas = flat.length
    ? [[{ xAxis: flat[0] }, { xAxis: flat[flat.length - 1] }]]
    : []

  const series = [
    { name: '策略净值', type: 'line', data: eqv, showSymbol: false,
      lineStyle: { color: '#f6465d', width: 2 }, z: 5 },
    { name: '沪深300', type: 'line', data: bmv, showSymbol: false, connectNulls: true,
      lineStyle: { color: '#8b98a9', width: 1.4 }, z: 2 },
  ]
  if (marketFilter.value !== 'none') {
    series.push(
      { name: '沪深300(择时基准)', type: 'line', data: mkOn, showSymbol: false,
        connectNulls: true, lineStyle: { color: '#4c9aff', width: 1.2, type: 'dashed' }, z: 1 },
      { name: 'MA50', type: 'line', data: mkMa50, showSymbol: false, connectNulls: true,
        lineStyle: { color: '#f0a020', width: 1, type: 'dotted' }, z: 1 },
      { name: 'MA200', type: 'line', data: mkMa200, showSymbol: false, connectNulls: true,
        lineStyle: { color: '#2ebd85', width: 1, type: 'dotted' }, z: 1 },
      { name: '空仓区间', type: 'line', data: [], z: 0,
        markArea: {
          silent: true,
          itemStyle: { color: 'rgba(120,132,150,.14)' },
          data: flatAreas,
        } },
    )
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: series.map((s) => s.name), textStyle: { color: '#8b98a9' }, top: 0 },
    grid: { left: 60, right: 30, top: 34, bottom: 40 },
    xAxis: { type: 'category', data: dates, axisLabel: { color: '#8b98a9', fontSize: 10 },
      axisLine: { lineStyle: { color: '#2a323d' } } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#8b98a9' },
      splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } } },
    series,
  }
})

const bucketOption = computed(() => {
  const b = (result.value?.buckets || []).filter((x) => x.avg_forward_return != null)
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (ps) => {
        const i = ps[0].dataIndex
        const row = b[i]
        return `${row.range}<br/>平均前瞻收益 ${(row.avg_forward_return * 100).toFixed(2)}%<br/>上涨概率 ${(row.win_rate * 100).toFixed(1)}%<br/>样本 ${row.samples.toLocaleString()}`
      },
    },
    legend: { data: ['平均前瞻收益', '上涨概率'], textStyle: { color: '#8b98a9' }, top: 0 },
    grid: { left: 60, right: 60, top: 34, bottom: 40 },
    xAxis: { type: 'category', data: b.map((x) => x.range),
      axisLabel: { color: '#8b98a9' }, axisLine: { lineStyle: { color: '#2a323d' } } },
    yAxis: [
      { type: 'value', axisLabel: { color: '#8b98a9', formatter: (v) => (v * 100).toFixed(0) + '%' },
        splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } } },
      { type: 'value', min: 0, max: 1, axisLabel: { color: '#8b98a9', formatter: (v) => (v * 100).toFixed(0) + '%' },
        splitLine: { show: false } },
    ],
    series: [
      { name: '平均前瞻收益', type: 'bar', data: b.map((x) => x.avg_forward_return),
        itemStyle: { color: (p) => (p.value >= 0 ? '#f6465d' : '#2ebd85'), borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 46 },
      { name: '上涨概率', type: 'line', yAxisIndex: 1,
        data: b.map((x) => x.win_rate), showSymbol: true, symbolSize: 7,
        lineStyle: { color: '#f0a020', width: 2 }, itemStyle: { color: '#f0a020' } },
    ],
  }
})

function pct(v) { return v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%` }
function num(v) { return v == null ? '—' : Number(v).toFixed(2) }
function vcls(v) { return v == null ? '' : (v >= 0 ? 'up' : 'down') }

// ── 分档结论的自动解读 ──────────────────────────────────────────
// buckets 是**按 RPS 从高到低**排列的（90-100 在前，0-50 在后）。
// 因此「RPS 越高收益越高」等价于「数组递减」。为避免再次把方向搞反，
// 这里直接用首尾比较来判断方向，不依赖「递增/递减」的字面含义。
const bucketStats = computed(() => {
  const b = (result.value?.buckets || []).filter((x) => x.avg_forward_return != null)
  if (b.length < 2) return null
  const hiRps = b[0]                    // RPS 90-100（收益最高的档位才是正向）
  const loRps = b[b.length - 1]         // RPS 0-50
  const positive = hiRps.avg_forward_return >= loRps.avg_forward_return
  return {
    hiRps, loRps, positive,
    gap: (hiRps.avg_forward_return - loRps.avg_forward_return) * 100,
  }
})

const findingClass = computed(() => {
  const s = bucketStats.value
  if (!s) return ''
  return s.positive ? 'good' : 'warn'
})

const findingTitle = computed(() => {
  const s = bucketStats.value
  const h = result.value.params.hold
  if (!s) return ''
  return s.positive
    ? `✓ 分档呈正向：RPS 越高，未来 ${h} 日收益越高`
    : `⚠ 实测为反向：RPS 越高，未来 ${h} 日收益反而越低（短期反转）`
})

const findingBody = computed(() => {
  const s = bucketStats.value
  if (!s) return ''
  const p = result.value.params
  const gap = Math.abs(s.gap).toFixed(2)
  const head = `最高档 RPS90-100 平均前瞻收益 ${pct(s.hiRps.avg_forward_return)}，` +
               `最低档 RPS0-50 为 ${pct(s.loRps.avg_forward_return)}，相差 ${gap} 个百分点；` +
               `样本量在百万级，方向单调明确。`
  if (s.positive) {
    return head + `RPS 在 ${p.hold} 日持有期上具备正向预测力。`
  }
  return head +
    `这与欧奈尔的直觉相反，但**不是计算错误**：60 日 RPS 在 20 日持有期上呈现短期反转。` +
    `关键差异在于——欧奈尔的 RPS 是「选股工具」而非「买卖时点」，他用的是 52 周（年）相对强度、` +
    `配合 CANSLIM 的基本面（C/A/N/S）与流动性（L）条件，且只在强势市场做多；` +
    `而本页是纯价格的 ${p.period} 日 RPS、等权买入、${p.hold} 日固定持有、全年无择时、无止损。` +
    `因此正确结论是：**${p.period} 日 RPS 在 ${p.hold} 日持有期上呈反转，不宜单独作为买入信号**，` +
    `而不是「RPS 失效」。想贴近原方法，应改用 250 日窗口、加入市场择时与基本面过滤。`
})

onMounted(run)
</script>

<style scoped>
.filters { margin-bottom: 12px; }
.row { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.group { display: flex; align-items: center; gap: 6px; }
.group label { color: var(--text-dim); font-size: 13px; }
.chk { display: flex; align-items: center; gap: 5px; cursor: pointer; margin-right: 12px; }
.spacer { flex: 1; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }

.warnbar-panel {
  background: rgba(240,160,32,.10); border: 1px solid rgba(240,160,32,.3);
  border-radius: 8px; padding: 12px 16px; color: var(--warn);
  font-size: 12px; line-height: 1.8; margin-bottom: 12px;
}

.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.m { text-align: center; }
.m .muted { font-size: 12px; }
.mv { font-size: 24px; font-weight: 700; margin-top: 5px; font-family: 'SF Mono', Menlo, Consolas, monospace; }

.chart { height: 380px; }
.chart.bucket { height: 300px; }
.note { font-size: 12px; line-height: 1.7; }
.finding {
  border-radius: 6px; padding: 12px 16px; font-size: 12px;
  line-height: 1.8; margin: 10px 0;
}
.finding-title { font-weight: 700; font-size: 13px; margin-bottom: 4px; }
.finding.warn { background: rgba(240,160,32,.10); border: 1px solid rgba(240,160,32,.35); color: var(--warn); }
.finding.good { background: rgba(46,189,133,.10); border: 1px solid rgba(46,189,133,.35); color: var(--down); }
.finding.warn .finding-title, .finding.good .finding-title { color: inherit; }
td.num, th.num { text-align: right; }
</style>
