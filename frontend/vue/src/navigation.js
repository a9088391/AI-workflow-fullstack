import { BACKENDS } from './auth.js'
import { articlePage, localPage } from './articles.js'

// 登入只能返回已知本機內容頁，避免回到帳號表單或任意外站。
export function loginReturnTarget(target) {
  return target && localPage(target) && target !== '/vue/login' && target !== '/vue/register' ? target : '/vue/'
}

// 六版都已提供共同語意頁；保留原 Nuxt 對無效返回值先回列表的既有行為。
export function frontendDestination(frontend, path, target) {
  if (!['nuxt', 'react', 'next', 'angular', 'sveltekit'].includes(frontend)) return null
  const page = articlePage(path)
  if (page === 'missing') return null
  const returnTo = loginReturnTarget(target)
  // 其餘四版須先拒絕不合法或帳號表單返回值，不能藉清理後的列表誤判為可切換。
  if (frontend !== 'nuxt' && page === 'login' && target !== null
    && (!localPage(target) || ['login', 'register'].includes(articlePage(target)))) return null
  const destination = path.replace(/^\/vue/, `/${frontend}`)
  const nextReturn = page === 'login' && target && returnTo !== '/vue/'
    ? `?return_to=${encodeURIComponent(returnTo.replace(/^\/vue/, `/${frontend}`))}` : ''
  return `${destination}${nextReturn}`
}

// API 選擇只影響標籤；固定名稱與原顯示文字一致，不從 URL 猜測服務。
export function selectedBackendLabel(backend) {
  const labels = {
    laravel: 'Laravel', symfony: 'Symfony', aspnet: 'ASP.NET Core Controllers',
    django: 'Django', flask: 'Flask', fastapi: 'FastAPI', gin: 'Gin', echo: 'Echo',
    fiber: 'Fiber', express: 'Express', fastify: 'Fastify', nestjs: 'NestJS', rails: 'Ruby on Rails',
  }
  for (const [key, value] of Object.entries(BACKENDS)) if (backend === value) return labels[key]
  return 'Laravel'
}
