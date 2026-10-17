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

    <!-- 自建模拟盘（多账户、原价撮合）已迁到「模拟持仓」页 -->
    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">自建模拟盘已迁到独立页面</h3>
      <p class="muted tip">
        现在支持<b>多个账户</b>（手动 / 策略各自一条曲线）、用<b>不复权原价</b>记账，
        本地委托与成交存在独立的表里，不再与券商数据混在一张表上。
        请到 <router-link to="/portfolio">模拟持仓</router-link> 使用；
        仓位/费用/压力测试在 <router-link to="/tools">交易工具</router-link>。
      </p>
      <p class="muted tip">
        命令行等价入口：<code>cli trade account list|new</code>、
        <code>cli trade run --account N</code>、<code>cli trade paper --account N</code>、
        <code>cli trade risk --account N</code>。
      </p>
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

/**
 * QMT（迅投 miniQMT）交易接入页：**只读库 + 两个写入口**。
 *
 * 自建模拟盘（多账户、原价撮合）已经不在这里了 —— 它迁到了 `/portfolio`。
 * 本页只显示券商侧的东西：账户/资金（来自最近一次成功 `cli trade status`）、
 * 持仓快照、交易计划、券商委托与成交、对账差异。
 */
const s = ref(null)
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
const money = (v) => (v == null || v === '' ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))

const STATUS_CLASS = {
  filled: 'ok', executed: 'ok', submitted: 'dim', planned: 'dim',
  pending: 'dim', canceling: 'warn', part: 'warn', partial: 'warn',
  canceled: 'dim', rejected: 'bad', halted: 'bad', unknown: 'bad',
}
const statusClass = (x) => STATUS_CLASS[x] ?? 'dim'

async function reload() {
  try {
    s.value = await tradeApi.summary()
    errMsg.value = ''
  } catch (e) {
    errMsg.value = `交易状态加载失败：${e?.message || e}`
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
