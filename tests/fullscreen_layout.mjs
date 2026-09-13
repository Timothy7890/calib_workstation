// Build frontend first. Run with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tests/fullscreen_layout.mjs.
// All requests are intercepted: no robot/service calls are made.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const dist = fileURLToPath(new URL('../frontend/dist/', import.meta.url))
const browser = await chromium.launch({ headless: true })
try {
  for (const [width, height] of [[5114, 2360], [1920, 1080], [1280, 720], [800, 600]]) {
    const page = await browser.newPage({ viewport: { width, height } })
    let restoreCalls = 0
    let objectCalls = 0
    const alreadyConfirmed = width !== 800
    const job = { calibration_kind: '3d', step: 'annotating', arm: 'left', camera_role: 'head',
      run_dir: '/mock/capture', object_mode: alreadyConfirmed ? 'hand' : null, model_id: 'qiangnao-revo2-left' }
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      assert.equal(url.hostname, 'layout.test', 'No external services may be contacted')
      const path = url.pathname
      const reply = data => route.fulfill({ json: data })
      if (path === '/api/config') return reply({ robot: { unit_code: 'H2-TEST' }, cameras: {}, board: {}, services_public: {} })
      if (path === '/api/health') return reply({ ok: true, services: {} })
      if (path === '/api/hand-calibration') return reply({ job, ui_url: '/mock-viewer/', service: { ok: true },
        replay: { state: 'completed', arm: { arm: 'left', engaged: true } }, active: { arm: 'left_arm' },
        episodes: [], tasks: [], annotation: {} })
      if (path === '/api/hand-calibration/object') { objectCalls += 1; job.object_mode = 'hand'; return reply({ ok: true, job }) }
      if (path === '/api/hand-calibration/annotation-session') { restoreCalls += 1; return reply({ ok: true, job }) }
      if (path === '/three-d/api/hands') return reply({ hands: [{ hand_id: job.model_id, label: '强脑-Revo2-左', side: 'left' }] })
      if (path.startsWith('/api/calibration/')) throw new Error(`Unexpected control request: ${path}`)
      if (path.startsWith('/api/')) return reply({ plans: [], devices: [], roles: {} })
      if (path === '/mock-viewer/') return route.fulfill({ contentType: 'text/html', body: '<body style="margin:0;background:#0b121d;color:white">隔离测试选点操作台</body>' })
      const file = path.startsWith('/assets/') ? path.slice(1) : 'index.html'
      const contentType = file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'application/javascript' : file.endsWith('.png') ? 'image/png' : 'text/html'
      return route.fulfill({ contentType, body: await readFile(dist + file) })
    })
    await page.goto('http://layout.test/hand-calibration')
    await page.getByRole('button', { name: '确认对象', exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '全屏选点', exact: true }).count(), 0)
    assert.equal(await page.locator('.annotation-card iframe').count(), 0, 'No inline viewer before confirmation')
    assert.equal(await page.locator('.annotation-actions').count(), 0, 'No inline viewer footer')
    assert.equal(restoreCalls, 0, 'Do not restore an annotation session before confirmation')
    if (!alreadyConfirmed) await page.getByLabel('几何模型').selectOption(job.model_id)
    await page.getByRole('button', { name: '确认对象', exact: true }).click()
    await page.locator('.annotation-fullscreen').waitFor()
    const frame = await page.locator('.annotation-card iframe').boundingBox()
    const card = await page.locator('.annotation-card').boundingBox()
    const controls = await page.locator('.arm-recovery').boundingBox()
    const actions = await page.locator('.annotation-actions').boundingBox()
    assert.ok(frame.height > height * 0.55, `Collapsed frame at ${width}x${height}: ${frame.height}`)
    assert.ok(Math.abs(card.y + card.height - (height - 8)) <= 3, 'Card must extend to viewport bottom')
    assert.ok(frame.y >= controls.y + controls.height, 'Control bar must remain outside iframe')
    assert.ok(actions.y >= frame.y + frame.height - 2 && actions.y + actions.height <= height, 'Footer must remain visible')
    for (const name of ['立即停止', '卸力拖动', '保持', '结束接管', '退出全屏']) {
      assert.ok(await page.getByRole('button', { name, exact: true }).isVisible(), name)
    }
    // The single confirmation entry reopens the existing viewer without losing unsaved state.
    const viewer = await (await page.locator('.annotation-card iframe').elementHandle()).contentFrame()
    await viewer.evaluate(() => { window.unsavedSelection = 'retained' })
    await page.getByRole('button', { name: '退出全屏', exact: true }).click()
    assert.equal(await page.locator('.annotation-fullscreen').count(), 0)
    assert.equal(await page.locator('.annotation-card iframe').isVisible(), false, 'No small viewer after exiting fullscreen')
    assert.equal(await page.locator('.annotation-actions').count(), 0)
    await page.getByRole('button', { name: '确认对象', exact: true }).click()
    await page.locator('.annotation-fullscreen').waitFor()
    assert.ok((await page.locator('.annotation-card iframe').boundingBox()).height > height * 0.55)
    assert.equal(await viewer.evaluate(() => window.unsavedSelection), 'retained')
    assert.equal(restoreCalls, alreadyConfirmed ? 2 : 1)
    assert.equal(objectCalls, alreadyConfirmed ? 0 : 1, 'Reconfirming the same object must not reset the calibration job')
    console.log(`${width}x${height}: iframe ${Math.round(frame.width)}x${Math.round(frame.height)}, footer visible`)
    if (process.env.LAYOUT_SCREENSHOT && width === 1920) await page.screenshot({ path: process.env.LAYOUT_SCREENSHOT })
    await page.close()
  }
} finally {
  await browser.close()
}
