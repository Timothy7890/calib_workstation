"""Model-free rigid tool calibration from repeated RGB-D observations (metres)."""
from __future__ import annotations

import numpy as np


def solve_tool(samples: list[dict], mode: str, R: np.ndarray, t: np.ndarray) -> dict:
    if mode not in ("tcp", "tool"):
        raise ValueError("请选择单点TCP或刚性工具")
    ids = ["tcp"] if mode == "tcp" else ["origin", "x", "xy"]
    points, residuals = {}, []
    for point_id in ids:
        rows = [s for s in samples if s["point_id"] == point_id]
        if len({s["episode"] for s in rows}) < 3:
            raise ValueError(f"{point_id} 至少需要在3个不同姿态中选取同一实体点")
        qs = np.asarray([s["qpos_median_rad"] for s in rows], dtype=float)
        if qs.ndim != 2 or qs.shape[1] != 7 or not np.isfinite(qs).all():
            raise ValueError("样本关节角无效")
        if len(np.unique(np.round(qs, 3), axis=0)) < 3:
            raise ValueError(f"{point_id} 的机器人姿态重复，请选择不同姿态的数据")
        wrist_points = []
        for s in rows:
            T = np.asarray(s["T_base_wrist"], dtype=float)
            p = np.asarray(s["p_camera"], dtype=float)
            if T.shape != (4, 4) or p.shape != (3,) or not np.isfinite(T).all() or not np.isfinite(p).all():
                raise ValueError("样本坐标无效")
            wrist_points.append(T[:3, :3].T @ (R @ p + t - T[:3, 3]))
        values = np.asarray(wrist_points)
        center = values.mean(axis=0)
        residual = np.linalg.norm(values - center, axis=1) * 1000
        points[point_id] = center
        residuals.extend(residual.tolist())
    result = {
        "mode": mode, "units": "m", "num_samples": len(samples),
        "pose_count": len({s["episode"] for s in samples}),
        "point_count": len(ids), "wrist_link": samples[0]["wrist_link"],
        "residual_mm": {"rms": float(np.sqrt(np.mean(np.square(residuals)))), "max": max(residuals)},
        "tcp_points_wrist_m": [{"point_id": k, "p_wrist_m": v.tolist()} for k, v in points.items()],
        "default_tcp_point_id": ids[0],
        "orientation_defined": mode == "tool",
    }
    if mode == "tool":
        x = points["x"] - points["origin"]
        y = points["xy"] - points["origin"]
        if min(np.linalg.norm(x), np.linalg.norm(y)) < 0.005:
            raise ValueError("定义方向的点离原点不足5mm，请选择间距更大的特征点")
        z = np.cross(x, y)
        if np.linalg.norm(z) / (np.linalg.norm(x) * np.linalg.norm(y)) < 0.1:
            raise ValueError("工具方向点接近共线，无法确定工具坐标系")
        x /= np.linalg.norm(x)
        z /= np.linalg.norm(z)
        T = np.eye(4)
        T[:3, :3] = np.column_stack((x, np.cross(z, x), z))
        T[:3, 3] = points["origin"]
        result["T_wrist2tool"] = T.tolist()
    return result
