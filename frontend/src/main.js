import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import VChart from 'vue-echarts'
import * as echarts from 'echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, HeatmapChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  TitleComponent, MarkLineComponent, VisualMapComponent,
} from 'echarts/components'

import App from './App.vue'
import './style.css'

// heatmap 的着色依赖 VisualMapComponent，必须显式注册
use([CanvasRenderer, LineChart, BarChart, HeatmapChart, ScatterChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  TitleComponent, MarkLineComponent, VisualMapComponent])

echarts.registerTheme('dark', {
  backgroundColor: 'transparent',
  textStyle: { color: '#e6edf3' },
})

const routes = [
  { path: '/', name: 'rankings', component: () => import('./views/Rankings.vue'),
    meta: { title: 'RPS 排行榜' } },
  { path: '/sectors', name: 'sectors', component: () => import('./views/Sectors.vue'),
    meta: { title: '板块强弱' } },
  { path: '/stock/:symbol', name: 'stock',
    component: () => import('./views/StockDetail.vue'), meta: { title: '个股详情' } },
  { path: '/backtest', name: 'backtest', component: () => import('./views/Backtest.vue'),
    meta: { title: '策略回测' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

createApp(App).component('VChart', VChart).use(router).mount('#app')
