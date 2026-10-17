/**
 * 「回看某一天 → 点个股 → 返回」的日期穿透（node:test，零第三方依赖）。
 *
 * 守的是真实缺陷：个股页原来不从 URL 读日期，复盘页/排行榜/自选的链接也不带日期，
 * 于是「看 2024-05-06 的复盘 → 点某只票 → 返回」会悄悄回到最新一天。
 *
 * 用的是**真实渲染**（把 Review.vue 的模板编译后 SSR），不是断言源码文本；
 * 链接规则本身来自 src/composables/useStockLinks.js —— 页面与测试共用同一份。
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { compile, createSSRApp } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'

import {
  BACK_TARGETS, backLabel, backLink, stockLink, stockQuery,
} from '../src/composables/useStockLinks.js'

const DAY = '2024-05-06'


// ── 链接规则本身 ────────────────────────────────────────────────────
test('去个股：带日期与来源', () => {
  assert.deepEqual(stockLink('600519', DAY, 'review'),
    { path: '/stock/600519', query: { date: DAY, from: 'review' } })
  // 没有日期/来源时不塞空串进 query（空值会让页面以为「选了某天」）
  assert.deepEqual(stockLink('600519', '', ''), { path: '/stock/600519', query: {} })
})

test('返回：按白名单映射，不接受任意返回地址', () => {
  for (const [from, target] of Object.entries(BACK_TARGETS)) {
    const link = backLink(from, DAY)
    assert.equal(link.path, target.path)
    assert.equal(link.query.date, DAY)
    assert.equal(backLabel(from), target.label)
  }
  // 恶意/未知来源一律回带日期的排行榜，绝不照抄外部地址
  const evil = backLink('https://evil.example/x', DAY)
  assert.equal(evil.path, '/')
  assert.equal(evil.query.date, DAY)
  assert.equal(backLabel('https://evil.example/x'), '排行榜')
  // 来源只有白名单成员才写进 query
  assert.deepEqual(stockQuery(DAY, 'https://evil.example'), { date: DAY })
})


// ── 真实渲染：复盘页的信号名单链接 ──────────────────────────────────
const reviewSource = fs.readFileSync(
  new URL('../src/views/Review.vue', import.meta.url), 'utf8')
const reviewRender = compile(parse(reviewSource).descriptor.template.content)

/** router-link 桩：把 `to` 序列化成 data 属性，才好断言真实渲染结果。 */
const RouterLinkStub = {
  props: ['to'],
  template: '<a :data-to="JSON.stringify(to)"><slot /></a>',
}

async function renderReview(state) {
  const app = createSSRApp({ render: reviewRender, setup: () => state })
  app.component('router-link', RouterLinkStub)
  app.component('ReviewNote', { props: ['date', 'symbol'], template: '<div />' })
  app.component('VChart', { template: '<div />' })
  app.config.warnHandler = () => {}      // 未注册的下拉/图标不必刷屏
  return renderToString(app)
}

function reviewState(over = {}) {
  const data = {
    date: DAY,
    period: 60,
    threshold: 80,
    breadth: { traded: 10, up: 6, down: 3, flat: 1, limit_up: 2, limit_down: 0, median_pct: 0.5 },
    limit_up: [{ symbol: '600519', name: '贵州茅台', industry: '白酒', pct: 10.0, limit_ratio: 10, streak: 1 }],
    limit_down: [],
    sectors: [],
    signals: [{ symbol: '600519', name: '贵州茅台', industry: '白酒', rps: 95, period_return: 0.2, pct_chg: 0.03 }],
    signals_note: '口径说明',
    ...(over.data || {}),
  }
  return {
    data,
    errMsg: '',
    date: DAY,
    period: 60,
    threshold: 80,
    availableDates: [DAY],
    periods: [15, 20, 60],
    sectorRows: [],
    num: (v) => String(v ?? '—'),
    pct: (v) => `${(Number(v ?? 0) * 100).toFixed(2)}%`,
    stockLink: (s) => stockLink(s, data.date, 'review'),
    ...over,
  }
}

test('复盘页：信号名单与涨跌停清单的链接都带生效日与来源', async () => {
  const html = await renderReview(reviewState())
  const links = [...html.matchAll(/data-to="([^"]+)"/g)].map((m) => m[1])
  assert.ok(links.length >= 2, `应当渲染出多个链接，实际 ${links.length}`)
  for (const raw of links) {
    // 模板里的 `"` 会被转义成 &quot;
    const to = JSON.parse(raw.replaceAll('&quot;', '"'))
    assert.equal(to.path, '/stock/600519', to.path)
    assert.equal(to.query.date, DAY, '日期必须穿透到个股页')
    assert.equal(to.query.from, 'review', '来源要能映射回复盘页')
  }
})

test('复盘页：没有生效日时不带 date（不要塞空串）', async () => {
  const html = await renderReview(reviewState({ data: { date: null } }))
  const links = [...html.matchAll(/data-to="([^"]+)"/g)].map((m) => m[1])
  for (const raw of links) {
    const to = JSON.parse(raw.replaceAll('&quot;', '"'))
    assert.ok(!('date' in to.query), JSON.stringify(to.query))
  }
})


// ── 真实渲染：笔记组件在「没有生效日」时不给编辑 ────────────────────
const noteSource = fs.readFileSync(
  new URL('../src/components/ReviewNote.vue', import.meta.url), 'utf8')
const noteRender = compile(parse(noteSource).descriptor.template.content)

async function renderNote(state) {
  const app = createSSRApp({ render: noteRender, setup: () => state })
  app.config.warnHandler = () => {}
  return renderToString(app)
}

test('笔记：数据未就绪时输入框禁用并给出说明', async () => {
  const html = await renderNote({
    symbol: '', date: '', text: '', effectiveDate: '', savedAt: '',
    saving: false, dirty: false, message: '', fieldId: 'note-market-none',
    save: () => true, placeholder: '',
  })
  assert.match(html, /disabled/, '没有生效日就不能编辑（否则会写成孤儿记录）')
  assert.match(html, /无法锚定笔记日期/)
})

test('笔记：有生效日时可编辑，且显示保存状态', async () => {
  const html = await renderNote({
    symbol: '600519', date: DAY, text: '放量突破', effectiveDate: DAY,
    savedAt: '2024-05-06T15:10:00', saving: false, dirty: true, message: '',
    fieldId: 'note-600519-2024-05-06', save: () => true, placeholder: '',
  })
  assert.doesNotMatch(html, /disabled/)
  assert.match(html, /个股笔记 · 600519/)
  assert.match(html, /有未保存的修改/)
  assert.match(html, /放量突破/)
})
