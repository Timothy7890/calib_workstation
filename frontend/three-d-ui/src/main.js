import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

// 3D界面与主站同源；根API请求统一收口到18005内置3D子应用。
const nativeFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = typeof input === 'string' && input.startsWith('/api/')
    ? `/three-d${input}`
    : input
  return nativeFetch(url, init)
}

createApp(App).mount('#app')
