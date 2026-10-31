import { readFileSync } from 'node:fs'
import { babelParse, MagicString, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import { renderToString } from '@vue/server-renderer'

// 以 Vue 編譯器的模組節點範圍移除 import，避免文字行清理破壞多行語法或誤改字串。
export function removeImportDeclarations(source) {
  const ast = babelParse(source, { sourceType: 'module' })
  const output = new MagicString(source)

  // 只刪除 AST 確認的匯入節點，保留註解、字串與其他程式碼原貌。
  for (const node of ast.program.body) {
    if (node.type === 'ImportDeclaration') output.remove(node.start, node.end)
  }

  return output.toString()
}

// 讀取 SFC setup 區塊，讓現有 VM harness 使用同一套 AST 匯入清理。
export function loadScriptSetup(fileUrl) {
  const { descriptor, errors } = parse(readFileSync(fileUrl, 'utf8'), { filename: fileUrl.pathname })
  // 編譯錯誤立即保留原診斷，不讓不完整 setup 在 VM 裡產生誤導的後續錯誤。
  if (errors.length) throw errors[0]
  if (!descriptor.scriptSetup) throw new Error(`缺少 script setup：${fileUrl.pathname}`)
  return removeImportDeclarations(descriptor.scriptSetup.content)
}

// 使用 Vue SSR 編譯實際 SFC template，直接檢查使用者可見的 HTML 結構。
export async function renderSfcTemplate(fileUrl, bindings) {
  const { descriptor, errors } = parse(readFileSync(fileUrl, 'utf8'), { filename: fileUrl.pathname })
  if (errors.length) throw errors[0]
  if (!descriptor.template) throw new Error(`缺少 template：${fileUrl.pathname}`)

  const component = {
    // 將被測 setup 狀態提供給真實 template，SSR 不執行瀏覽器生命週期。
    setup() { return bindings },
    render: vue.compile(descriptor.template.content),
  }
  const app = vue.createSSRApp(component)

  // 根 layout 的頁面出口只影響內容區，選單測試以空元件維持真實外殼。
  app.component('NuxtPage', {
    // NuxtPage 由測試替身留空，避免引入 Router 或瀏覽器 DOM。
    render() { return null },
  })

  return renderToString(app)
}
