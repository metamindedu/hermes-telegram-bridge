// Exercise the real pinned-core disk reconciler, not a fabricated GUI loader.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const source = fileURLToPath(new URL('./desktop/plugin.js', import.meta.url))
const repo = fileURLToPath(new URL('../../', import.meta.url))
const core = process.env.HERMES_CORE_ROOT || path.join(repo, 'hermes-agent')
const native = await import(pathToFileURL(path.join(core, 'apps/desktop/electron/desktop-plugins-root.ts')).href)
const id = 'session-complete-telegram'

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(process.env.HERMES_TEST_TMPDIR || process.env.TMPDIR || os.tmpdir(), 'materialize '))
  t.after(() => fs.rm(root, {recursive:true, force:true}))
  const packageDir = path.join(root, 'fake home', 'plugins', id)
  const appRoot = path.join(root, 'fake home', 'desktop-plugins')
  await fs.mkdir(path.join(packageDir, 'desktop'), {recursive:true})
  await fs.copyFile(source, path.join(packageDir, 'desktop', 'plugin.js'))
  return {root, packageDir, appRoot, target:path.join(appRoot, id)}
}

test('native fresh materialization retains the existing Desktop ID and exact candidate bytes', async t => {
  const f = await fixture(t)
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), f.target)
  assert.deepEqual(await fs.readFile(path.join(f.target, 'plugin.js')), await fs.readFile(source))
  const materializedText = await fs.readFile(path.join(f.target, 'plugin.js'), 'utf8')
  assert.match(materializedText, /APPROVAL_CAPABILITY = 'desktop-command-approval-v6'/u)
  assert.match(materializedText, /VERSION = '1.8.1'/u)
  const marker = JSON.parse(await fs.readFile(path.join(f.target, '.hermes-package.json'), 'utf8'))
  assert.equal(marker.package, id)
  assert.equal(marker.source, path.join(f.packageDir, 'desktop'))
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), null)
})

test('native reconciler refuses a different legacy entry, then adopts explicitly aligned bytes without deleting unrelated files', async t => {
  const f = await fixture(t)
  await fs.mkdir(f.target, {recursive:true})
  const legacy = Buffer.from('// synthetic legacy manual Desktop entry\n')
  await fs.writeFile(path.join(f.target, 'plugin.js'), legacy)
  await fs.writeFile(path.join(f.target, 'operator-note.txt'), 'retain unrelated legacy data')
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), null)
  assert.deepEqual(await fs.readFile(path.join(f.target, 'plugin.js')), legacy)
  await assert.rejects(fs.readFile(path.join(f.target, '.hermes-package.json')), {code:'ENOENT'})
  // This is a task-owned fixture only. Production entry alignment requires
  // separate approval, byte readback and an enabled native CLI package first.
  await fs.copyFile(source, path.join(f.target, 'plugin.js'))
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), f.target)
  assert.equal(await fs.readFile(path.join(f.target, 'operator-note.txt'), 'utf8'), 'retain unrelated legacy data')
  assert.equal(JSON.parse(await fs.readFile(path.join(f.target, '.hermes-package.json'), 'utf8')).package, id)
})

test('legacy alignment adoption requires prior preservation before packaged update and rollback', async t => {
  const f = await fixture(t)
  const sourceEntry = path.join(f.packageDir, 'desktop', 'plugin.js')
  const v1 = Buffer.from('// synthetic packaged candidate v1\n')
  const v2 = Buffer.from('// synthetic packaged candidate v2\n')
  const time1 = 1_900_000_000_000
  const time2 = time1 + 2_000
  const time3 = time2 + 2_000
  await fs.writeFile(sourceEntry, v1)
  await fs.utimes(sourceEntry, new Date(time1), new Date(time1))
  await fs.mkdir(f.target, {recursive:true})
  await fs.writeFile(path.join(f.target, 'plugin.js'), '// unrelated synthetic legacy entry\n')
  const operatorNote = path.join(f.target, 'operator-note.txt')
  await fs.writeFile(operatorNote, 'synthetic note preserved only by explicit task-owned relocation')

  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), null)
  assert.equal(await fs.readFile(operatorNote, 'utf8'), 'synthetic note preserved only by explicit task-owned relocation')
  await assert.rejects(fs.readFile(path.join(f.target, '.hermes-package.json')), {code:'ENOENT'})

  // Model a separate, approved preservation step in this disposable fixture;
  // entry alignment/adoption itself does not authorize moving unrelated files.
  const preservationRoot = path.join(f.root, 'approved task-owned preservation')
  await fs.mkdir(preservationRoot, {recursive:true})
  const preservedNote = path.join(preservationRoot, 'operator-note.txt')
  await fs.rename(operatorNote, preservedNote)
  await fs.copyFile(sourceEntry, path.join(f.target, 'plugin.js'))
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), f.target)
  assert.deepEqual(await fs.readFile(path.join(f.target, 'plugin.js')), v1)
  assert.equal(JSON.parse(await fs.readFile(path.join(f.target, '.hermes-package.json'), 'utf8')).package, id)
  assert.equal(await fs.readFile(preservedNote, 'utf8'), 'synthetic note preserved only by explicit task-owned relocation')

  const updateLeftover = path.join(f.target, 'unrelated-inside-managed-tree.txt')
  await fs.writeFile(updateLeftover, 'synthetic managed-tree leftover')
  await fs.writeFile(sourceEntry, v2)
  await fs.utimes(sourceEntry, new Date(time2), new Date(time2))
  assert.ok((await fs.stat(sourceEntry)).mtimeMs > time1)
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), f.target)
  assert.deepEqual(await fs.readFile(path.join(f.target, 'plugin.js')), v2)
  await assert.rejects(fs.readFile(updateLeftover), {code:'ENOENT'})
  assert.equal(await fs.readFile(preservedNote, 'utf8'), 'synthetic note preserved only by explicit task-owned relocation')

  const rollbackLeftover = path.join(f.target, 'unrelated-before-packaged-rollback.txt')
  await fs.writeFile(rollbackLeftover, 'synthetic managed-tree leftover before rollback')
  await fs.writeFile(sourceEntry, v1)
  await fs.utimes(sourceEntry, new Date(time3), new Date(time3))
  assert.ok((await fs.stat(sourceEntry)).mtimeMs > time2)
  assert.equal(await native.materializeDesktopHalf(f.packageDir, f.appRoot, id), f.target)
  assert.deepEqual(await fs.readFile(path.join(f.target, 'plugin.js')), v1)
  await assert.rejects(fs.readFile(rollbackLeftover), {code:'ENOENT'})
  assert.equal(await fs.readFile(preservedNote, 'utf8'), 'synthetic note preserved only by explicit task-owned relocation')
})
