import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { _electron as electron } from 'playwright'

const required = name => {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required`)
  return value
}

const poll = async (fn, predicate, timeoutMs = 90_000) => {
  const deadline = Date.now() + timeoutMs
  let value
  while (Date.now() < deadline) {
    try {
      value = await fn()
      if (predicate(value)) return value
    } catch {
      // Retry while updater/relaunch writes atomically.
    }
    await new Promise(resolve => setTimeout(resolve, 200))
  }
  throw new Error(`timed out waiting for condition; last value: ${JSON.stringify(value)}`)
}

const hermesHome = required('HERMES_HOME')
const releases = required('HERMES_DESKTOP_E2E_RELEASES')
const v1Executable = path.join(hermesHome, 'versions', '1.0.0', 'desktop', 'Hermes')
const versionFile = path.join(hermesHome, 'desktop-e2e-version.json')
const marker = path.join(hermesHome, '.hermes-update-in-progress')
const userData = path.join(hermesHome, 'desktop-user-data')

const app = await electron.launch({
  executablePath: v1Executable,
  args: ['--disable-gpu', '--no-sandbox'],
  env: {
    ...process.env,
    HERMES_HOME: hermesHome,
    HERMES_DESKTOP_APP_NAME: `HermesUpdaterE2E-${process.pid}`,
    HERMES_DESKTOP_IGNORE_EXISTING: '1',
    HERMES_DESKTOP_BOOT_FAKE: '1',
    HERMES_DESKTOP_BOOT_FAKE_STEP_MS: '10',
    HERMES_DESKTOP_E2E_VERSION_FILE: versionFile,
    HERMES_DESKTOP_UPDATE_SOURCE: `file://${releases}`,
    HERMES_DESKTOP_USER_DATA_DIR: userData
  }
})
app.process().stdout?.on('data', chunk => process.stderr.write(`[desktop stdout] ${chunk}`))
app.process().stderr?.on('data', chunk => process.stderr.write(`[desktop stderr] ${chunk}`))

try {
  const page = await app.firstWindow({ timeout: 60_000 })
  const initial = await page.evaluate(() => window.hermesDesktop.getVersion())
  assert.equal(initial.appVersion, '1.0.0')
  assert.equal(initial.installType, 'slot')

  const status = await page.evaluate(() => window.hermesDesktop.updates.check())
  assert.equal(status.supported, true)
  assert.ok(status.behind > 0)

  const apply = await page.evaluate(() => window.hermesDesktop.updates.apply())
  assert.deepEqual({ ok: apply.ok, handedOff: apply.handedOff }, { ok: true, handedOff: true })
  // Playwright owns v1's debugger connection, so production's app.quit()
  // cannot complete until the harness detaches. End v1 immediately after the
  // successful handoff so updater-owned v2 can acquire the single-instance
  // lock and relaunch normally.
  const v1 = app.process()
  const v1Exited = new Promise(resolve => v1.once('exit', resolve))
  v1.kill('SIGKILL')
  await v1Exited

  await poll(
    () => fs.readFileSync(path.join(hermesHome, 'current.txt'), 'utf8').trim(),
    value => value === '2.0.0'
  )
  assert.equal(fs.readFileSync(path.join(hermesHome, 'previous.txt'), 'utf8').trim(), '1.0.0')

  const relaunched = await poll(
    () => JSON.parse(fs.readFileSync(versionFile, 'utf8')),
    value => value.appVersion === '2.0.0'
  )
  assert.ok(relaunched.execPath.includes(path.join('versions', '2.0.0', 'desktop', 'Hermes')))
  await poll(() => fs.existsSync(marker), exists => !exists, 30_000)
  console.log('PLAYWRIGHT_PASS: packaged v1 applied signed update and relaunched v2')
} finally {
  // Closing this harness process releases Playwright's inspector pipe. The
  // updater-owned v2 process is separate and is cleaned by the shell harness.
  app.process().kill('SIGKILL')
}

process.exit(0)
