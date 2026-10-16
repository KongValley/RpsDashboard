/**
 * 个股页的失败/加载状态渲染（node:test，零第三方依赖：`npm test`）。
 *
 * 守的是真实缺陷：`v-if="errMsg"` 的错误条原来被关在 `<template v-else>`
 * 里面，而 `v-else` 的前提是 `info` 非空 —— 首屏任一请求失败时 `info` 仍是
 * null，错误条根本不渲染，页面永远停在「加载中…」。
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { compile, createSSRApp } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'

const source = fs.readFileSync(
  new URL('../src/views/StockDetail.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const render = compile(descriptor.template.content)

async function renderDetail(state) {
  const app = createSSRApp({ render, setup: () => state })
  app.config.warnHandler = () => {}   // 未注册的 router-link/VChart 不必刷屏
  return renderToString(app)
}

test('首屏失败：info 为空时错误条仍必须渲染', async () => {
  const html = await renderDetail({ info: null, errMsg: '个股信息加载失败：HTTP 404' })
  assert.match(html, /个股信息加载失败/)
  assert.doesNotMatch(html, /加载中…/, '有错误时不该只显示加载中')
  assert.match(html, /加载失败/)
})

test('加载中：没有错误时显示加载提示', async () => {
  const html = await renderDetail({ info: null, errMsg: '' })
  assert.match(html, /加载中…/)
})
