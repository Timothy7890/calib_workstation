#!/usr/bin/env python3
"""Solve eye-to-hand calibration from captured (left image + joints) pairs.

Setup
-----
The head stereo camera is FIXED to the robot (eye), and the chessboard is mounted
on the moving right hand (target on gripper). This is the classic *eye-to-hand*
case. We recover ``T_cam2base`` (camera frame -> robot base frame), which is the
real-world replacement for the nominal URDF camera mount.

Both eyes of the stereo pair are solved independently (``--eye both`` by default),
each with its own intrinsics, yielding ``T_camL2base`` and ``T_camR2base``. A
stereo cross-check reports the recovered right->left baseline as a sanity metric.

Pipeline (per captured sample, per eye)
---------------------------------------
1. Detect chessboard corners in the eye's image (left/ or right/).
2. ``solvePnP`` with that eye's calibrated intrinsics -> ``T_target2cam`` (board in
   camera frame).
3. Forward kinematics from the recorded joint vector -> ``T_gripper2base`` (hand
   tip in base frame).
4. Feed the *inverse* (``T_base2gripper``) plus ``T_target2cam`` to
   ``cv2.calibrateHandEye``; the returned X is ``T_cam2base``.

Units
-----
FK is in METERS, so object points are built in meters (square_size_mm / 1000) to
keep PnP translations in meters and consistent with FK.

Example
-------
    python solve_handeye.py \
        --data ./handeye_data/20260615_180000 \
        --intrinsics ../third_party/robot-twoeyes/data/calib_images/20260615172934/stereo_calibration.yaml \
        --urdf ../../g1_d.urdf \
        --board-size 11x8 --square-size 25
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

# Make repo-root modules importable (urdf_robot_model, ik_5d_suction_solver).
# solve_handeye.py: parents [0]=handeye [1]=scripts [2]=vision_arm_control(repo root)
_HANDEYE_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HANDEYE_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR), str(_HANDEYE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

def parse_board_size(value: str) -> Tuple[int, int]:
    try:
        cols, rows = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"棋盘格尺寸应为 11x8，收到 {value!r}") from exc
    if cols < 2 or rows < 2:
        raise ValueError("棋盘格内角点行列必须至少为2")
    return cols, rows


# --------------- small SE(3) helpers ---------------


def make_T(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(t, dtype=float).reshape(3)
    return T


def invert_T(T: np.ndarray) -> np.ndarray:
    R = T[:3, :3]
    t = T[:3, 3]
    Ti = np.eye(4)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ t
    return Ti


def rot_to_rpy(R: np.ndarray) -> Tuple[float, float, float]:
    """URDF fixed-axis RPY (R = Rz(yaw) Ry(pitch) Rx(roll))."""
    pitch = math.atan2(-R[2, 0], math.hypot(R[0, 0], R[1, 0]))
    if abs(math.cos(pitch)) < 1e-8:
        roll = 0.0
        yaw = math.atan2(-R[0, 1], R[1, 1])
    else:
        roll = math.atan2(R[2, 1], R[2, 2])
        yaw = math.atan2(R[1, 0], R[0, 0])
    return roll, pitch, yaw


def geodesic_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    R = Ra.T @ Rb
    c = (np.trace(R) - 1.0) / 2.0
    c = max(-1.0, min(1.0, c))
    return math.degrees(math.acos(c))


# --------------- calibration data loading ---------------


def load_intrinsics(yaml_path: Path, eye: str) -> Tuple[np.ndarray, np.ndarray]:
    """Read {eye}_camera_matrix / {eye}_dist_coeffs from a stereo yaml (eye = left|right)."""
    if yaml_path.suffix.lower() == ".json":
        try:
            payload = json.loads(yaml_path.read_text(encoding="utf-8"))
            K = np.asarray(payload["camera_matrix"], dtype=np.float64).reshape(3, 3)
            dist = np.asarray(payload["distortion"], dtype=np.float64).reshape(-1, 1)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Cannot read SDK intrinsics JSON {yaml_path}: {exc}") from exc
        return K, dist
    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)
    if not fs.isOpened():
        raise SystemExit(f"Cannot open intrinsics yaml: {yaml_path}")
    K = fs.getNode(f"{eye}_camera_matrix").mat()
    dist = fs.getNode(f"{eye}_dist_coeffs").mat()
    fs.release()
    if K is None or dist is None:
        raise SystemExit(f"{yaml_path} missing {eye}_camera_matrix / {eye}_dist_coeffs")
    return K.astype(np.float64), dist.astype(np.float64)


def object_points_m(board_size: Tuple[int, int], square_size_mm: float) -> np.ndarray:
    cols, rows = board_size
    objp = np.zeros((cols * rows, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    objp *= (square_size_mm / 1000.0)  # meters
    return objp


def detect_corners_strict(img: np.ndarray, board_size: Tuple[int, int]) -> Optional[np.ndarray]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCornersSB(gray, board_size)
    if found and corners is not None and corners.shape[0] == board_size[0] * board_size[1]:
        return corners
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, board_size, flags=flags)
    if not found or corners is None or corners.shape[0] != board_size[0] * board_size[1]:
        return None
    corners = cv2.cornerSubPix(
        gray, corners, (11, 11), (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
    )
    return corners


def reorder_q(q: np.ndarray, stored_names: List[str], model_names: List[str]) -> np.ndarray:
    """Reorder a captured joint vector into the URDF model's active-joint order."""
    if not stored_names:
        if q.size != len(model_names):
            raise ValueError(f"q size {q.size} != model joints {len(model_names)} and no names to map")
        return q
    index = {n: i for i, n in enumerate(stored_names)}
    out = np.empty(len(model_names), dtype=float)
    for i, name in enumerate(model_names):
        if name not in index:
            raise ValueError(f"Captured joints missing '{name}'. stored={stored_names}")
        out[i] = q[index[name]]
    return out


# --------------- main ---------------


def solve_eye(
    eye: str,
    data_dir: Path,
    K: np.ndarray,
    dist: np.ndarray,
    objp: np.ndarray,
    board_size: Tuple[int, int],
    model,
    method_map: dict,
    args: argparse.Namespace,
) -> Optional[dict]:
    """Run the full eye-to-hand pipeline for one eye. Returns the result dict, or None if skipped."""
    img_dir = data_dir / eye
    joints_dir = data_dir / "joints"
    if not img_dir.exists():
        print(f"\n[{eye}] SKIP: image dir {img_dir} does not exist.")
        return None

    print("\n" + "#" * 56)
    print(f"# EYE = {eye}   ({img_dir})")
    print("#" * 56)

    R_base2gripper: List[np.ndarray] = []
    t_base2gripper: List[np.ndarray] = []
    R_target2cam: List[np.ndarray] = []
    t_target2cam: List[np.ndarray] = []
    T_g2b_used: List[np.ndarray] = []
    T_t2c_used: List[np.ndarray] = []

    img_files = sorted(img_dir.glob("*.jpg"))
    print(f"Found {len(img_files)} images\n")

    for lf in img_files:
        stem = lf.stem
        jf = joints_dir / f"{stem}.json"
        if not jf.exists():
            print(f"  [{stem}] SKIP no joints json")
            continue

        img = cv2.imread(str(lf))
        if img is None:
            print(f"  [{stem}] SKIP unreadable image")
            continue

        corners = detect_corners_strict(img, board_size)
        if corners is None:
            print(f"  [{stem}] SKIP board not detected")
            continue

        ok, rvec, tvec = cv2.solvePnP(objp, corners, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            print(f"  [{stem}] SKIP solvePnP failed")
            continue
        R_t2c, _ = cv2.Rodrigues(rvec)
        t_t2c = tvec.reshape(3)

        rec = json.loads(jf.read_text())
        q = np.asarray(rec.get("q_rad", []), dtype=float).reshape(-1)
        stored_names = rec.get("joint_names", [])
        try:
            q_ordered = reorder_q(q, list(stored_names), model.joint_names)
        except ValueError as exc:
            print(f"  [{stem}] SKIP {exc}")
            continue

        p_tip, R_tip = model.fk(q_ordered)
        T_g2b = make_T(R_tip, p_tip)          # gripper(tip) -> base
        T_b2g = invert_T(T_g2b)               # base -> gripper

        R_base2gripper.append(T_b2g[:3, :3])
        t_base2gripper.append(T_b2g[:3, 3])
        R_target2cam.append(R_t2c)
        t_target2cam.append(t_t2c)
        T_g2b_used.append(T_g2b)
        T_t2c_used.append(make_T(R_t2c, t_t2c))
        print(f"  [{stem}] OK  pnp_t(m)=[{t_t2c[0]:+.3f} {t_t2c[1]:+.3f} {t_t2c[2]:+.3f}]")

    n = len(R_target2cam)
    print(f"\n[{eye}] Usable samples: {n}")
    if n < 3:
        print(f"[{eye}] Need at least 3 usable samples (recommended >= 8 with diverse arm poses). SKIP.")
        return None

    def solve_subset(idx):
        Rc2b, tc2b = cv2.calibrateHandEye(
            [R_base2gripper[i] for i in idx], [t_base2gripper[i] for i in idx],
            [R_target2cam[i] for i in idx], [t_target2cam[i] for i in idx],
            method=method_map[args.method],
        )
        return make_T(Rc2b, tc2b.reshape(3))

    def residuals(T_cb):
        # board->hand should be constant: const_i = inv(T_g2b_i) @ T_cam2base @ T_t2c_i
        consts_ = [invert_T(T_g2b_used[i]) @ T_cb @ T_t2c_used[i] for i in range(n)]
        trans_ = np.array([c[:3, 3] for c in consts_])
        # robust center: median translation + the rotation closest to all others
        med_t = np.median(trans_, axis=0)
        tr_mm = np.linalg.norm(trans_ - med_t, axis=1) * 1000.0
        rot_sum = [sum(geodesic_deg(consts_[i][:3, :3], consts_[j][:3, :3]) for j in range(n))
                   for i in range(n)]
        ref_R = consts_[int(np.argmin(rot_sum))][:3, :3]
        rot_deg = np.array([geodesic_deg(ref_R, c[:3, :3]) for c in consts_])
        return tr_mm, rot_deg

    inliers = list(range(n))
    T_cam2base = solve_subset(inliers)
    if not args.no_reject:
        for it in range(args.reject_iters):
            tr_mm, rot_deg = residuals(T_cam2base)
            keep = [i for i in range(n)
                    if tr_mm[i] <= args.max_trans_res_mm and rot_deg[i] <= args.max_rot_res_deg]
            if len(keep) < 6:
                print(f"[{eye}][reject] iter {it}: only {len(keep)} inliers left — thresholds too tight, stopping.")
                break
            if keep == inliers:
                print(f"[{eye}][reject] iter {it}: converged, {len(keep)}/{n} inliers.")
                break
            print(f"[{eye}][reject] iter {it}: {len(keep)}/{n} inliers "
                  f"(dropped {n - len(keep)} outliers).")
            inliers = keep
            T_cam2base = solve_subset(inliers)

    R_cam2base = T_cam2base[:3, :3]
    t_cam2base = T_cam2base[:3, 3]

    # Final residuals reported over INLIERS only.
    tr_mm_all, rot_deg_all = residuals(T_cam2base)
    trans_res_mm = np.array([tr_mm_all[i] for i in inliers])
    rot_res_deg = np.array([rot_deg_all[i] for i in inliers])

    rpy = rot_to_rpy(R_cam2base)
    t = t_cam2base.reshape(3)

    print("\n" + "=" * 56)
    print(f"[{eye}] T_cam2base  (camera -> {args.base_link})   method={args.method}")
    print("-" * 56)
    print(f"  xyz (m)  = [{t[0]:+.5f}, {t[1]:+.5f}, {t[2]:+.5f}]")
    print(f"  rpy (rad)= [{rpy[0]:+.5f}, {rpy[1]:+.5f}, {rpy[2]:+.5f}]")
    print(f"  rpy (deg)= [{math.degrees(rpy[0]):+.2f}, {math.degrees(rpy[1]):+.2f}, {math.degrees(rpy[2]):+.2f}]")
    print(f"  R =\n{np.array2string(R_cam2base, precision=5, suppress_small=True)}")
    print("-" * 56)
    print(f"[{eye}] Inliers used: {len(inliers)}/{n}")
    print("Consistency over inliers (board pose relative to hand should be constant):")
    print(f"  translation residual: mean={trans_res_mm.mean():.2f}mm  max={trans_res_mm.max():.2f}mm")
    print(f"  rotation residual:    mean={rot_res_deg.mean():.3f}deg max={rot_res_deg.max():.3f}deg")
    print("=" * 56)
    if trans_res_mm.mean() > 5.0 or rot_res_deg.mean() > 1.0:
        print(f"[{eye}][WARN] residuals still high -> add more / more-diverse arm poses, or check joint/image sync.")

    result = {
        "eye": eye,
        "base_link": args.base_link,
        "tip_link": args.tip_link,
        "method": args.method,
        "num_samples": n,
        "num_inliers": len(inliers),
        "inlier_indices": inliers,
        "board_size": list(board_size),
        "square_size_mm": args.square_size,
        "intrinsics_file": str(Path(args.intrinsics).resolve()),
        "intrinsics_yaml": str(Path(args.intrinsics).resolve()),
        "T_cam2base": T_cam2base.tolist(),
        "R_cam2base": R_cam2base.tolist(),
        "t_cam2base_m": t.tolist(),
        "rpy_rad": list(rpy),
        "residual_translation_mm": {"mean": float(trans_res_mm.mean()), "max": float(trans_res_mm.max())},
        "residual_rotation_deg": {"mean": float(rot_res_deg.mean()), "max": float(rot_res_deg.max())},
    }
    out_path = data_dir / f"handeye_result_{eye}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[{eye}] saved -> {out_path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Eye-to-hand calibration solver (left + right)")
    parser.add_argument("--data", required=True, help="Capture session dir (has left/ right/ and joints/)")
    parser.add_argument("--intrinsics", required=True,
                        help="stereo_calibration.yaml (uses {eye}_camera_matrix / {eye}_dist_coeffs)")
    parser.add_argument("--urdf", required=True, help="Robot URDF path")
    parser.add_argument("--eye", default="both", choices=["left", "right", "both"],
                        help="Which eye(s) to solve (default: both)")
    parser.add_argument("--base-link", default="torso_link", help="FK base frame (output is cam->this frame)")
    parser.add_argument("--tip-link", default="right_dex1_tool_link", help="Link the board is mounted near")
    parser.add_argument("--board-size", default="11x8", help="Chessboard inner corners, e.g. 11x8")
    parser.add_argument("--square-size", type=float, required=True, help="Square side length in mm")
    parser.add_argument("--method", default="park",
                        choices=["tsai", "park", "horaud", "andreff", "daniilidis"],
                        help="cv2.calibrateHandEye method")
    parser.add_argument("--no-reject", action="store_true",
                        help="Disable robust outlier rejection (keep all samples)")
    parser.add_argument("--max-trans-res-mm", type=float, default=8.0,
                        help="Inlier threshold on board-to-hand translation residual (mm)")
    parser.add_argument("--max-rot-res-deg", type=float, default=2.5,
                        help="Inlier threshold on board-to-hand rotation residual (deg)")
    parser.add_argument("--reject-iters", type=int, default=5,
                        help="Max robust re-solve iterations")
    args = parser.parse_args()

    board_size = parse_board_size(args.board_size)
    data_dir = Path(args.data)
    joints_dir = data_dir / "joints"
    if not joints_dir.exists():
        raise SystemExit(f"{data_dir} must contain joints/")

    objp = object_points_m(board_size, args.square_size)

    from .urdf_robot_model import URDFRobotModel
    model = URDFRobotModel(args.urdf, args.base_link, args.tip_link)
    print(f"[fk] base={args.base_link} tip={args.tip_link} joints={model.joint_names}")

    method_map = {
        "tsai": cv2.CALIB_HAND_EYE_TSAI,
        "park": cv2.CALIB_HAND_EYE_PARK,
        "horaud": cv2.CALIB_HAND_EYE_HORAUD,
        "andreff": cv2.CALIB_HAND_EYE_ANDREFF,
        "daniilidis": cv2.CALIB_HAND_EYE_DANIILIDIS,
    }

    eyes = ["left", "right"] if args.eye == "both" else [args.eye]
    results: dict = {}
    for eye in eyes:
        K, dist = load_intrinsics(Path(args.intrinsics), eye)
        res = solve_eye(eye, data_dir, K, dist, objp, board_size, model, method_map, args)
        if res is not None:
            results[eye] = res

    if not results:
        raise SystemExit("No eye produced a usable result.")

    # Stereo cross-check: the two cameras are rigidly linked, so
    # T_right2left = inv(T_camL2base) @ T_camR2base should equal the fixed stereo baseline.
    if "left" in results and "right" in results:
        T_l = np.array(results["left"]["T_cam2base"])
        T_r = np.array(results["right"]["T_cam2base"])
        T_r2l = invert_T(T_l) @ T_r
        baseline_mm = float(np.linalg.norm(T_r2l[:3, 3]) * 1000.0)
        print("\n" + "=" * 56)
        print("Stereo cross-check (right camera relative to left camera):")
        print(f"  baseline |t| = {baseline_mm:.2f} mm")
        print(f"  t (m)        = {np.array2string(T_r2l[:3, 3], precision=5, suppress_small=True)}")
        print("  (should match the known left<->right stereo baseline)")
        print("=" * 56)
        combined_path = data_dir / "handeye_result.json"
        combined_path.write_text(json.dumps({
            "left": results["left"],
            "right": results["right"],
            "T_right2left": T_r2l.tolist(),
            "stereo_baseline_mm": baseline_mm,
        }, indent=2, ensure_ascii=False))
        print(f"[OK] combined saved -> {combined_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
