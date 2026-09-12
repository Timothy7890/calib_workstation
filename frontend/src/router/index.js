import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import WizardView from '../views/WizardView.vue'
import HistoryView from '../views/HistoryView.vue'
import PlansView from '../views/PlansView.vue'
import GuideView from '../views/GuideView.vue'
import HandCalibrationView from '../views/HandCalibrationView.vue'

const routes = [
  { path: '/', name: 'home', component: HomeView },
  { path: '/calibrate', name: 'calibrate', component: WizardView, meta: { title: '开始标定' } },
  { path: '/hand-calibration', name: 'hand-calibration', component: HandCalibrationView, meta: { title: '3D 手/TCP 标定' } },
  { path: '/history', name: 'history', component: HistoryView, meta: { title: '标定记录' } },
  { path: '/plans', name: 'plans', component: PlansView, meta: { title: '采集计划（高级）' } },
  { path: '/guide', name: 'guide', component: GuideView, meta: { title: '使用说明' } },
]

const router = createRouter({ history: createWebHistory(), routes })
export default router
