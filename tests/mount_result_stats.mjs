import assert from 'node:assert/strict'
import { participatingResultStats } from '../frontend/three-d-ui/src/mountResultStats.js'

const old = {
  point_ids: ['green'], excluded_point_ids: ['pink'], num_samples: 2, pose_count: 3,
  residual_by_point_mm: {
    green: { count: 2, rms: Math.sqrt(10), max: 4, per_observation: [2, 4] },
    pink: { count: 2, rms: 50, max: 50, per_observation: [50, 50], excluded: true },
  },
  residual_by_pose_mm: { p0: { max: 50 }, p1: { max: 4 }, excluded_only: { max: 50 } },
  stage1: { points: [
    { point_id: 'green', pose_count: 2, per_pose: [
      { pose_id: 'p0', deviation_mm: 1, outlier: false },
      { pose_id: 'p1', deviation_mm: 3, outlier: true },
    ] },
    { point_id: 'pink', pose_count: 2, per_pose: [
      { pose_id: 'p0', deviation_mm: 20 }, { pose_id: 'excluded_only', deviation_mm: 20 },
    ] },
  ] },
}
const before = JSON.stringify(old)
const stats = participatingResultStats(old)
assert.equal(stats.complete, true)
assert.equal(stats.poseCount, 2)
assert.deepEqual(Object.keys(stats.points), ['green'])
assert.deepEqual(stats.poses, { p0: { count: 1, rms: 2, max: 2 }, p1: { count: 1, rms: 4, max: 4 } })
assert.deepEqual(stats.stage1, { count: 1, rms: 1, max: 1 })
assert.equal(JSON.stringify(old), before, 'Legacy results must not be mutated')
const modern = { ...old, residual_scope: 'participating_points', residual_by_pose_mm: stats.poses,
  stage1_used_stats_mm: stats.stage1 }
assert.deepEqual(participatingResultStats(modern), stats)
for (const broken of [
  { ...old, num_samples: 3 },
  { ...old, stage1: {} },
  { ...old, residual_by_point_mm: { green: { per_observation: [2, NaN] } } },
]) {
  const result = participatingResultStats(broken)
  assert.equal(result.complete, false)
  assert.deepEqual(result.poses, {}, 'Do not show unverifiable old mixed-scope rankings')
}
console.log('Participating scope, legacy reconstruction, outlier distinction and read-only behavior passed')
