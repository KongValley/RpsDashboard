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
      </nav>
      <div class="status">
        <span class="dot" :class="statusDot"></span>
        <span class="muted">{{ statusText }}</span>
        <button class="btn" :disabled="collecting" @click="refresh">
          {{ collecting ? '更新中…' : '更新数据' }}
        </button>
      </div>
    </header>

    <div v-if="unmapped > 0 && dbReady" class="warnbar soft">
      ℹ {{ unmapped }} 只股票暂无行业分类，已被排除在板块 RPS 之外，个股页显示「未分类」。
    </div>

    <div v-if="spotStale && dbReady" class="warnbar">
      ⚠ 当日快照（成交额/换手率/量比/市值）未更新或已过期 —— RPS 数值不受影响，仅展示字段缺失。
      行情快照来自 akshare（东方财富），受反爬限制可能失败。
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
const collecting = ref(false)

const dbReady = computed(() => !!status.value?.db_ready)
const spotStale = computed(() => !!status.value?.spot_stale)
const unmapped = computed(() => status.value?.unmapped_symbols || 0)

const statusDot = computed(() => {
  if (!dbReady.value) return 'bad'
  if (spotStale.value) return 'warn'
  return 'ok'
})

const statusText = computed(() => {
  const s = status.value
  if (!s) return '连接中…'
  if (!s.db_ready) return '数据库未初始化'
  const d = s.last_bar_date || '—'
  const n = s.bar_rows || 0
  return `日K ${d} · ${n.toLocaleString()} 行`
})

async function refresh() {
  collecting.value = true
  try {
    await adminApi.collect('compute-rps')
    await load()
  } catch (e) {
    alert(`更新失败：${e.message}`)
  } finally {
    collecting.value = false
  }
}

onMounted(() => {
  load()
  startPolling()
})
onUnmounted(stopPolling)
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
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.ok { background: var(--down); }
.dot.warn { background: var(--warn); }
.dot.bad { background: var(--up); }

.warnbar {
  background: rgba(240, 160, 32, .12); color: var(--warn);
  border-bottom: 1px solid rgba(240, 160, 32, .3);
  padding: 7px 20px; font-size: 12px; flex-shrink: 0;
}
/* 数据缺口提示：不是故障，用中性色，别跟真正的采集告警混淆 */
.warnbar.soft {
  background: rgba(120, 132, 150, .12); color: var(--text-dim);
  border-bottom-color: rgba(120, 132, 150, .3);
}

.content { flex: 1; overflow: auto; padding: 18px 20px; }

.empty-guide { max-width: 640px; margin: 60px auto; }
.empty-guide h2 { margin-top: 0; }
.empty-guide pre {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 14px; overflow-x: auto;
  font-size: 12px; line-height: 1.9;
}
</style>
