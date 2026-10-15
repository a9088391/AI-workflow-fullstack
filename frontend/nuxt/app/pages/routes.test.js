import assert from 'node:assert/strict'
import { readdirSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { buildTree, toVueRouter4 } from 'unrouting'

// 使用 Nuxt 同一套檔案路由產生器，防止詳情頁意外成為編修頁的無出口父頁。
test('article and comment edit pages resolve beside their detail pages', () => {
  const root = fileURLToPath(new URL('.', import.meta.url))
  const files = []
  for (const relative of readdirSync(root, { recursive: true })) {
    if (relative.endsWith('.vue')) files.push({ path: resolve(root, relative), priority: 0 })
  }
  // 路由警告代表檔案配置不再唯一，應讓測試直接失敗。
  const routes = toVueRouter4(buildTree(files, { roots: [root], modes: ['client'], warn(message) { throw new Error(message) } }), { attrs: { mode: ['client'] } })
  for (const resource of ['articles', 'comments']) {
    let detail
    let editor
    for (const route of routes) {
      if (route.file === resolve(root, resource, '[id]', 'index.vue')) detail = route
      if (route.file === resolve(root, resource, '[id]', 'edit.vue')) editor = route
    }
    assert.equal(detail?.path, `/${resource}/:id()`)
    assert.equal(editor?.path, `/${resource}/:id()/edit`)
    assert.deepEqual(detail.children, [])
    assert.deepEqual(editor.children, [])
  }
})

// 管理員五個語意入口皆須由 Nuxt 實際檔案路由產生，深連結不落入 catch-all。
test('all administration routes resolve to their own Nuxt pages', () => {
  const root = fileURLToPath(new URL('.', import.meta.url))
  const files = []
  for (const relative of readdirSync(root, { recursive: true })) {
    if (relative.endsWith('.vue')) files.push({ path: resolve(root, relative), priority: 0 })
  }
  const routes = toVueRouter4(buildTree(files, { roots: [root], modes: ['client'],
    // 重複或模糊頁面配置會讓深連結結果不可靠，直接視為失敗。
    warn(message) { throw new Error(message) },
  }), { attrs: { mode: ['client'] } })
  const actual = routes.filter(
    // 只選管理語意路由，保留與公開頁分開的精確對照。
    (route) => route.path.startsWith('/admin/'),
  ).map(
    // 檔案路由的 path 是瀏覽器深連結會解析的實際值。
    (route) => route.path,
  ).sort()
  assert.deepEqual(actual, ['/admin/articles', '/admin/comments', '/admin/members', '/admin/members/:id()', '/admin/settings/login-lockout'])
})
