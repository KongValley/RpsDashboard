<template>
  <div class="tools">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

    <div class="panel">
      <div class="flex items-center gap-3" style="margin-bottom:10px">
        <h2 style="margin:0">交易工具</h2>
        <span class="muted">
          仓位与费用是**纯计算**（不读行情、不查证券表），现金口径用同一套费用模型
          （最低佣金 5 元、印花税按日分段、过户费），所以算出来的数量拿去下单不会被拒
        </span>
      </div>
      <p class="muted note" style="margin-top:0">
        工具里的「止损」只是**预算假设**，不是保证成交的止损单；
        压力测试是**静态价格情景**，不是 VaR、也不是发生概率预测。
      </p>
    </div>

    <!-- 仓位预算 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:8px">
        <h3 style="margin:0">仓位 / 止损预算</h3>
        <span class="muted">按「这一笔最多亏权益的 risk%」反推可买数量</span>
        <div class="spacer" />
        <button class="btn" :disabled="!accounts.length" @click="fromAccount">用某个账户的资金</button>
      </div>
      <div class="flex items-center gap-2" style="flex-wrap:wrap">
        <label class="muted">代码 <input v-model.trim="ps.symbol" type="text" maxlength="6" style="width:86px" /></label>
        <label class="muted">权益 <input v-model.number="ps.equity" type="number" min="1" style="width:120px" /></label>
        <label class="muted">可用现金 <input v-model.number="ps.cash" type="number" min="0" style="width:120px" /></label>
        <label class="muted">买入价 <input v-model.number="ps.entry" type="number" min="0.01" step="0.01" style="width:90px" /></label>
        <label class="muted">止损价 <input v-model.number="ps.stop" type="number" min="0.01" step="0.01" style="width:90px" /></label>
        <label class="muted">风险 <input v-model.number="ps.risk_pct" type="number" min="0.001" max="1" step="0.001" style="width:80px" /></label>
        <label class="muted">单票上限 <input v-model.number="ps.max_weight" type="number" min="0.01" max="1" step="0.01" style="width:80px" /></label>
        <label class="muted">日期 <input v-model="ps.trade_date" type="date" /></label>
        <button class="btn primary" @click="runPositionSize">计算</button>
      </div>
      <div v-if="psResult" class="stats" style="margin-top:10px">
        <div class="stat"><span class="muted">可买数量</span>
          <strong>{{ num(psResult.volume) }}</strong>
          <span class="sub muted">{{ psResult.board_label }}</span></div>
        <div class="stat"><span class="muted">金额</span><strong>{{ money(psResult.notional) }}</strong></div>
        <div class="stat"><span class="muted">买入费用</span><strong>{{ money(psResult.buy_fee) }}</strong></div>
        <div class="stat"><span class="muted">止损卖出费用</span><strong>{{ money(psResult.stop_sell_fee) }}</strong></div>
        <div class="stat"><span class="muted">到止损共亏</span>
          <strong class="down">{{ money(psResult.loss_at_stop) }}</strong>
          <span class="sub muted">预算 {{ money(psResult.risk_budget) }}</span></div>
        <div class="stat"><span class="muted">剩余现金</span><strong>{{ money(psResult.remaining_cash) }}</strong></div>
        <div class="stat"><span class="muted">占权益</span><strong>{{ pctText(psResult.weight) }}</strong></div>
      </div>
      <p v-if="psResult && psResult.reason !== 'ok'" class="muted note">
        连最小申报单位都买不起（{{ psResult.reason }}）——不会向上凑整。
      </p>
    </div>

    <!-- 往返成本 -->
    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">往返成本 / 保本价</h3>
      <div class="flex items-center gap-2" style="flex-wrap:wrap">
        <label class="muted">代码 <input v-model.trim="rt.symbol" type="text" maxlength="6" style="width:86px" /></label>
        <label class="muted">股数 <input v-model.number="rt.volume" type="number" min="1" style="width:96px" /></label>
        <label class="muted">买入价 <input v-model.number="rt.buy_price" type="number" min="0.01" step="0.01" style="width:90px" /></label>
        <label class="muted">卖出价 <input v-model.number="rt.sell_price" type="number" min="0.01" step="0.01" style="width:90px" /></label>
        <label class="muted">日期 <input v-model="rt.trade_date" type="date" /></label>
        <button class="btn primary" @click="runRoundTrip">计算</button>
      </div>
      <div v-if="rtResult" class="stats" style="margin-top:10px">
        <div class="stat"><span class="muted">买入费用</span><strong>{{ money(rtResult.buy_fee) }}</strong></div>
        <div class="stat"><span class="muted">卖出费用</span><strong>{{ money(rtResult.sell_fee) }}</strong></div>
        <div class="stat"><span class="muted">费用合计</span><strong>{{ money(rtResult.total_fees) }}</strong></div>
        <div class="stat"><span class="muted">净盈亏</span>
          <strong :class="rtResult.net_pnl >= 0 ? 'up' : 'down'">{{ money(rtResult.net_pnl) }}</strong></div>
        <div class="stat"><span class="muted">净收益率</span>
          <strong :class="rtResult.net_pnl >= 0 ? 'up' : 'down'">{{ pctText(rtResult.net_return) }}</strong>
          <span class="sub muted">分母 = 买入金额 + 买入费用</span></div>
        <div class="stat"><span class="muted">保本价</span>
          <strong class="mono">{{ fmt2(rtResult.breakeven_price) }}</strong>
          <span class="sub muted">0.01 元网格上第一个不亏的价</span></div>
      </div>
    </div>

    <!-- 压力测试 -->
    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:8px">
        <h3 style="margin:0">持仓压力测试</h3>
        <span class="muted">给定各股涨跌幅，静态重算权益</span>
        <div class="spacer" />
        <button class="btn" :disabled="!stressAccountId" @click="fillAll(-0.05)">全填 -5%</button>
        <button class="btn" :disabled="!stressAccountId" @click="fillAll(-0.10)">全填 -10%</button>
        <button class="btn" :disabled="!stressAccountId" @click="fillAll(-0.20)">全填 -20%</button>
      </div>
      <div class="flex items-center gap-2" style="flex-wrap:wrap;margin-bottom:8px">
        <label class="muted">账户
          <select v-model.number="stressAccountId" @change="loadStressPositions">
            <option :value="0" disabled>选择账户…</option>
            <option v-for="a in accounts" :key="a.id" :value="a.id">
              #{{ a.id }} {{ a.name }}
            </option>
          </select>
        </label>
        <label class="muted">日期 <input v-model="stressDate" type="date" /></label>
        <button class="btn primary" :disabled="!stressAccountId" @click="runStress">测试</button>
      </div>
      <table class="w-table" v-if="stressRows.length">
        <thead><tr><th>代码</th><th class="num">市值</th><th class="num">冲击</th></tr></thead>
        <tbody>
          <tr v-for="r in stressRows" :key="r.symbol">
            <td class="mono">{{ r.symbol }}</td>
            <td class="num">{{ money(r.market_value) }}</td>
            <td class="num">
              <input v-model.number="r.shock" type="number" min="-1" max="10" step="0.01"
                     style="width:96px" />
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="stressResult" class="stats" style="margin-top:10px">
        <div class="stat"><span class="muted">测试日</span><strong class="mono">{{ stressResult.date }}</strong></div>
        <div class="stat"><span class="muted">权益（前）</span><strong>{{ money(stressResult.equity_before) }}</strong></div>
        <div class="stat"><span class="muted">盈亏</span>
          <strong :class="stressResult.pnl >= 0 ? 'up' : 'down'">{{ money(stressResult.pnl) }}</strong></div>
        <div class="stat"><span class="muted">权益（后）</span><strong>{{ money(stressResult.equity_after) }}</strong></div>
        <div class="stat"><span class="muted">变化</span>
          <strong :class="stressResult.return >= 0 ? 'up' : 'down'">{{ pctText(stressResult.return) }}</strong></div>
      </div>
      <p v-else class="muted">选一个账户后会列出它的持仓，逐只填冲击再点测试。</p>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { portfolioApi, toolsApi } from '../api'

const accounts = ref([])
const errMsg = ref('')
const psResult = ref(null)
const rtResult = ref(null)
const stressResult = ref(null)
const stressAccountId = ref(0)
const stressDate = ref('')
const stressRows = ref([])

const ps = reactive({
  symbol: '000001', equity: 100000, cash: 20000, entry: 10.0, stop: 9.0,
  risk_pct: 0.01, max_weight: 0.25, trade_date: '2026-01-05',
})
const rt = reactive({
  symbol: '000001', volume: 1000, buy_price: 10.0, sell_price: 11.0,
  trade_date: '2026-01-05',
})

const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN'))
const fmt2 = (v) => (v == null ? '—' : Number(v).toFixed(2))
const money = (v) => (v == null || v === '' ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const pctText = (v) => (v == null || Number.isNaN(Number(v)) ? '—'
  : `${(Number(v) * 100).toFixed(2)}%`)

async function runPositionSize() {
  errMsg.value = ''
  psResult.value = null
  try {
    psResult.value = await toolsApi.positionSize({ ...ps })
  } catch (e) {
    errMsg.value = `仓位计算失败：${e?.message || e}`
  }
}

async function runRoundTrip() {
  errMsg.value = ''
  rtResult.value = null
  try {
    rtResult.value = await toolsApi.roundTrip({ ...rt })
  } catch (e) {
    errMsg.value = `费用计算失败：${e?.message || e}`
  }
}

/**
 * 从某个账户带入资金口径：权益取**截止日总权益**，现金取**可用现金**
 * （已扣掉未成交委托的冻结）。应收/应付不算可花的钱。
 */
async function fromAccount() {
  const acc = accounts.value.find((a) => a.status === 'active')
  if (!acc) return
  try {
    const d = await portfolioApi.detail(acc.id)
    ps.equity = d.total ?? ps.equity
    ps.cash = Math.max(0, d.available_cash ?? d.cash ?? 0)
    errMsg.value = `已用账户 #${acc.id}（截至 ${d.to || '—'}）：权益 `
      + `${money(d.total)}，可用现金 ${money(ps.cash)}（已扣冻结 ${money(d.frozen_cash)}）`
  } catch (e) {
    errMsg.value = `读取账户失败：${e?.message || e}`
  }
}

async function loadStressPositions() {
  errMsg.value = ''
  stressResult.value = null
  if (!stressAccountId.value) return
  try {
    const d = await portfolioApi.detail(
      stressAccountId.value, stressDate.value ? { date: stressDate.value } : undefined)
    stressRows.value = (d.positions || []).map((p) => ({
      symbol: p.symbol, market_value: p.market_value, shock: 0,
    }))
  } catch (e) {
    errMsg.value = `读取持仓失败：${e?.message || e}`
    stressRows.value = []
  }
}

function fillAll(v) {
  stressRows.value = stressRows.value.map((r) => ({ ...r, shock: v }))
}

async function runStress() {
  errMsg.value = ''
  stressResult.value = null
  try {
    stressResult.value = await toolsApi.stress({
      account_id: stressAccountId.value,
      date: stressDate.value || null,
      shocks: stressRows.value
        .filter((r) => Number(r.shock) !== 0)
        .map((r) => ({ symbol: r.symbol, return: Number(r.shock) })),
    })
  } catch (e) {
    errMsg.value = `压力测试失败：${e?.message || e}`
  }
}

onMounted(async () => {
  try {
    accounts.value = (await portfolioApi.accounts()).rows || []
  } catch {
    accounts.value = []
  }
})
</script>

<style scoped>
.spacer { flex: 1; }
.note { font-size: 12px; line-height: 1.7; }
.up { color: var(--up); }
.down { color: var(--down); }
.chart { height: 300px; }
.w-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.w-table th {
  text-align: left; font-weight: 500; color: var(--text-dim);
  border-bottom: 1px solid var(--border); padding: 6px 8px; font-size: 12px;
}
.w-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.w-table .num { text-align: right; }
.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
</style>
