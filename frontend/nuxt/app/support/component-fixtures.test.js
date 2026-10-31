import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import { babelParse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import { removeImportDeclarations } from './component-fixtures.js'

// 多行與 side-effect import 都依 AST 移除，字串文字和剩餘執行語意維持不變。
test('component script fixture removes import declarations without rewriting source text', () => {
  const source = [
    "import {",
    '  ref,',
    '  computed,',
    "} from 'vue'",
    "import './side-effect.js'",
    "const marker = 'literal import remains'",
    'const count = ref(1)',
    '// computed callback keeps its ref dependency after import removal',
    'const label = computed(() => `${marker}:${count.value}`)',
  ].join('\n')
  const transformed = removeImportDeclarations(source)
  const ast = babelParse(transformed, { sourceType: 'module' })

  // 解析結果確認所有合法 import 節點已移除，而字面文字仍是原內容。
  let hasImport = false
  for (const node of ast.program.body) if (node.type === 'ImportDeclaration') hasImport = true
  assert.equal(hasImport, false)
  assert.match(transformed, /literal import remains/)
  assert.match(transformed, /computed callback keeps its ref dependency/)
  assert.doesNotMatch(transformed, /side-effect\.js/)

  // 注入原本 import 的 Vue API，確認清理後程式仍能保留 ref/computed 行為。
  const result = runInNewContext(`${transformed}; ({ marker, count, label })`, { ref: vue.ref, computed: vue.computed })
  assert.equal(result.marker, 'literal import remains')
  assert.equal(result.count.value, 1)
  assert.equal(result.label.value, 'literal import remains:1')
})
