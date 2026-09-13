// Read only the saved solve population, never the current checkbox draft.
const summarize = values => values.length ? {
  count: values.length,
  rms: Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length),
  max: Math.max(...values),
} : null

export function participatingResultStats(result) {
  if (!result) return { points: {}, poses: {}, poseCount: null, stage1: null, complete: false }
  const excluded = new Set(result.excluded_point_ids || [])
  const used = new Set((result.point_ids || result.stage2?.points?.map(p => p.point_id) || [])
    .filter(id => !excluded.has(id)))
  const points = Object.fromEntries(Object.entries(result.residual_by_point_mm || {})
    .filter(([id]) => used.has(id)))
  const stage1Points = (result.stage1?.points || []).filter(p => used.has(p.point_id))
  const stage1Errors = stage1Points.filter(p => p.pose_count >= 2)
    .flatMap(p => (p.per_pose || []).filter(o => !o.outlier).map(o => o.deviation_mm))
    .filter(Number.isFinite)
  const stage1 = result.stage1_used_stats_mm || summarize(stage1Errors)
  if (result.residual_scope === 'participating_points') {
    const poses = result.residual_by_pose_mm || {}
    return { points, poses, stage1, poseCount: Object.keys(poses).length, complete: true }
  }
  // Older results mixed excluded points into pose rankings. Reconstruct from
  // each point's saved observation errors and matching stage1 pose order.
  const errorsByPose = new Map()
  let count = 0
  for (const id of used) {
    const errors = points[id]?.per_observation
    const observations = stage1Points.find(p => p.point_id === id)?.per_pose
    if (!errors?.length || errors.length !== observations?.length) {
      return { points, poses: {}, stage1, poseCount: null, complete: false }
    }
    for (let i = 0; i < errors.length; i += 1) {
      const pose = observations[i].pose_id
      if (!pose || !Number.isFinite(errors[i])) {
        return { points, poses: {}, stage1, poseCount: null, complete: false }
      }
      if (!errorsByPose.has(pose)) errorsByPose.set(pose, [])
      errorsByPose.get(pose).push(errors[i])
      count += 1
    }
  }
  const complete = used.size > 0 && count === result.num_samples
  const poses = complete ? Object.fromEntries([...errorsByPose].map(([id, values]) => [id, summarize(values)])) : {}
  return { points, poses, stage1, poseCount: complete ? errorsByPose.size : null, complete }
}
