import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { MotionPlugin } from '@vueuse/motion'
import ProjectApp from './ProjectApp.vue'
import { router } from './router'
import { applyDarkTheme } from './theme'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import './styles.css'

applyDarkTheme()
createApp(ProjectApp).use(createPinia()).use(router).use(MotionPlugin).mount('#app')
