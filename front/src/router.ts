import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    { path: '/projects', component: () => import('./views/ProjectHome.vue') },
    { path: '/projects/:projectId', component: () => import('./views/ProjectWorkspace.vue'), props: true },
    { path: '/:pathMatch(.*)*', redirect: '/projects' },
  ],
})
