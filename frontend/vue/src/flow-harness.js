import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { babelParse, compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as navigation from './navigation.js'

// 依語法節點移除 import／export 外框，多行 import、註解或字串不會誤刪實際程式。
export function executableModule(source) {
  const ast = babelParse(source, { sourceType: 'module' })
  let executable = source
  for (const node of [...ast.program.body].reverse()) {
    if (node.type === 'ImportDeclaration') executable = executable.slice(0, node.start) + executable.slice(node.end)
    else if (node.type === 'ExportNamedDeclaration' && node.declaration) {
      executable = executable.slice(0, node.start) + executable.slice(node.declaration.start)
    }
  }
  return executable
}

// 使用已安裝的 SFC compiler 編譯真正欄位元件，SSR 斷言也會經過正式元件模板。
function accountForm() {
  const source = readFileSync(new URL('./AccountForm.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const compiled = compileScript(descriptor, { id: 'account-form-test', inlineTemplate: true, genDefaultAs: 'component' })
  const ast = babelParse(compiled.content, { sourceType: 'module' })
  const imports = []
  // 此元件只使用 Vue 原生 helper；測試拒絕未知依賴，避免悄悄漏掉真實匯入。
  for (const node of ast.program.body) {
    if (node.type !== 'ImportDeclaration') continue
    if (node.source.value !== 'vue') throw new Error(`Unsupported component import: ${node.source.value}`)
    for (const specifier of node.specifiers) imports.push(`const ${specifier.local.name} = Vue.${specifier.imported.name}`)
  }
  return runInNewContext(`${imports.join('\n')}\n${executableModule(compiled.content)}\n;component`, { Vue: vue })
}

export const AccountForm = accountForm()
export const navigationBindings = { ...navigation, AccountForm }

// composable 與 App 共用同一個受控 HTTP／Storage／時鐘環境；這是輔助 harness，並非瀏覽器。
export function appScript(descriptor) {
  const session = readFileSync(new URL('./useAccountSession.js', import.meta.url), 'utf8')
  return `${executableModule(session)}\n${executableModule(descriptor.scriptSetup.content)}`
}

// 由編譯器辨識解構與多行宣告，不再依頂層名稱的文字排列猜測可觀察狀態。
export function appBindings(descriptor) {
  return Object.keys(compileScript(descriptor, { id: 'app-flow-test' }).bindings)
}
