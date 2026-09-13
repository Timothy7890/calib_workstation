// Build first; all requests are mocked, including camera and robot APIs.
// PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tests/mount_viewport_panels.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const dist = fileURLToPath(new URL('../frontend/dist/', import.meta.url))
const handId = 'qiangnao-revo2-left'
const samples = [
  { index: 0, hand_id: handId, episode: 'episode_0000', pose_id: 'episode_0000',
    point_id: 'palm-red-01', p_camera: [0, 0, 0.3], p_hand: [0, 0, 0.1], link: 'base_link' },
  { index: 1, hand_id: handId, episode: 'episode_0000', pose_id: 'episode_0000',
    point_id: 'palm-red-02', p_camera: [0.01, 0, 0.3], p_hand: null },
]
const ply = 'ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0.3\n0.01 0 0.3\n0 0.01 0.3\n'
// Check that the model-only clear action retains even vertex index zero.
const source = await readFile(new URL('../frontend/three-d-ui/src/App.vue', import.meta.url), 'utf8')
const clearFunction = source.match(/function clearMountDrafts\(\) \{[\s\S]*?\n\}/)[0]
const state = {
  mountDrafts: { value: [
    { point_id: 'paired', vertexIndex: 0, point: [1, 2, 3], p_hand: [0, 0, 0], p_local: [0, 0, 0], link: 'base_link', meshFaceIndex: 1 },
    { point_id: 'cloud', vertexIndex: 2, point: [4, 5, 6] },
    { point_id: 'model', p_hand: [0, 0, 0] },
  ] },
  mountProfileDirty: {}, activeMountSlotId: {}, mountViewport: {}, mountSlots: [{ point_id: 'palm-red-01' }],
  refreshHighlights() {}, refreshHandPointMarkers() {},
}
runInNewContext(`${clearFunction}\nclearMountDrafts()`, state)
assert.deepEqual(JSON.parse(JSON.stringify(state.mountDrafts.value)), [
  { point_id: 'paired', vertexIndex: 0, point: [1, 2, 3] },
  { point_id: 'cloud', vertexIndex: 2, point: [4, 5, 6] },
])
assert.equal(state.mountProfileDirty.value, true)
const deleteModelFunction = source.match(/function clearMountSlotModelPoint\(pointId\) \{[\s\S]*?\n\}/)[0]
state.mountProfileBusy = { value: false }
state.infoMsg = {}
state.mountSlotInfo = point_id => ({ shortLabel: point_id })
state.mountDrafts.value = [
  { point_id: 'paired', vertexIndex: 0, point: [1, 2, 3], p_hand: [0, 0, 0], link: 'base_link' },
  { point_id: 'other', p_hand: [4, 5, 6] },
]
runInNewContext(`${deleteModelFunction}\nclearMountSlotModelPoint('paired')`, state)
assert.deepEqual(JSON.parse(JSON.stringify(state.mountDrafts.value)), [
  { point_id: 'paired', vertexIndex: 0, point: [1, 2, 3] },
  { point_id: 'other', p_hand: [4, 5, 6] },
])
assert.equal(state.activeMountSlotId.value, 'paired')
// Saving a scheme serializes only remaining model points, never the deleted one.
state.mountSlots = [{ point_id: 'paired' }, { point_id: 'other' }]
const serializeFunction = source.match(/function currentModelPoints\(\) \{[\s\S]*?\n\}/)[0]
assert.equal(runInNewContext(`${serializeFunction}\ncurrentModelPoints().map(p => p.point_id).join(',')`, state), 'other')
const browser = await chromium.launch({ headless: true })
try {
  for (const width of [1920, 1280]) {
    const page = await browser.newPage({ viewport: { width, height: 1080 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      assert.equal(url.hostname, 'panels.test')
      assert.equal(route.request().method(), 'GET', 'Switching views must not write data or control hardware')
      const path = url.pathname.replace(/^\/three-d(?=\/api\/)/, '')
      const reply = json => route.fulfill({ json })
      if (path === '/api/status') return reply({ offline: { enabled: true } })
      if (path === '/api/offline/episodes') return reply({ episodes: [{ name: 'episode_0000' }] })
      if (path === '/api/hands') return reply({ hands: [{ hand_id: handId, label: '强脑-Revo2-左', side: 'left' }] })
      if (path === `/api/hands/${handId}/model`) return reply({ hand_id: handId, label: '强脑-Revo2-左', base_link: 'base_link', links: [] })
      if (path === '/api/mount/samples') return reply({ samples })
      if (path === '/api/mount/result') return reply({ result: {
        num_samples: 2, point_count: 1, pose_count: 3,
        point_ids: ['back-green-01'], excluded_point_ids: ['palm-red-02', 'side-yellow-01'],
        residual_mm: { rms: Math.sqrt(10), median: 3, max: 4 },
        residual_by_point_mm: {
          'back-green-01': { count: 2, rms: Math.sqrt(10), max: 4, per_observation: [2, 4] },
          'palm-red-02': { count: 1, rms: 50, max: 50, per_observation: [50] },
        },
        residual_by_pose_mm: { excluded_only: { count: 1, rms: 50, max: 50 } },
        stage1: { points: ['back-green-01', 'palm-red-02', 'side-yellow-01'].map((point_id, i) => ({
          point_id, has_model_point: i !== 2, pose_count: 5, used_pose_count: 3,
          spread_mm: { rms: 2.65 },
          per_pose: i === 0 ? [
            { pose_id: 'episode_0000', deviation_mm: 1, outlier: false },
            { pose_id: 'episode_0001', deviation_mm: 1, outlier: false },
          ] : [{ pose_id: 'excluded_only', deviation_mm: 50, outlier: true }],
        })) },
        stage2: { points: [{ point_id: 'back-green-01', residual_mm: 22.28 }] },
      } })
      if (path.endsWith('point-cloud.ply')) return route.fulfill({ body: ply, headers: {
        'X-Point-Cloud-Id': 'mock-cloud', 'X-Point-Count': '3', 'X-Point-Cloud-Stride': '1',
      } })
      if (path.startsWith('/api/')) return reply({})
      const file = path.startsWith('/assets/') ? path.slice(1) : 'three-d-ui/index.html'
      return route.fulfill({ body: await readFile(dist + file), contentType:
        file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' })
    })
    await page.goto('http://panels.test/three-d-ui/')
    const panel = page.locator('.selection-panel')
    await page.waitForFunction(() => document.querySelector('.mount-slots-card')?.textContent.includes('已选 1/20'))
    const modelText = await panel.innerText()
    assert.ok(modelText.includes('模型点方案') && modelText.includes('3. 模型选点'))
    for (const forbidden of ['缺模型点', '待保存', '安装样本与解算', '当前 episode', '18089', '写入已保存样本']) {
      assert.ok(!modelText.includes(forbidden), `Model view leaked: ${forbidden}`)
    }
    assert.equal(await panel.locator('.mount-slot-clear:not(.model-point-clear)').count(), 0)
    assert.equal(await panel.locator('.model-point-clear').count(), 1)
    assert.equal(await panel.locator('.mount-slot.saved, .mount-slot.paired, .mount-slot.cloud-only').count(), 0)
    await page.getByRole('button', { name: '实体点云', exact: true }).click()
    assert.ok(await panel.locator('.mount-episode-card').isVisible())
    assert.ok(await panel.locator('.mount-samples-card').isVisible())
    assert.ok(await panel.locator('.hand-hold-row').isVisible())
    assert.ok((await panel.innerText()).includes('写入已保存样本（1）'))
    assert.equal(await panel.locator('.mount-profile-card').count(), 0)
    assert.equal(await panel.locator('.mount-slot-clear').count(), 2)
    const table = panel.locator('.stage-table')
    await table.waitFor()
    const checkTable = async () => {
      const rows = await table.locator('.stage-row').evaluateAll(elements => elements.map(row => ({
        height: row.getBoundingClientRect().height,
        cells: [...row.children].map(cell => ({
          text: cell.textContent.trim(), width: cell.clientWidth, contentWidth: cell.scrollWidth,
          height: cell.getBoundingClientRect().height,
        })),
      })))
      for (const row of rows) {
        assert.ok(row.height <= 32, 'Rows must not grow from wrapped labels')
        for (const cell of row.cells) {
          assert.ok(cell.height <= 24, `Wrapped cell: ${cell.text}`)
          assert.ok(cell.contentWidth <= cell.width + 1, `Overlapping cell: ${cell.text}`)
        }
      }
      assert.equal(rows[0].cells[1].text, '贴纸')
      assert.equal(rows[1].cells[1].text, '绿1')
    }
    await checkTable()
    // A panel narrower than the columns must scroll, never stack sticker names.
    await table.evaluate(el => { el.style.width = '240px' })
    await checkTable()
    assert.ok(await table.evaluate(el => el.scrollWidth > el.clientWidth))
    await table.evaluate(el => { el.style.width = '' })
    const ranking = panel.locator('.residual-ranking')
    assert.ok((await ranking.innerText()).includes('episode_0001'))
    assert.ok(!(await ranking.innerText()).includes('excluded_only'))
    assert.ok(!(await ranking.innerText()).includes('50.00'))
    assert.ok((await panel.locator('.worst-points').innerText()).includes('绿1'))
    assert.ok(!(await panel.locator('.worst-points').innerText()).includes('红2'))
    assert.ok((await panel.innerText()).includes('2 个参与姿态'))
    const boxes = table.locator('input[type=checkbox]')
    assert.equal(await boxes.nth(0).isChecked(), true)
    assert.equal(await boxes.nth(1).isChecked(), false, 'Saved exclusions must survive refresh')
    await boxes.nth(1).check()
    assert.ok((await panel.innerText()).includes('勾选已变化'))
    assert.ok(!(await panel.locator('.worst-points').innerText()).includes('红2'), 'Draft changes must not alter saved result scope')
    await boxes.nth(1).uncheck()
    await panel.evaluate(el => { el.scrollTop = el.scrollHeight })
    await page.getByRole('button', { name: '零位手模型', exact: true }).click()
    assert.ok((await panel.innerText()).includes('已选 1/20'), 'Switching views must retain model points')
    assert.equal(await panel.locator('.mount-samples-card').count(), 0)
    assert.equal(await panel.evaluate(el => el.scrollTop), 0, 'New panel must start at the top')
    await panel.getByRole('button', { name: '删除 红1 的模型点', exact: true }).click()
    assert.ok((await panel.innerText()).includes('已选 0/20'))
    assert.ok((await panel.innerText()).includes('未保存修改'))
    assert.equal(await panel.locator('.model-point-clear').count(), 0)
    assert.ok(await page.getByRole('button', { name: '零位手模型', exact: true }).evaluate(el => el.classList.contains('active')))
    await page.getByRole('button', { name: '实体点云', exact: true }).click()
    assert.equal(await panel.locator('.mount-slot-clear').count(), 2, 'Removing a model point must preserve saved cloud points')
    assert.deepEqual(errors, [])
    console.log(`${width}px: model/cloud panels isolated; annotations retained; no write or control calls`)
    await page.close()
  }
} finally {
  await browser.close()
}
