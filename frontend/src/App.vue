<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="logo">RPS</span>
        <span class="brand-text">A股相对强度看板</span>
      </div>
      <nav class="nav">
        <router-link to="/" :class="{ active: $route.path === '/' }">排行榜</router-link>
        <router-link to="/sectors" :class="{ active: $route.path === '/sectors' }">板块强弱</router-link>
        <router-link to="/backtest" :class="{ active: $route.path === '/backtest' }">策略回测</router-link>
        <router-link to="/watchlist" :class="{ active: $route.path === '/watchlist' }">自选预警</router-link>
      </nav>
      <div class="status">
        <span class="dot" :class="statusDot"></span>
        <span class="muted">
          {{ statusText }}
          <span v-if="updating" class="phase"> · {{ phaseLabel }}</span>
          <span v-else-if="updateError" class="phase err">{{ updateError }}</span>
          <span v-else-if="lastUpdate" class="phase"> · 上次更新 {{ lastUpdate }}</span>
        </span>
        <button class="btn" :disabled="updating" @click="refresh">
          {{ updating ? '更新中…' : '更新数据' }}
        </button>
      </div>
    </header>

    <div v-if="unmapped > 0 && dbReady" class="warnbar soft">
      ℹ {{ unmapped }} 只股票暂无行业分类，已被排除在板块 RPS 之外，个股页显示「未分类」。
    </div>

    <main class="content">
      <router-view v-if="dbReady" :key="$route.fullPath" />
      <div v-else class="empty-guide panel">
        <h2>数据尚未初始化</h2>
        <p>数据库为空。请在项目根目录依次执行：</p>
        <pre>
.venv\Scripts\python.exe -m rpsboard.cli init
.venv\Scripts\python.exe -m rpsboard.cli backfill
.venv\Scripts\python.exe -m rpsboard.cli compute-rps</pre>
        <p class="muted">首次 backfill 约需 12~20 分钟（拉取约 5200 只 A 股一年半历史）。</p>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { metaApi, adminApi } from './api'

/** 给任何请求加硬超时兜底：请求挂起时不能让整页永远停在「加载中」 */
function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(`请求超时 ${ms}ms`)), ms)),
  ])
}

/**
 * 轮询拉状态，而不是只在 onMounted 调一次。
 *
 * 实测本环境下偶发请求长期 pending：偶尔整页会停在「连接中…」且不会自愈，
 * 需要手动刷新才恢复。改为定时重试后，即使某次请求挂起，页面也能自行恢复。
 */
let timer = null

async function load() {
  try {
    status.value = await withTimeout(metaApi.status(), 15000)
  } catch (e) {
    console.warn('读取状态失败，将自动重试：', e?.message || e)
    status.value = null
  }
}

function startPolling() {
  if (timer) return
  timer = setInterval(() => {
    // 已有正常数据时降低频率，避免无谓请求
    if (status.value?.db_ready) return
    load()
  }, 5000)
}

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

const status = ref(null)
const updating = ref(false)
const phase = ref(null)
const updateError = ref('')
const lastUpdate = ref('')
let pollTimer = null

// 阶段名必须与后端 run_update_pipeline 的 say(...) 一一对应：漏掉的阶段会把
// 英文 key 直接显示给用户（industry / patterns / alerts 此前就是这样）。
const PHASES = {
  starting: '准备中',
  bars: '拉取日K（约 7 分钟）',
  limits: '采集涨跌停（约 35 秒）',
  industry: '补行业分类',
  rps: '重算 RPS',
  patterns: '识别三升底',
  alerts: '评估预警规则',
  quality: '复权异常扫描（约 20 秒）',
  done: '完成',
  failed: '失败',
}
const phaseLabel = computed(() => PHASES[phase.value] || phase.value || '')

async function pollStatus() {
  try {
    const s = await adminApi.updateStatus()
    phase.value = s.phase
    if (!s.running && s.phase === 'failed') {
      updating.value = false
      updateError.value = s.error || '未知错误'
      stopPoll()
      return
    }
    if (!s.running && s.phase === 'done') {
      updating.value = false
      lastUpdate.value = `${s.finished_at || ''}${s.elapsed ? ` · 耗时 ${s.elapsed}s` : ''}`
      updateError.value = ''
      stopPoll()
      load()
      return
    }
    // 进程重启后前端不知道上一轮是否跑完，兜底展示持久化的最后一次采集
    if (!s.running && !s.phase && s.last_collect_log?.finished_at) {
      lastUpdate.value = s.last_collect_log.finished_at.replace('T', ' ')
    }
  } catch (e) {
    updating.value = false
    updateError.value = `无法获取更新状态：${e.message}`
    stopPoll()
  }
}

function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(pollStatus, 3000)
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function refresh() {
  updating.value = true
  updateError.value = ''
  phase.value = 'starting'
  try {
    const r = await adminApi.update()
    if (!r.ok) {
      updating.value = false
      updateError.value = r.error || '启动失败'
      return
    }
    startPoll()
  } catch (e) {
    updating.value = false
    updateError.value = e.message
  }
}

const dbReady = computed(() => !!status.value?.db_ready)
const unmapped = computed(() => status.value?.unmapped_symbols || 0)

const statusDot = computed(() => (dbReady.value ? 'ok' : 'bad'))

const statusText = computed(() => {
  const s = status.value
  if (!s) return '连接中…'
  if (!s.db_ready) return '数据库未初始化'
  const n = s.bar_rows || 0
  const parts = [`日K ${s.last_bar_date || '—'} · ${n.toLocaleString()} 行`]
  // 数据质量跟顶栏一起显示（涨跌停实测覆盖 / 概念覆盖 / 复权异常计数）。
  // 这些数字以前只在 CLI 里，页面用户不知道「回测为什么没剔涨停」、
  // 「为什么有些概念没有 RPS」。缺数据时字段为 null，静默跳过即可。
  const q = s.quality || {}
  const lim = q.price_limit
  if (lim?.days) {
    parts.push(`涨跌停实测 ${lim.days}/${lim.total_days || lim.days} 天`)
  }
  if (q.concept?.total) parts.push(`概念 ${q.concept.with_rps}/${q.concept.total}`)
  const ev = q.adjust_events || {}
  const anomalies = (ev['internal/hfq_jump'] || 0)
    + (ev['internal/tiny_price'] || 0)
    + (ev['internal/nonpositive_price'] || 0)
  if (anomalies) parts.push(`复权异常 ${anomalies.toLocaleString()} 处`)
  return parts.join(' · ')
})

// 旧的 refresh（同步调用 collect）已被上面的异步版本取代：
// 完整更新要 209+10 秒，同步 HTTP 必然超时，且会阻塞服务。

onMounted(() => {
  load()
  startPolling()
  pollStatus()   // 页面刷新后恢复上一轮更新的进度显示
})
onUnmounted(() => {
  stopPolling()
  stopPoll()
})
</script>

<style scoped>
.app-shell { display: flex; flex-direction: column; height: 100%; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 24px; padding: 0 20px; height: 56px;
  background: var(--bg-panel); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo {
  background: var(--accent); color: #fff; font-weight: 700;
  padding: 3px 9px; border-radius: 5px; font-size: 13px; letter-spacing: .5px;
}
.brand-text { font-weight: 600; letter-spacing: .3px; }

.nav { display: flex; gap: 4px; }
.nav a {
  padding: 6px 14px; border-radius: 6px; color: var(--text-dim); font-size: 14px;
}
.nav a:hover { color: var(--text); background: var(--bg-panel-2); }
.nav a.active { color: #fff; background: var(--bg-panel-2); }

.status { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.status .phase { color: var(--accent); }
.status .phase.err { color: var(--up); }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.ok { background: var(--down); }
.dot.warn { background: var(--warn); }
.dot.bad { background: var(--up); }

/* .warnbar / .warnbar.soft 已移到 src/style.css（Sectors 等视图也要用） */

.content { flex: 1; overflow: auto; padding: 18px 20px; }

.empty-guide { max-width: 640px; margin: 60px auto; }
.empty-guide h2 { margin-top: 0; }
.empty-guide pre {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 14px; overflow-x: auto;
  font-size: 12px; line-height: 1.9;
}
</style>
