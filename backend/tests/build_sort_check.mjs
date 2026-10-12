/**
 * 生成「热力图按 20/60/120/250 各周期独立排序」的静态验证页。
 *
 * 为什么要单独生成：应用页面靠 fetch 拉数据再赋值，而当前这个内置浏览器
 * （FilePanel / Electron）在页面加载完成后会冻结渲染进程 —— 定时器、
 * Promise 回调、点击事件都不再执行，只有「加载期同步渲染」还能工作。
 * 所以把真实接口数据直接内联进 HTML，用与 Sectors.vue 完全相同的
 * option 构造逻辑同步渲染，四个周期各出一张图，用来肉眼验证排序。
 *
 * 产物自包含（echarts 也内联），双击即可在任意浏览器打开。
 *
 * 用法：node backend/tests/build_sort_check.mjs
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const API = process.env.RPS_API ?? 'http://127.0.0.1:8000'
const PERIODS = [20, 60, 120, 250]
const TOP_N = 26
/** 板块强弱页的行高与 Sectors.vue 的 chartHeight 公式保持一致 */
const ROW_H = 19
const CHART_H = Math.max(420, TOP_N * ROW_H + 70)

const echartsPath = [
  'frontend/node_modules/echarts/dist/echarts.min.js',
  'frontend/node_modules/echarts/dist/echarts.js',
].map((p) => resolve(ROOT, p)).find((p) => {
  try { readFileSync(p); return true } catch { return false }
})
if (!echartsPath) {
  console.error('找不到 echarts 发行包，先在 frontend/ 下执行 npm install')
  process.exit(1)
}
const echartsSrc = readFileSync(echartsPath, 'utf8')

const res = await fetch(`${API}/api/rps/heatmap`)
if (!res.ok) {
  console.error(`接口请求失败: ${res.status} ${API}/api/rps/heatmap`)
  process.exit(1)
}
const payload = await res.json()
const matrix = payload.matrix ?? []
if (!matrix.length) {
  console.error('接口返回的 matrix 为空，无法生成验证页')
  process.exit(1)
}

/** 与 Sectors.vue 的 shortName() 一致：去掉证监会行业分类代码前缀 */
const shortName = (s) => String(s ?? '').replace(/^[A-Z]\d{2}/, '')

/** 与 Sectors.vue 的 sortedByPeriod 一致：按指定周期降序，缺失值沉底 */
function sortedBy(p) {
  const key = `rps${p}`
  return [...matrix].sort((a, b) => {
    const va = a[key]
    const vb = b[key]
    if (va === null || va === undefined) return 1
    if (vb === null || vb === undefined) return -1
    return vb - va
  })
}

/** 校验：四个周期必须给出四种不同的次序，否则说明排序没生效 */
const orders = PERIODS.map((p) => sortedBy(p).map((r) => r.industry))
const distinct = new Set(orders.map((o) => o.join('|'))).size
console.log(`行业数 ${matrix.length}，数据日期 ${payload.data_date}`)
console.log(`四个周期产生 ${distinct} 种不同排序${distinct === 4 ? '（符合预期）' : '（异常！）'}`)
for (let i = 0; i < PERIODS.length; i += 1) {
  const p = PERIODS[i]
  console.log(`  RPS${p} 前 5：${orders[i].slice(0, 5).map(shortName).join(' / ')}`)
}
if (distinct !== 4) process.exit(1)

/** 与 Sectors.vue 的 heatmapOption 完全同构，只是数据内联 */
function chartOption(p, rows) {
  const activeX = PERIODS.indexOf(p)
  const cols = PERIODS.map((x) => `RPS${x}`)
  const data = []
  rows.forEach((r, y) => {
    // 注意：x 必须是**类别索引**（0~3），不能拿周期值 20/60/120/250 当索引。
    // x 轴 data 是 ['RPS20','RPS60','RPS120','RPS250']，只认 0~3；
    // 一旦写成周期值，所有格子都会越界、一个都画不出来。
    PERIODS.forEach((p, x) => {
      const v = r[`rps${p}`]
      data.push([x, y, v === null || v === undefined ? '-' : v, r.industry, x === activeX])
    })
  })
  return {
    // 83 行热力图做入场动画只会晃眼，直接出终态。
    animation: false,
    tooltip: {
      formatter: (q) => {
        const [x, , v, name, isActive] = q.data
        const extra = isActive ? '<br/><b>（当前排序依据）</b>' : ''
        return `${name}<br/>${cols[x]}：${v === '-' ? '无数据' : v}${extra}`
      },
    },
    grid: { left: 8, right: 16, top: 12, bottom: 58, containLabel: true },
    xAxis: {
      type: 'category', data: cols, position: 'top', splitArea: { show: false },
      axisLabel: {
        color: (val) => (val === `RPS${p}` ? '#4c9aff' : '#8b98a9'),
        fontSize: 11,
        fontWeight: (val) => (val === `RPS${p}` ? 'bold' : 'normal'),
        formatter: (val) => (val === `RPS${p}` ? `${val} ↓` : val),
      },
      axisLine: { lineStyle: { color: '#2a323d' } },
    },
    yAxis: {
      type: 'category',
      data: rows.map((r) => shortName(r.industry)),
      inverse: true,
      axisLabel: { color: '#8b98a9', fontSize: 10, width: 120, overflow: 'truncate' },
      axisLine: { show: false }, axisTick: { show: false },
    },
    visualMap: {
      dimension: 2,
      min: 0, max: 100, calculable: true, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: 12, itemHeight: 140,
      text: ['强 100', '弱 0'],
      textStyle: { color: '#8b98a9', fontSize: 10 },
      inRange: { color: ['#2ebd85', '#1f6f52', '#4c9aff', '#f0a020', '#f6465d'] },
    },
    series: [{
      type: 'heatmap', data,
      itemStyle: { borderColor: '#171c24', borderWidth: 1 },
      label: { show: true, fontSize: 10, formatter: (q) => (q.value[2] === '-' ? '' : Math.round(q.value[2])) },
      emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1 } },
    }],
  }
}

function page(p) {
  const rows = sortedBy(p).slice(0, TOP_N)
  const first = shortName(rows[0].industry)
  const last = shortName(rows[rows.length - 1].industry)
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>热力图按 RPS${p} 独立排序 · ${payload.data_date}</title>
<style>
  html,body{margin:0;background:#0d1117;color:#e6edf3;
    font:14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .wrap{padding:14px 18px}
  h1{margin:0 0 4px;font-size:17px}
  .meta{color:#8b98a9;font-size:12.5px;margin-bottom:10px}
  .meta b{color:#4c9aff}
  #c{height:${CHART_H}px;background:#131820;border:1px solid #2a323d;border-radius:8px}
  .foot{color:#6e7b8d;font-size:12px;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <h1>行业 × 周期 RPS 热力图 —— 行按 RPS${p} 降序独立排列</h1>
  <div class="meta">
    数据日期 <b>${payload.data_date}</b> ·
    共 <b>${matrix.length}</b> 个证监会行业，展示按 RPS${p} 排名前 <b>${TOP_N}</b> 名 ·
    榜首 <b>${first}</b>，末位 <b>${last}</b> ·
    四个周期各自独立排序（RPS20 与 RPS250 名次几乎不相关）
  </div>
  <div id="c"></div>
  <div class="foot">蓝色 ↓ 标记的列即当前排序依据；同一行业在不同周期下的名次会大幅变化，这正是分周期维护强弱的意义。</div>
</div>
<script>${echartsSrc}</script>
<script>
  // 这里内联的是**函数源码**而不是 JSON：option 里的 axisLabel.color /
  // formatter、series.label.formatter、tooltip.formatter 都是回调函数，
  // JSON.stringify 会把它们静默丢掉（↓ 标记和格子数值就是这么消失的）。
  const PERIODS = ${JSON.stringify(PERIODS)}
  const shortName = ${shortName.toString()}
  ${chartOption.toString()}
  const ROWS = ${JSON.stringify(rows)}
  echarts.init(document.getElementById('c')).setOption(chartOption(${p}, ROWS))
</script>
</body>
</html>`
}

mkdirSync(resolve(ROOT, 'docs'), { recursive: true })
const out = []
for (const p of PERIODS) {
  const html = page(p)
  // docs/ 为正式产物；dist/ 放一份供本地 HTTP 访问（验证截图用）
  for (const dir of ['docs', 'frontend/dist']) {
    const file = resolve(ROOT, dir, `sector-sort-rps${p}.html`)
    writeFileSync(file, html, 'utf8')
    if (dir === 'docs') out.push(file)
  }
}
console.log('\n已生成：')
for (const f of out) console.log('  ' + f)