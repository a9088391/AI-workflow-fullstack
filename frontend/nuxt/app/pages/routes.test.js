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
