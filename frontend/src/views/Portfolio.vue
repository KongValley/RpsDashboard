<template>
  <div class="portfolio">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>
    <div v-if="msg" class="warnbar soft inset">{{ msg }}</div>

    <div class="panel">
      <div class="flex items-center gap-3" style="margin-bottom:12px;flex-wrap:wrap">
        <h3 style="margin:0">模拟持仓</h3>
        <label class="muted">账户
          <select v-model.number="accountId" style="margin-left:6px;min-width:180px"
                  @change="applyQuery">
            <option :value="0" disabled>选择账户…</option>
            <option v-for="a in accounts" :key="a.id" :value="a.id">
              #{{ a.id }} {{ a.name }}（{{ KIND_LABEL[a.kind] || a.kind }}）
            </option>
          </select>
        </label>
        <span v-if="acc" class="badge mode">{{ acc.price_basis === 'raw' ? '原价记账' : 'HFQ 研究口径' }}</span>
        <span v-if="acc" class="badge" :class="acc.status === 'active' ? 'ok' : 'dim'">
          {{ acc.status === 'active' ? '可操作' : '已归档' }}
        </span>
        <label v-if="dates.length" class="muted">回看
          <select v-model="viewDate" style="margin-left:6px" @change="applyQuery">
            <option value="">最新</option>
            <option v-for="d in dates" :key="d" :value="d">{{ d }}</option>
          </select>
        </label>
        <div class="spacer" />
        <button class="btn" :disabled="!canAdvance || advancing" @click="advance(null)">
          {{ advancing ? '推进中…' : '推进到最新' }}
        </button>
        <button class="btn" :disabled="!acc || acc.status !== 'active'" @click="archive">
          归档
        </button>
      </div>

      <p v-if="!accounts.length" class="muted">
        还没有模拟账户。用下面的「新建账户」建一个：手动账户用来记录你自己的买卖，
        策略账户按信号自动调仓。两者互不干扰，也都不连券商。
      </p>

      <div v-if="viewDate && acc && viewDate !== acc.latest_date"
           class="warnbar soft inset" style="margin-bottom:10px">
        正在回看 {{ viewDate }}（账户最新结算 {{ acc.latest_date }}）：下方持仓、统计与
        流水都是**截至这一天**的口径。切回「最新」才能下单。
      </div>

      <template v-if="acc">
        <div class="stats" style="margin-bottom:12px">
          <div class="stat"><span class="muted">总权益</span><strong>{{ money(summary.total) }}</strong></div>
          <div class="stat"><span class="muted">现金</span><strong>{{ money(summary.cash) }}</strong></div>
          <div class="stat"><span class="muted">持仓市值</span><strong>{{ money(summary.market_value) }}</strong></div>
          <div class="stat"><span class="muted">应收/应付</span><strong>{{ money(summary.receivable) }}</strong></div>
          <div class="stat">
            <span class="muted">区间收益</span>
            <strong :class="(summary.return ?? 0) >= 0 ? 'up' : 'down'">{{ pct(summary.return) }}</strong>
          </div>
          <div class="stat"><span class="muted">最大回撤</span><strong class="down">{{ pct(-(summary.max_drawdown ?? 0)) }}</strong></div>
          <div class="stat">
            <span class="muted">区间</span>
            <strong class="mono">{{ summary.from || '—' }} ~ {{ summary.to || '—' }}</strong>
            <span class="sub muted">{{ summary.days || 0 }} 个交易日</span>
          </div>
        </div>

        <div v-if="summary.curve_truncated" class="warnbar soft inset" style="margin-bottom:8px">
          净值曲线已超过 {{ summary.curve_limit }} 个交易日：<b>图</b>只画最近这一段，
          但下方所有统计仍是完整区间（{{ summary.stats_days }} 天）。
        </div>

        <VChart v-if="(summary.curve || []).length" class="chart" :option="navOption" autoresize />

        <div v-if="riskAlerts.length" class="warnbar soft inset" style="margin:10px 0">
          <div v-for="a in riskAlerts" :key="a.kind">⚠ {{ a.name }}：{{ a.detail }}</div>
        </div>
      </template>
    </div>

    <!-- 新建账户 -->
    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">新建账户</h3>
      <div class="flex items-center gap-2" style="flex-wrap:wrap">
        <input v-model.trim="form.name" type="text" placeholder="账户名" style="width:140px" />
        <select v-model="form.kind">
          <option value="manual">手动（我自己下单）</option>
          <option value="strategy">策略（按信号自动调仓）</option>
        </select>
        <label class="muted">初始资金
          <input v-model.number="form.cash" type="number" min="1" step="10000" style="width:130px" />
        </label>
        <label class="muted">起点
          <input v-model="form.start_date" type="date" />
        </label>
        <label class="muted">参与率
          <input v-model.number="form.participation" type="number" min="0" max="1" step="0.01"
                 style="width:74px" title="单日最多吃掉该股成交量的比例；0 = 不限制" />
        </label>
        <button class="btn primary" @click="createAccount">新建</button>
        <button class="btn" :disabled="!acc" @click="copyParams">用当前账户参数新建</button>
      </div>
      <template v-if="form.kind === 'strategy'">
        <div class="flex items-center gap-2" style="flex-wrap:wrap;margin-top:8px">
          <label class="muted">RPS 周期
            <select v-model.number="form.period">
              <option v-for="p in periods" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <label class="muted">阈值 <input v-model.number="form.threshold" type="number" min="0" max="100" style="width:70px" /></label>
          <label class="muted">持有 <input v-model.number="form.hold" type="number" min="1" style="width:64px" /></label>
          <label class="muted">最多 <input v-model.number="form.max_positions" type="number" min="1" max="200" style="width:64px" /></label>
          <label class="muted">信号源
            <select v-model="form.signal">
              <option value="rps">单因子 RPS</option>
              <option value="composite">多因子合成分</option>
            </select>
          </label>
          <label class="chk"><input v-model="form.resonance" type="checkbox" />共振</label>
          <label class="chk"><input v-model="form.exclude_st" type="checkbox" />剔除 ST</label>
        </div>
        <p class="muted note">策略参数与参与率**创建时固化**：改参数请新建账户（旧曲线不会被重置或混算）。</p>
      </template>
    </div>

    <!-- 手动下单 -->
    <div v-if="canOrder" class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">手动限价委托</h3>
      <p class="muted note" style="margin-top:0">
        以账户**当前结算日**为决策日，在下一个市场交易日按日线撮合：
        买单价 ≥ 当日开盘则按开盘成交，否则当日最低价触及限价则按限价成交，一整天没触及就撤掉余量。
        停牌/一字涨停会顺延到下一个交易日。
      </p>
      <div class="flex items-center gap-2" style="flex-wrap:wrap">
        <input v-model.trim="ord.symbol" type="text" maxlength="6" placeholder="代码" style="width:86px" />
        <select v-model="ord.side">
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <input v-model.number="ord.volume" type="number" min="1" placeholder="股数" style="width:96px" />
        <input v-model.number="ord.price" type="number" min="0.01" step="0.01" placeholder="限价" style="width:86px" />
        <input v-model.trim="ord.thesis" type="text" maxlength="2000"
               placeholder="交易理由（会被留档，事后复盘用）" style="width:280px" />
        <button class="btn primary" :disabled="submitting" @click="submitOrder">提交</button>
      </div>
      <p class="muted note">数量按板块规则校验（主板/创业板 100 股整数倍、科创板 ≥200 股、
        北交所 ≥100 股）；现金或可卖量不足会被直接拒绝，不会悄悄缩量。</p>
    </div>

    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">委托</h3>
      <table class="w-table" v-if="orders.length">
        <thead>
          <tr><th>决策日</th><th>代码</th><th>方向</th><th class="num">委托</th>
            <th class="num">已成交</th><th class="num">剩余</th><th class="num">限价</th>
            <th>状态</th><th>来源</th><th>理由 / 复盘</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="o in orders" :key="o.order_id">
            <td class="mono muted">{{ o.trade_date }}</td>
            <td class="mono">
              <router-link :to="stockLink(o.symbol)" class="mono">{{ o.symbol }}</router-link>
            </td>
            <td :class="o.side === 'buy' ? 'buy' : 'sell'">{{ o.side === 'buy' ? '买入' : '卖出' }}</td>
            <td class="num">{{ num(o.volume) }}</td>
            <td class="num">{{ num(o.filled_volume) }}</td>
            <td class="num">{{ num(o.remaining_volume) }}</td>
            <td class="num mono">{{ fmt2(o.price) }}</td>
            <td><span class="badge" :class="statusClass(o.status)">{{ STATUS_LABEL[o.status] || o.status }}</span>
              <span v-if="o.err_msg" class="muted" style="font-size:11px"> · {{ o.err_msg }}</span>
            </td>
            <td class="muted">{{ o.source === 'manual' ? '手动' : '策略' }}</td>
            <td class="muted" style="max-width:280px">
              <div v-if="o.thesis" style="font-size:12px">{{ o.thesis }}</div>
              <input v-model="o._note" type="text" maxlength="10000" placeholder="事后复盘"
                     style="width:100%;margin-top:2px" />
            </td>
            <td>
              <button class="btn" @click="saveReview(o)">存复盘</button>
              <button v-if="['pending','part'].includes(o.status)" class="btn"
                      @click="cancel(o)">撤余量</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有委托。</p>
    </div>

    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:8px">
        <h3 style="margin:0">持仓</h3>
        <span class="muted">{{ positions.length }} 只</span>
        <div class="spacer" />
        <button class="btn" :disabled="!acc" @click="exportCsv('nav')">导出净值</button>
        <button class="btn" :disabled="!acc" @click="exportCsv('positions')">导出持仓</button>
        <button class="btn" :disabled="!acc" @click="exportCsv('orders')">导出委托</button>
        <button class="btn" :disabled="!acc" @click="exportCsv('fills')">导出成交</button>
      </div>
      <table class="w-table" v-if="positions.length">
        <thead>
          <tr><th>代码</th><th class="num">股数</th><th class="num">可卖</th>
            <th class="num">成本价</th><th class="num">成本余额</th>
            <th class="num">收盘价</th><th>价格日</th><th class="num">市值</th>
            <th class="num">浮动盈亏</th><th class="num">权重</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in positions" :key="p.symbol">
            <td class="mono">
              <router-link :to="stockLink(p.symbol)" class="mono">{{ p.symbol }}</router-link>
            </td>
            <td class="num">{{ num(p.volume) }}</td>
            <td class="num muted">{{ num(p.available_volume) }}</td>
            <td class="num mono">{{ fmt2(p.avg_price) }}</td>
            <td class="num mono">{{ money(p.cost_basis) }}</td>
            <td class="num mono">{{ fmt2(p.close) }}</td>
            <td class="mono muted">{{ p.price_date || '—' }}</td>
            <td class="num">{{ money(p.market_value) }}</td>
            <td class="num" :class="p.unrealized_pnl >= 0 ? 'up' : 'down'">{{ money(p.unrealized_pnl) }}</td>
            <td class="num muted">{{ pct(p.weight) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">当前没有持仓。</p>
    </div>

    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">复盘（截至 {{ summary.to || '—' }}）</h3>
      <div class="stats" v-if="review.summary && review.summary.round_trips">
        <div class="stat"><span class="muted">回合数</span><strong>{{ review.summary.round_trips }}</strong></div>
        <div class="stat"><span class="muted">胜率</span><strong>{{ pct(review.summary.win_rate) }}</strong>
          <span class="sub muted">{{ review.summary.wins }} 盈 / {{ review.summary.losses }} 亏</span></div>
        <div class="stat"><span class="muted">盈亏比</span><strong>{{ num2(review.summary.profit_factor) }}</strong></div>
        <div class="stat"><span class="muted">平均持有</span><strong>{{ review.summary.avg_holding_days ?? '—' }} 日</strong></div>
        <div class="stat"><span class="muted">已实现</span>
          <strong :class="review.summary.realized_total >= 0 ? 'up' : 'down'">{{ money(review.summary.realized_total) }}</strong></div>
        <div class="stat"><span class="muted">浮动</span>
          <strong :class="review.summary.unrealized_total >= 0 ? 'up' : 'down'">{{ money(review.summary.unrealized_total) }}</strong></div>
        <div class="stat"><span class="muted">分红（含税）</span>
          <strong>{{ money(review.summary.dividend_income) }}</strong></div>
        <div class="stat"><span class="muted">累计费用</span><strong>{{ money(review.summary.fees_total) }}</strong></div>
        <div class="stat"><span class="muted">波动</span><strong>{{ pct(review.summary.volatility) }}</strong>
          <span class="sub muted">夏普 {{ num2(review.summary.sharpe) }}</span></div>
        <div class="stat"><span class="muted">年化</span><strong>{{ pct(review.summary.annualized) }}</strong></div>
        <div class="stat" v-if="review.benchmark"><span class="muted">超额（vs 沪深300）</span>
          <strong :class="review.benchmark.excess >= 0 ? 'up' : 'down'">{{ pct(review.benchmark.excess) }}</strong></div>
      </div>
      <p v-else class="muted">还没有已完成的回合（费用与已实现盈亏在成交时就落库，不靠重放）。</p>
      <div class="note muted" v-for="(n, i) in review.notes || []" :key="i">· {{ n }}</div>
    </div>

    <!-- 公司行为核对 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:8px">
        <h3 style="margin:0">公司行为核对</h3>
        <span class="muted">
          接口只给事件日期与类别，**没有**登记日/到账日/可卖日/个人分红税 ——
          这些必须按公告核对后填，否则账户会停在事件日之前（不会拿复权系数当真实现金）
        </span>
      </div>

      <div v-if="unresolved.length" class="warnbar soft inset" style="margin-bottom:8px">
        <div v-for="(u, i) in unresolved" :key="i">
          待核对：{{ u.symbol }} {{ u.event_date }} 类别 {{ u.category }}
          （{{ u.reason }}）
        </div>
      </div>

      <table class="w-table" v-if="events.length">
        <thead>
          <tr><th>代码</th><th>事件日</th><th>类别</th><th>原始字段</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="e in events" :key="`${e.symbol}-${e.event_date}-${e.category}`">
            <td class="mono">{{ e.symbol }}</td>
            <td class="mono">{{ e.event_date }}</td>
            <td>{{ e.category }}</td>
            <td class="mono muted" style="max-width:320px;overflow:hidden">{{ e.payload }}</td>
            <td>
              <span class="badge" :class="e.resolved ? 'ok' : 'warn'">
                {{ e.resolved ? '已核对' : '待核对' }}
              </span>
            </td>
            <td>
              <button class="btn" @click="openResolve(e)">填写核对表</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">这只账户涉及的股票还没有采集到公司行为事件。</p>

      <div v-if="resolveForm" class="resolve" style="margin-top:10px">
        <h4 style="margin:0 0 6px">
          {{ resolveForm.symbol }} {{ resolveForm.event_date }} 类别 {{ resolveForm.category }}
        </h4>
        <p class="muted note" style="margin-top:0">
          SDK 的每十股字段（类别 1）：D = 现金分红、P = 配股价、S = 送转、R = 配股。
          请按公告/对账单把下面的**账户总额**与日期填准。
        </p>
        <div class="flex items-center gap-2" style="flex-wrap:wrap">
          <label class="muted">登记日 <input v-model="resolveForm.record_date" type="date" /></label>
          <label class="muted">生效日 <input v-model="resolveForm.effective_date" type="date" /></label>
          <label class="muted">现金分红总额 <input v-model.number="resolveForm.cash_gross" type="number" step="0.01" style="width:110px" /></label>
          <label class="muted">到账日 <input v-model="resolveForm.cash_pay_date" type="date" /></label>
          <label class="muted">送转股数 <input v-model.number="resolveForm.share_delta" type="number" style="width:100px" /></label>
          <label class="muted">解锁日 <input v-model="resolveForm.share_available_date" type="date" /></label>
          <label class="muted">认购款总额 <input v-model.number="resolveForm.subscription_cash" type="number" step="0.01" style="width:110px" /></label>
          <label class="muted">缴款日 <input v-model="resolveForm.subscription_pay_date" type="date" /></label>
          <label class="muted">税核对截止
            <input v-model="resolveForm.taxes_checked_through" type="date"
                   title="截至这一天已核对的扣税；留空 = 尚未核对（会阻断推进）" />
          </label>
          <label class="muted">扣税日 <input v-model="resolveForm.tax_date" type="date" /></label>
          <label class="muted">扣税额 <input v-model.number="resolveForm.tax_amount" type="number" step="0.01" style="width:100px" /></label>
          <input v-model.trim="resolveForm.source_note" type="text" maxlength="1000"
                 placeholder="公告依据（必填）" style="width:260px" />
          <button class="btn primary" @click="submitResolve">登记</button>
          <button class="btn" @click="resolveForm = null">取消</button>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">停牌核对</h3>
      <p class="muted note" style="margin-top:0">
        某天缺日线又**不是**停牌时，账户会停在缺口前并提示 <code>raw_bar_missing</code>。
        按公告确认确实停牌后在这里登记，账户才能继续推进（估值沿用上一个有效原价）。
      </p>
      <div class="flex items-center gap-2" style="flex-wrap:wrap">
        <input v-model.trim="susp.symbol" type="text" maxlength="6" placeholder="代码" style="width:86px" />
        <input v-model="susp.date" type="date" />
        <input v-model.trim="susp.source_note" type="text" maxlength="1000"
               placeholder="公告依据" style="width:240px" />
        <button class="btn" @click="confirmSuspension">登记停牌</button>
      </div>
    </div>

    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:8px">
        <h3 style="margin:0">历史曲线归档</h3>
        <span class="muted">
          迁移前的曲线是 HFQ 研究口径，只读保留、不重算；
          新账户的留档就是它自己的完整流水（点上方「归档」冻结账户）
        </span>
      </div>
      <table class="w-table" v-if="archives.length">
        <thead><tr><th>#</th><th>名称</th><th>口径</th><th>区间</th><th class="num">收益</th><th class="num">天</th></tr></thead>
        <tbody>
          <tr v-for="a in archives" :key="a.id">
            <td class="mono">{{ a.id }}</td>
            <td>{{ a.label }}</td>
            <td class="muted">{{ a.price_basis === 'raw' ? '原价' : 'HFQ 旧口径' }}</td>
            <td class="mono muted">{{ a.from_date || '—' }} ~ {{ a.to_date || '—' }}</td>
            <td class="num" :class="a.total_return >= 0 ? 'up' : 'down'">{{ pct(a.total_return) }}</td>
            <td class="num muted">{{ a.days }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有归档。</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { portfolioApi } from '../api'
import { usePeriods } from '../composables/usePeriods'
import { stockLink as makeStockLink } from '../composables/useStockLinks'

const route = useRoute()
const router = useRouter()
const { periods } = usePeriods()

const KIND_LABEL = { manual: '手动', strategy: '策略', legacy_hfq: '旧研究账户' }
const STATUS_LABEL = { pending: '待成交', part: '部分成交', filled: '已成交',
                       canceled: '已撤', rejected: '已拒绝' }
const STATUS_CLASS = { filled: 'ok', part: 'warn', pending: 'dim',
                       canceled: 'dim', rejected: 'bad' }
const statusClass = (s) => STATUS_CLASS[s] ?? 'dim'

const accounts = ref([])
const acc = ref(null)
const summary = ref({})
const positions = ref([])
const orders = ref([])
const review = ref({})
const riskAlerts = ref([])
const events = ref([])
const unresolved = ref([])
const archives = ref([])
const dates = ref([])
const errMsg = ref('')
const msg = ref('')
const advancing = ref(false)
const submitting = ref(false)
const resolveForm = ref(null)

const q0 = route.query
const accountId = ref(Number(q0.account_id) || 0)
const viewDate = ref(typeof q0.date === 'string' ? q0.date : '')

const form = reactive({
  name: '', kind: 'manual', cash: 1000000, start_date: '',
  participation: 0.1, period: 60, threshold: 80, hold: 20, max_positions: 20,
  resonance: false, exclude_st: true, signal: 'rps',
})
const ord = reactive({ symbol: '', side: 'buy', volume: 100, price: null, thesis: '' })
const susp = reactive({ symbol: '', date: '', source_note: '' })
/** 每次「明确的一次提交」一个 request_id：重复点击/超时重试用同一个值。 */
let requestId = newUuid()

function newUuid() {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `r-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** 能推进：账户可操作即可（策略账户正是靠推进来结算的）。 */
const canAdvance = computed(() => !!acc.value && acc.value.status === 'active')

/** 能手工下单：只有手动账户，且必须停在最新视图（历史视图是只读的）。 */
const canOrder = computed(() => canAdvance.value && acc.value.kind === 'manual'
  && (!viewDate.value || viewDate.value === acc.value.latest_date))

const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN'))
const num2 = (v) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(2))
const fmt2 = (v) => (v == null ? '—' : Number(v).toFixed(2))
const money = (v) => (v == null || v === '' ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const pct = (v) => (v == null || Number.isNaN(Number(v)) ? '—'
  : `${(Number(v) * 100).toFixed(2)}%`)

function stockLink(symbol) {
  return makeStockLink(symbol, viewDate.value, 'portfolio')
}

/** 只改 query；App 以 fullPath 重挂载，由新实例统一加载一次。 */
function applyQuery() {
  router.replace({
    path: '/portfolio',
    query: {
      ...(accountId.value ? { account_id: accountId.value } : {}),
      ...(viewDate.value ? { date: viewDate.value } : {}),
    },
  })
}

const navOption = computed(() => {
  const curve = summary.value.curve || []
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 62, right: 30, top: 24, bottom: 36 },
    xAxis: { type: 'category', data: curve.map((x) => x.trade_date),
             axisLabel: { color: '#8b98a9', fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#8b98a9', fontSize: 10 } },
    series: [{
      name: '净值', type: 'line', data: curve.map((x) => x.nav), showSymbol: false,
      lineStyle: { color: '#4c9aff', width: 2 },
      markLine: { silent: true, symbol: 'none',
        lineStyle: { color: '#8b98a9', type: 'dashed', width: 1 },
        data: [{ yAxis: 1 }, ...(viewDate.value ? [{ xAxis: viewDate.value }] : [])] },
    }],
  }
})

async function loadAll() {
  errMsg.value = ''
  try {
    accounts.value = (await portfolioApi.accounts()).rows || []
  } catch (e) {
    errMsg.value = `账户列表加载失败：${e?.message || e}`
    return
  }
  if (!accountId.value || !accounts.value.some((a) => a.id === accountId.value)) {
    // 默认选第一个可操作的账户；没有就什么都不选（历史 legacy 单列可看）
    const active = accounts.value.find((a) => a.status === 'active')
    accountId.value = (active || accounts.value[0] || {}).id || 0
  }
  if (!accountId.value) return
  const params = viewDate.value ? { date: viewDate.value } : undefined
  try {
    const d = await portfolioApi.detail(accountId.value, params)
    summary.value = d
    acc.value = d.account
    review.value = d.review || {}
    // 用 review.positions：它带浮动盈亏/权重/可卖（summary.positions 只有库里的裸行，
    // 直接绑上去会让「浮动盈亏」「权重」两列永远显示 —）。
    positions.value = d.review?.positions || d.positions || []
  } catch (e) {
    errMsg.value = `账户加载失败：${e?.message || e}`
    return
  }
  try {
    orders.value = ((await portfolioApi.orders(accountId.value, params)).rows || [])
      .map((o) => ({ ...o, _note: o.review_note || '' }))
  } catch { console.warn('委托加载失败') }
  try {
    dates.value = (await portfolioApi.dates(accountId.value)) || []
  } catch { dates.value = [] }
  try {
    riskAlerts.value = (await portfolioApi.risk(accountId.value, params)).alerts || []
  } catch { riskAlerts.value = [] }
  try {
    const a = await portfolioApi.actions(accountId.value)
    events.value = a.rows || []
    unresolved.value = a.unresolved || []
  } catch { events.value = []; unresolved.value = [] }
  try {
    archives.value = (await portfolioApi.archives({ limit: 20 })).rows || []
  } catch { archives.value = [] }
}

async function createAccount() {
  errMsg.value = ''; msg.value = ''
  const body = {
    name: form.name, kind: form.kind, cash: Number(form.cash),
    start_date: form.start_date, participation: Number(form.participation),
  }
  if (form.kind === 'strategy') {
    body.params = {
      period: Number(form.period), threshold: Number(form.threshold),
      hold: Number(form.hold), max_positions: Number(form.max_positions),
      resonance: !!form.resonance, exclude_st: !!form.exclude_st,
      signal: form.signal,
    }
  }
  try {
    const r = await portfolioApi.createAccount(body)
    msg.value = `已建账户 #${r.account.id}「${r.account.name}」`
    accountId.value = r.account.id
    viewDate.value = ''
    form.name = ''
    applyQuery()
  } catch (e) {
    errMsg.value = `新建账户失败：${e?.message || e}`
  }
}

function copyParams() {
  if (!acc.value) return
  form.kind = acc.value.kind === 'strategy' ? 'strategy' : 'manual'
  form.cash = acc.value.initial_cash
  form.start_date = acc.value.start_date || ''
  form.participation = acc.value.execution?.participation ?? 0.1
  const p = acc.value.params
  if (p) {
    form.period = p.period; form.threshold = p.threshold; form.hold = p.hold
    form.max_positions = p.max_positions; form.resonance = p.resonance
    form.exclude_st = p.exclude_st; form.signal = p.signal
  }
  form.name = `${acc.value.name}-副本`
  msg.value = '参数已复制到「新建账户」，确认后点新建（不会改动原账户）'
}

async function archive() {
  if (!window.confirm('归档后不能再下单或推进（历史全部保留）。确认？')) return
  try {
    await portfolioApi.archiveAccount(accountId.value)
    await loadAll()
  } catch (e) {
    errMsg.value = `归档失败：${e?.message || e}`
  }
}

async function advance(toDate) {
  advancing.value = true
  errMsg.value = ''; msg.value = ''
  try {
    const r = await portfolioApi.advance(accountId.value, toDate)
    if (r.blocked_reason) {
      const miss = (r.missing || []).slice(0, 5)
        .map((m) => m.symbol ? `${m.symbol}@${m.date}` : JSON.stringify(m)).join('、')
      msg.value = `推进到 ${r.settled_through || '（尚未开始）'}（新增 ${r.days} 天）；`
        + `数据缺口挡住继续：${r.blocked_reason}${miss ? ` · ${miss}` : ''}`
    } else {
      msg.value = `已结算到 ${r.settled_through}（新增 ${r.days} 个交易日）`
    }
    await loadAll()
  } catch (e) {
    errMsg.value = `推进失败：${e?.message || e}`
  } finally {
    advancing.value = false
  }
}

async function submitOrder() {
  submitting.value = true
  errMsg.value = ''; msg.value = ''
  try {
    await portfolioApi.submitOrder(accountId.value, {
      request_id: requestId,
      symbol: ord.symbol, side: ord.side,
      volume: Number(ord.volume), price: Number(ord.price),
      thesis: ord.thesis,
    })
    // 成功之后才换新的 request_id：重复点击/超时重试都复用同一个
    requestId = newUuid()
    ord.thesis = ''
    msg.value = '委托已登记，将在下一个市场交易日撮合'
    await loadAll()
  } catch (e) {
    errMsg.value = `下单失败：${e?.message || e}`
  } finally {
    submitting.value = false
  }
}

async function cancel(o) {
  try {
    await portfolioApi.cancelOrder(accountId.value, o.order_id)
    await loadAll()
  } catch (e) {
    errMsg.value = `撤单失败：${e?.message || e}`
  }
}

async function saveReview(o) {
  try {
    await portfolioApi.reviewOrder(accountId.value, o.order_id, o._note || '')
    o.review_note = o._note || ''
    msg.value = '复盘文本已保存'
  } catch (e) {
    errMsg.value = `复盘保存失败：${e?.message || e}`
  }
}

function openResolve(e) {
  resolveForm.value = {
    symbol: e.symbol, event_date: e.event_date, category: e.category,
    record_date: '', effective_date: e.event_date, cash_gross: 0, cash_pay_date: '',
    share_delta: 0, share_available_date: '',
    subscription_cash: 0, subscription_pay_date: '',
    taxes_checked_through: '', tax_date: '', tax_amount: 0, source_note: '',
  }
}

async function submitResolve() {
  const f = resolveForm.value
  const body = {
    symbol: f.symbol, event_date: f.event_date, category: Number(f.category),
    resolution: {
      record_date: f.record_date || null,
      effective_date: f.effective_date || null,
      cash_gross: Number(f.cash_gross) || 0,
      cash_pay_date: f.cash_pay_date || null,
      share_delta: Number(f.share_delta) || 0,
      share_unlocks: Number(f.share_delta) > 0
        ? [{ kind: 'bonus', volume: Number(f.share_delta),
             available_date: f.share_available_date || f.effective_date }]
        : [],
      subscription_cash: Number(f.subscription_cash) || 0,
      subscription_pay_date: f.subscription_pay_date || null,
      taxes: (Number(f.tax_amount) > 0 && f.tax_date)
        ? [{ date: f.tax_date, amount: Number(f.tax_amount) }] : [],
      taxes_checked_through: f.taxes_checked_through || null,
      source_note: f.source_note,
    },
  }
  try {
    await portfolioApi.registerAction(accountId.value, body)
    resolveForm.value = null
    msg.value = '权益方案已登记'
    await loadAll()
  } catch (e) {
    errMsg.value = `登记失败：${e?.message || e}`
  }
}

async function confirmSuspension() {
  try {
    await portfolioApi.confirmSuspension({ ...susp })
    susp.symbol = ''; susp.date = ''; susp.source_note = ''
    msg.value = '停牌已登记'
  } catch (e) {
    errMsg.value = `停牌登记失败：${e?.message || e}`
  }
}

async function exportCsv(kind) {
  try {
    await portfolioApi.exportCsv(accountId.value, kind,
      `paper_${kind}_${accountId.value}.csv`)
  } catch (e) {
    errMsg.value = `导出失败：${e?.message || e}`
  }
}

onMounted(loadAll)
</script>

<style scoped>
.spacer { flex: 1; }
.badge.ok { background: rgba(56, 189, 148, .18); color: var(--down); }
.badge.bad { background: rgba(246, 70, 93, .2); color: var(--up); }
.badge.warn { background: rgba(240, 160, 32, .18); color: var(--warn); }
.badge.dim { background: rgba(120, 132, 150, .16); color: var(--text-dim); }
.badge.mode { background: rgba(76, 154, 255, .18); color: var(--accent); }
.buy { color: var(--up); }
.sell { color: var(--down); }
.up { color: var(--up); }
.down { color: var(--down); }
.chk { display: flex; align-items: center; gap: 5px; color: var(--text-dim); font-size: 13px; }
.note { font-size: 12px; line-height: 1.7; margin: 8px 0 0; }
.tip code { background: var(--bg-panel-2); padding: 1px 5px; border-radius: 3px; }
.chart { height: 320px; margin-top: 10px; }
.w-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.w-table th {
  text-align: left; font-weight: 500; color: var(--text-dim);
  border-bottom: 1px solid var(--border); padding: 6px 8px; font-size: 12px;
}
.w-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.w-table .num { text-align: right; }
.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.resolve { border-top: 1px solid var(--border); padding-top: 10px; }
</style>
