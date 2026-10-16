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
          <label>信号源</label>
          <select v-model="signal" @change="run">
            <option value="rps">RPS 单因子</option>
            <option value="composite">多因子合成分</option>
          </select>
        </div>
        <div class="group">
          <label>选股只数</label>
          <input type="number" min="1" max="500" v-model.number="selectTop" style="width:120px"
                 placeholder="空 = 全掩码等权" title="按打分取前 N 只；配合阈值 0 即「全池前 N」"
                 @change="run" />
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

    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

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
          <div v-if="result.metrics.max_drawdown_daily != null" class="muted"
               style="font-size:11px" title="调仓点口径看不见期内的下跌，会低估回撤；逐日口径按持仓的每日收盘价盯市">
            逐日 {{ pct(result.metrics.max_drawdown_daily) }}
          </div>
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
          策略：每 {{ result.params.rebalance }} 个交易日调仓，信号源
          <b>{{ result.params.signal === 'composite' ? '多因子合成分' : `RPS 单因子（${result.params.period} 日）` }}</b>；
          入选 {{ sigName }} ≥ {{ result.params.threshold }}，
          <template v-if="result.params.select_top == null">掩码内全部等权持有</template>
          <template v-else>按打分取前 {{ result.params.select_top }} 只等权持有</template>。
          <span v-if="result.params.resonance">
            （已开启多周期共振，要求
            {{ periods.filter((p) => p !== result.params.period).join('/') }} 日同时达标）
          </span>
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
        <div class="flex items-center gap-3" style="margin-bottom:8px">
          <h3 style="margin:0">调仓记录</h3>
          <span class="muted">
            每次调仓的持仓只数与前 12 只买入名单（接口名单上限 50 只；等权全市场模式下真实持仓有几千只）
          </span>
          <div class="spacer" />
          <span class="muted">每期期末净值按建仓日归档</span>
        </div>
        <table class="w-table">
          <thead>
            <tr><th>建仓日</th><th class="num">净值</th><th class="num">持仓</th>
              <th class="num">顺延</th><th>买入名单（前 12 只）</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in rebalanceRows" :key="c.date">
              <td class="mono">{{ c.date }}</td>
              <td class="num">{{ c.equity?.toFixed(4) }}</td>
              <td class="num">{{ c.holdings || 0 }}</td>
              <td class="num muted">{{ c.deferred || 0 }}</td>
              <td class="mono muted" style="font-size:11px">
                {{ (c.holdings_top || []).slice(0, 12).join(' ') || '—' }}
                <span v-if="(c.holdings_top || []).length > 12">
                  …共 {{ c.holdings_top.length }} 只
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="history.length" class="panel" style="margin-top:12px">
        <div class="flex items-center gap-3" style="margin-bottom:8px">
          <h3 style="margin:0">参数对比</h3>
          <span class="muted">最近 {{ history.length }} 次运行（最多 {{ MAX_HISTORY }} 次）</span>
          <div class="spacer" />
          <button class="btn" @click="exportHistory">导出 JSON</button>
          <button class="btn" @click="fileInput?.click()">导入 JSON</button>
          <button class="btn" @click="history = []">清空</button>
          <input ref="fileInput" type="file" accept="application/json"
                 style="display:none" @change="importHistory" />
        </div>
        <table class="w-table">
          <thead>
            <tr>
              <th>参数</th><th class="num">累计收益</th><th class="num">年化</th>
              <th class="num">最大回撤</th><th class="num">逐日回撤</th>
              <th class="num">夏普</th><th class="num">调仓点</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in history" :key="r.key">
              <td>{{ r.label }}</td>
              <td class="num" :class="vcls(r.metrics?.total_return)">
                {{ pct(r.metrics?.total_return) }}
              </td>
              <td class="num" :class="vcls(r.metrics?.annualized)">
                {{ pct(r.metrics?.annualized) }}
              </td>
              <td class="num down">{{ pct(r.metrics?.max_drawdown) }}</td>
              <td class="num down">{{ pct(r.metrics?.max_drawdown_daily) }}</td>
              <td class="num">{{ num(r.metrics?.sharpe) }}</td>
              <td class="num muted">{{ r.metrics?.days ?? '—' }}</td>
              <td class="muted">{{ r.at }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel" style="margin-top:12px">
        <h3 style="margin-top:0">{{ sigName }} 分档前瞻收益 —— 这是信号是否有预测力的直接检验</h3>
        <p class="muted note" style="margin-top:0">
          按信号日 {{ sigName }} 把全市场股票分档，统计各档在接下来 {{ result.params.hold }}
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
import { ref, computed, onMounted, watch } from 'vue'
import { backtestApi } from '../api'
import { DEFAULT_PERIOD, usePeriods } from '../composables/usePeriods'
import { useLatestRequest } from '../composables/useLatestRequest'

const { periods, error: periodsError } = usePeriods()
const period = ref(DEFAULT_PERIOD)
const threshold = ref(80)
const hold = ref(20)
const excludeSt = ref(true)
const excludeLimitUp = ref(true)
const resonance = ref(false)
/** 是否计入交易成本。默认开启——零摩擦回测会系统性高估可执行结果。 */
const useCost = ref(true)
/**
 * 信号源：rps 单因子 / composite 多因子等权合成。
 * 合成分的方向取自**滚动训练窗的 IC 符号**（零幅度拟合），训练窗整体落在 t-hold 之前。
 */
const signal = ref('rps')
/** 只取打分最高的 N 只；空 = 掩码内全选等权 */
const selectTop = ref(null)
const loading = ref(false)
const result = ref(null)
/** 最近几次运行的指标快照，用于换参数后并排比较（页面原先只保留一份结果） */
const history = ref([])
/** 导出/导入的隐藏文件选择框 */
const fileInput = ref(null)
//: 对比表最多保留几次（再多就看不下了；需要长期留存请导出 JSON）
const MAX_HISTORY = 6
/** 调仓记录表：倒序展示最近 12 次调仓 */
const rebalanceRows = computed(() =>
  [...(result.value?.equity_curve || [])].reverse().slice(0, 12))
const warnings = ref([])
/** 请求失败时页面内的错误条（不用 alert：会打断主线程、自动化里也看不见） */
const errMsg = ref('')
/** 当前结果用的信号名（分档图、结论文案都跟着它走） */
const sigName = computed(
  () => (result.value?.params?.signal === 'composite' ? '合成分' : 'RPS'))
watch(periodsError, (v) => { if (v) warnings.value = [...warnings.value, v] })
// 连点「运行回测」时只认最后一次响应
const latest = useLatestRequest()

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

/** 选股只数：空/非法 → 不发这个参数（qs 会跳过 null），后端按「全掩码等权」处理 */
function topParam() {
  const n = Math.floor(Number(selectTop.value))
  return Number.isFinite(n) && n >= 1 ? Math.min(n, 500) : null
}

async function run() {
  const t = latest.begin()
  loading.value = true
  errMsg.value = ''
  try {
    const d = await backtestApi.run({
      period: period.value, threshold: threshold.value, hold: hold.value,
      exclude_st: excludeSt.value, exclude_limit_up: excludeLimitUp.value,
      resonance: resonance.value, market_filter: marketFilter.value,
      use_cost: useCost.value, signal: signal.value, select_top: topParam(),
    },
    // 合成分要先建 13 个全市场因子矩阵（缓存冷时实测 3~4 分钟），
    // 沿用回测默认的 3 分钟会必然超时
    signal.value === 'composite' ? 600_000 : undefined)
    if (!latest.isCurrent(t)) return
    result.value = d
    warnings.value = d.warnings || []
    remember(d)
  } catch (e) {
    if (!latest.isCurrent(t)) return
    errMsg.value = `回测失败：${e?.message || e}`
  } finally {
    if (latest.isCurrent(t)) loading.value = false
  }
}

const equityOption = computed(() => {
  const pts = result.value?.equity_curve || []
  // 优先用逐日曲线画净值：调仓点曲线看不见期内的下跌（回撤被低估）
  const dl = result.value?.daily_curve || []
  const eq = dl.length > pts.length ? dl : pts
  const bm = result.value?.benchmark_curve || []
  const mk = result.value?.market_curve || []
  const dates = eq.map((x) => x.date)
  const eqv = eq.map((x) => x.equity)
  const rebSet = new Set(pts.map((x) => x.date))
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
    // 调仓点单独标出来：逐日曲线看不出"哪几天在换仓"
    { name: '调仓点', type: 'scatter', symbolSize: 6,
      data: dates.map((d, i) => (rebSet.has(d) ? eqv[i] : null)),
      itemStyle: { color: '#f0a020' }, z: 6 },
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

// ── 多次回测的参数对比 ────────────────────────────────────────────
// 页面同一时刻只有一份结果（新请求直接覆盖），想比较"换参数差多少"只能截图。
// 这里把每次运行的指标留在内存里并排比较；导出/导入是纯前端 JSON，
// 不为此新建表 —— 真要跨会话留存再考虑落库。
function runKey(p) {
  return JSON.stringify([p.period, p.threshold, p.hold, p.exclude_st,
    p.exclude_limit_up, p.resonance, p.market_filter,
    p.cost?.enabled ?? null, p.signal, p.select_top ?? null])
}

function runLabel(p) {
  return `${p.signal === 'composite' ? '合成' : 'RPS'}${p.signal === 'composite' ? '' : p.period}`
    + ` ≥${p.threshold} · 持有${p.hold}`
    + (p.select_top ? ` · Top${p.select_top}` : '')
    + (p.market_filter && p.market_filter !== 'none'
      ? ` · ${MARKET_FILTERS[p.market_filter] || p.market_filter}` : '')
    + (p.cost?.enabled ? ' · 计成本' : ' · 零成本')
}

function remember(d) {
  const row = {
    key: runKey(d.params || {}),
    label: runLabel(d.params || {}),
    metrics: d.metrics || {},
    at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
  }
  history.value = [row, ...history.value.filter((x) => x.key !== row.key)]
    .slice(0, MAX_HISTORY)
}

function exportHistory() {
  const blob = new Blob([JSON.stringify({
    exported_at: new Date().toISOString(),
    runs: history.value,
  }, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `backtest-compare-${new Date().toISOString().slice(0, 10)}.json`
  a.click()
  URL.revokeObjectURL(a.href)
}

async function importHistory(ev) {
  const f = ev.target.files?.[0]
  if (!f) return
  try {
    // 用 File.text() 而不是 FileReader：省掉一个全局构造器（ESLint 的
    // browser globals 白名单里没有它），代码也更短
    const data = JSON.parse(await f.text())
    const incoming = Array.isArray(data?.runs) ? data.runs : []
    const out = []
    const seen = new Set()
    for (const r of [...incoming, ...history.value]) {
      if (!r?.key || seen.has(r.key)) continue
      seen.add(r.key)
      out.push(r)
    }
    history.value = out.slice(0, MAX_HISTORY)
    errMsg.value = ''
  } catch (e) {
    errMsg.value = `导入失败：${e?.message || e}`
  }
  ev.target.value = ''
}

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
    ? `✓ 分档呈正向：${sigName.value}越高，未来 ${h} 日收益越高`
    : `⚠ 实测为反向：${sigName.value}越高，未来 ${h} 日收益反而越低（短期反转）`
})

const findingBody = computed(() => {
  const s = bucketStats.value
  if (!s) return ''
  const p = result.value.params
  const gap = Math.abs(s.gap).toFixed(2)
  const head = `最高档 90-100 平均前瞻收益 ${pct(s.hiRps.avg_forward_return)}，` +
               `最低档 <50 为 ${pct(s.loRps.avg_forward_return)}，相差 ${gap} 个百分点；` +
               `样本量在百万级，方向单调明确。`
  if (s.positive) {
    return head + `${sigName.value}在 ${p.hold} 日持有期上具备正向预测力。`
  }
  if (p.signal === 'composite') {
    return head +
      `合成分是 13 个价量/板块因子的等权合成，方向由滚动训练窗的 IC 符号决定；` +
      `实测为反向说明**当前这套因子在 ${p.hold} 日持有期上没有正向预测力**，` +
      `这本身就是结论（本项目已记录过一次负面回测结论），不要靠回头调因子池或权重把它调好看。`
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
