import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import VChart from 'vue-echarts'
// 必须按需引入：整包 `import * as echarts from 'echarts'` 会让 tree-shaking 失效
// （实测主 chunk 1120 KB）。这里只注册实际用到的图表与组件。
import { use, registerTheme } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, HeatmapChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  MarkLineComponent, MarkAreaComponent, VisualMapComponent,
} from 'echarts/components'

import App from './App.vue'
import './style.css'
import { install as installPerf } from './perf'

// heatmap 的着色依赖 VisualMapComponent，必须显式注册；
// Backtest 的空仓区间底色依赖 MarkAreaComponent（漏注册时不会报错，只是不渲染）；
// 回测的「调仓点」依赖 ScatterChart —— 这个漏注册**会抛错**并把整个视图更新打断
// （实测：ECharts 报 "Series scatter is used but not imported"，之后连对比表都不刷新）。
use([CanvasRenderer, LineChart, BarChart, HeatmapChart, ScatterChart,
  GridComponent, TooltipComponent, LegendComponent,
  MarkLineComponent, MarkAreaComponent, VisualMapComponent])

registerTheme('dark', {
  backgroundColor: 'transparent',
  textStyle: { color: '#e6edf3' },
})

const routes = [
  { path: '/', name: 'rankings', component: () => import('./views/Rankings.vue'),
    meta: { title: 'RPS 排行榜' } },
  { path: '/sectors', name: 'sectors', component: () => import('./views/Sectors.vue'),
    meta: { title: '板块强弱' } },
  { path: '/review', name: 'review', component: () => import('./views/Review.vue'),
    meta: { title: '每日复盘' } },
  { path: '/stock/:symbol', name: 'stock',
    component: () => import('./views/StockDetail.vue'), meta: { title: '个股详情' } },
  { path: '/backtest', name: 'backtest', component: () => import('./views/Backtest.vue'),
    meta: { title: '策略回测' } },
  { path: '/watchlist', name: 'watchlist', component: () => import('./views/Watchlist.vue'),
    meta: { title: '自选与预警' } },
  { path: '/factors', name: 'factors', component: () => import('./views/Factors.vue'),
    meta: { title: '因子研究' } },
  { path: '/portfolio', name: 'portfolio',
    component: () => import('./views/Portfolio.vue'), meta: { title: '模拟持仓' } },
  { path: '/tools', name: 'tools', component: () => import('./views/Tools.vue'),
    meta: { title: '交易工具' } },
  { path: '/trade', name: 'trade', component: () => import('./views/Trade.vue'),
    meta: { title: '券商接入' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

installPerf()
createApp(App).component('VChart', VChart).use(router).mount('#app')
