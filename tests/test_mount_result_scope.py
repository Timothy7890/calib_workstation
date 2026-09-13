import numpy as np

from calib_workstation.calib3d.solver import solve_hand_mount_two_stage, solve_rigid_transform


def test_excluded_and_unmodeled_points_do_not_enter_result_rankings():
    model = {
        "a": np.array([0., 0., 0.]),
        "b": np.array([.1, 0., 0.]),
        "c": np.array([0., .1, 0.]),
        "d": np.array([0., 0., .1]),
        "excluded": np.array([.2, .2, .2]),
    }
    points, ids, poses = [], [], []
    for pose in range(3):
        for name, point in model.items():
            points.append(point + [.03, -.02, .01] + np.array([pose * .0001, 0., 0.])
                          + (np.array([.5, .4, .3]) if name == "excluded" else 0))
            ids.append(name)
            poses.append(f"pose{pose}")
    points.extend([[1., 1., 1.], [2., 2., 2.]])
    ids.extend(["excluded", "unmodeled"])
    poses.extend(["excluded_only", "unmodeled_only"])
    result = solve_hand_mount_two_stage(model, np.array(points), ids, poses, ["excluded"])
    assert result["residual_scope"] == "participating_points"
    assert result["num_samples"] == result["residual_mm"]["count"] == 12
    assert result["observation_count"] == 17
    assert result["pose_count"] == 3
    assert result["input_pose_count"] == 5
    assert result["pose_ids"] == ["pose0", "pose1", "pose2"]
    assert set(result["residual_by_point_mm"]) == {"a", "b", "c", "d"}
    for field in ["residual_by_point_mm", "residual_by_pose_mm"]:
        stats = result[field].values()
        assert sum(s["count"] for s in stats) == 12
        assert np.isclose(max(s["max"] for s in stats), result["residual_mm"]["max"])
        weighted_rms = np.sqrt(sum(s["count"] * s["rms"] ** 2 for s in stats) / 12)
        assert np.isclose(weighted_rms, result["residual_mm"]["rms"])
    assert "excluded" in result["diagnostic_residual_by_point_mm"]
    assert "excluded_only" in result["diagnostic_residual_by_pose_mm"]
    assert result["stage1"]["observation_count"] == 17
    assert result["stage1_used_stats_mm"]["count"] == 12
    # Scope changes must not change the fitted transform/point means.
    kept = [p for p in result["stage1"]["points"] if p["point_id"] in {"a", "b", "c", "d"}]
    expected = solve_rigid_transform(
        np.array([model[p["point_id"]] for p in kept]),
        np.array([p["mean_wrist_m"] for p in kept]),
    )
    np.testing.assert_allclose(result["R_wrist2hand"], expected["R_cam2base"])
    np.testing.assert_allclose(result["t_wrist2hand_m"], expected["t_cam2base_m"])


def test_no_exclusions_keeps_all_modeled_observations():
    model = {"a": [0., 0., 0.], "b": [.1, 0., 0.], "c": [0., .1, 0.]}
    result = solve_hand_mount_two_stage(model, np.array(list(model.values()) * 2),
                                       list(model) * 2, ["p0"] * 3 + ["p1"] * 3)
    assert result["num_samples"] == 6
    assert result["pose_count"] == 2
    assert sum(s["count"] for s in result["residual_by_pose_mm"].values()) == 6
    assert result["stage1_used_stats_mm"] == result["stage1"]["stats_mm"]
