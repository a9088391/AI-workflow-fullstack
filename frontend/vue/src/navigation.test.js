import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { readFileSync } from 'node:fs'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { BACKENDS } from './auth.js'
import { frontendDestination, loginReturnTarget, selectedBackendLabel } from './navigation.js'
import { executableModule } from './flow-harness.js'

// 精確保留六版既有路徑及 Nuxt 的安全降回列表差異，不開放外站返回。
test('navigation preserves IDs and existing frontend return target differences', () => {
  for (const frontend of ['nuxt', 'react', 'next', 'angular', 'sveltekit']) {
    assert.equal(frontendDestination(frontend, '/vue/articles/007/edit', null), `/${frontend}/articles/007/edit`)
    assert.equal(frontendDestination(frontend, '/vue/login', '/vue/admin/members/007'), `/${frontend}/login?return_to=${encodeURIComponent(`/${frontend}/admin/members/007`)}`)
    assert.equal(frontendDestination(frontend, '/vue/articles/0', null), null)
    for (const target of ['https://evil.test', '//evil.test', '/vue/login', '/vue/register', '/vue/articles/0', '']) {
      assert.equal(loginReturnTarget(target), '/vue/')
      assert.equal(frontendDestination(frontend, '/vue/login', target), frontend === 'nuxt' ? '/nuxt/login' : null)
    }
  }
  assert.equal(frontendDestination('invalid', '/vue/', null), null)
  assert.equal(selectedBackendLabel(BACKENDS.aspnet), 'ASP.NET Core Controllers')
  assert.equal(selectedBackendLabel(BACKENDS.nestjs), 'NestJS')
  assert.equal(selectedBackendLabel(BACKENDS.rails), 'Ruby on Rails')
})

// import 節點可跨行且含 alias；普通字串中的 import 文字不能被來源切割誤刪。
test('flow harness accepts multiline imports without deleting executable strings', () => {
  const source = `import {
    ref,
    computed as derive,
  } from 'vue'
  export function answer() { return 'import preserved' }
  const total = 42
  `
  const executable = executableModule(source)
  assert.deepEqual(runInNewContext(`${executable}; [answer(), total]`).join('|'), 'import preserved|42')
})

// 兩份實際 SFC 都經正式 script／template compiler，不能以移除 import 的 harness 代替編譯證據。
test('App and AccountForm compile their script setup and templates', () => {
  for (const filename of ['App.vue', 'AccountForm.vue']) {
    const source = readFileSync(new URL(`./${filename}`, import.meta.url), 'utf8')
    const { descriptor, errors } = parse(source)
    assert.deepEqual(errors, [])
    const script = compileScript(descriptor, { id: filename })
    const result = compileTemplate({
      id: filename, source: descriptor.template.content, filename,
      compilerOptions: { bindingMetadata: script.bindings },
    })
    assert.deepEqual(result.errors, [])
    assert.ok(result.code.includes('render'))
  }
})
