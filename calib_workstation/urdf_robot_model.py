"""
Lightweight URDF FK/Jacobian adapter for IK5DSuctionSolver.

Only standard library + numpy are used. The adapter extracts one serial chain
between base_link and tip_link, then exposes:
    fk(q) -> p_tip_base, R_tip_base
    jacobian(q) -> J_pos, J_omega

The implementation supports fixed, revolute, continuous, and prismatic joints.
For the suction IK use case here, the right arm chain from torso_link to
right_dex1_tool_link contains 7 revolute joints plus fixed tool offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

def normalize(value):
    vector = np.asarray(value, dtype=float)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:
        raise ValueError("Cannot normalize a zero vector")
    return vector / norm


def skew(value):
    x, y, z = np.asarray(value, dtype=float).reshape(3)
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _parse_xyz(value: Optional[str], default) -> np.ndarray:
    if value is None:
        return np.asarray(default, dtype=float)
    parts = [float(x) for x in value.split()]
    if len(parts) != 3:
        raise ValueError(f"Expected 3 values, got: {value}")
    return np.asarray(parts, dtype=float)


def _rot_x(a: float) -> np.ndarray:
    c = math.cos(a)
    s = math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], dtype=float)


def _rot_y(a: float) -> np.ndarray:
    c = math.cos(a)
    s = math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], dtype=float)


def _rot_z(a: float) -> np.ndarray:
    c = math.cos(a)
    s = math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=float)


def rpy_to_matrix(rpy: Sequence[float]) -> np.ndarray:
    """URDF fixed-axis RPY: R = Rz(yaw) @ Ry(pitch) @ Rx(roll)."""
    roll, pitch, yaw = [float(x) for x in rpy]
    return _rot_z(yaw) @ _rot_y(pitch) @ _rot_x(roll)


def axis_angle_to_matrix(axis, angle: float) -> np.ndarray:
    axis = normalize(axis)
    k = skew(axis)
    eye = np.eye(3)
    return eye + math.sin(angle) * k + (1.0 - math.cos(angle)) * (k @ k)


def make_transform(xyz, rpy) -> np.ndarray:
    t = np.eye(4)
    t[:3, :3] = rpy_to_matrix(rpy)
    t[:3, 3] = np.asarray(xyz, dtype=float).reshape(3)
    return t


def make_motion_transform(joint_type: str, axis, q_value: float) -> np.ndarray:
    t = np.eye(4)
    if joint_type in ("revolute", "continuous"):
        t[:3, :3] = axis_angle_to_matrix(axis, q_value)
    elif joint_type == "prismatic":
        t[:3, 3] = normalize(axis) * float(q_value)
    elif joint_type == "fixed":
        pass
    else:
        raise ValueError(f"Unsupported joint type in chain: {joint_type}")
    return t


@dataclass
class URDFJoint:
    name: str
    joint_type: str
    parent: str
    child: str
    origin_xyz: np.ndarray
    origin_rpy: np.ndarray
    axis: np.ndarray
    limit: Optional[Tuple[float, float]]

    @property
    def is_active(self) -> bool:
        return self.joint_type in ("revolute", "continuous", "prismatic")


class URDFRobotModel:
    def __init__(self, urdf_path: str, base_link: str, tip_link: str):
        self.urdf_path = urdf_path
        self.base_link = base_link
        self.tip_link = tip_link
        self.joints = self._load_chain(urdf_path, base_link, tip_link)
        self.active_joints = [joint for joint in self.joints if joint.is_active]
        self.joint_names = [joint.name for joint in self.active_joints]
        self.n = len(self.active_joints)
        if self.n == 0:
            raise ValueError("Selected URDF chain has no active joints.")

    def _load_chain(self, urdf_path: str, base_link: str, tip_link: str) -> List[URDFJoint]:
        tree = ET.parse(urdf_path)
        root = tree.getroot()
        children_by_parent: Dict[str, List[URDFJoint]] = {}

        for joint_el in root.findall("joint"):
            name = joint_el.attrib["name"]
            joint_type = joint_el.attrib.get("type", "fixed")
            parent_el = joint_el.find("parent")
            child_el = joint_el.find("child")
            if parent_el is None or child_el is None:
                continue
            parent = parent_el.attrib["link"]
            child = child_el.attrib["link"]

            origin_el = joint_el.find("origin")
            if origin_el is None:
                origin_xyz = np.zeros(3)
                origin_rpy = np.zeros(3)
            else:
                origin_xyz = _parse_xyz(origin_el.attrib.get("xyz"), [0.0, 0.0, 0.0])
                origin_rpy = _parse_xyz(origin_el.attrib.get("rpy"), [0.0, 0.0, 0.0])

            axis_el = joint_el.find("axis")
            axis = _parse_xyz(axis_el.attrib.get("xyz"), [1.0, 0.0, 0.0]) if axis_el is not None else np.array([1.0, 0.0, 0.0])

            limit_el = joint_el.find("limit")
            limit = None
            if limit_el is not None and "lower" in limit_el.attrib and "upper" in limit_el.attrib:
                limit = (float(limit_el.attrib["lower"]), float(limit_el.attrib["upper"]))
            elif joint_type == "continuous":
                limit = (-math.pi, math.pi)

            joint = URDFJoint(
                name=name,
                joint_type=joint_type,
                parent=parent,
                child=child,
                origin_xyz=origin_xyz,
                origin_rpy=origin_rpy,
                axis=axis,
                limit=limit,
            )
            children_by_parent.setdefault(parent, []).append(joint)

        path = self._find_chain(children_by_parent, base_link, tip_link)
        if path is None:
            raise ValueError(f"Cannot find URDF chain from {base_link!r} to {tip_link!r}.")
        return path

    def _find_chain(
        self,
        children_by_parent: Dict[str, List[URDFJoint]],
        base_link: str,
        tip_link: str,
    ) -> Optional[List[URDFJoint]]:
        stack = [(base_link, [])]
        visited = set()
        while stack:
            link, path = stack.pop()
            if link == tip_link:
                return path
            if link in visited:
                continue
            visited.add(link)
            for joint in children_by_parent.get(link, []):
                stack.append((joint.child, path + [joint]))
        return None

    def joint_limits(self) -> np.ndarray:
        limits = []
        for joint in self.active_joints:
            if joint.limit is None:
                limits.append((-math.pi, math.pi))
            else:
                limits.append(joint.limit)
        return np.asarray(limits, dtype=float)

    def _forward(self, q):
        q_arr = np.asarray(q, dtype=float).reshape(-1)
        if q_arr.size != self.n:
            raise ValueError(f"Expected q with {self.n} joints, got {q_arr.size}.")

        t = np.eye(4)
        active_index = 0
        joint_origins = []
        joint_axes_base = []
        joint_types = []

        for joint in self.joints:
            # URDF joint origin: parent link frame -> joint frame.
            t = t @ make_transform(joint.origin_xyz, joint.origin_rpy)

            if joint.is_active:
                axis_base = t[:3, :3] @ normalize(joint.axis)
                joint_origins.append(t[:3, 3].copy())
                joint_axes_base.append(normalize(axis_base))
                joint_types.append(joint.joint_type)

                # Joint motion happens after the origin transform, about axis in joint frame.
                t = t @ make_motion_transform(joint.joint_type, joint.axis, q_arr[active_index])
                active_index += 1

        return t[:3, 3].copy(), t[:3, :3].copy(), joint_origins, joint_axes_base, joint_types

    def fk(self, q):
        p_tip, r_tip, _, _, _ = self._forward(q)
        return p_tip, r_tip

    def jacobian(self, q):
        p_tip, _, joint_origins, joint_axes_base, joint_types = self._forward(q)
        j_pos = np.zeros((3, self.n))
        j_omega = np.zeros((3, self.n))

        for i, (origin, axis, joint_type) in enumerate(zip(joint_origins, joint_axes_base, joint_types)):
            if joint_type in ("revolute", "continuous"):
                # Revolute geometric Jacobian in selected base_link frame.
                j_pos[:, i] = np.cross(axis, p_tip - origin)
                j_omega[:, i] = axis
            elif joint_type == "prismatic":
                j_pos[:, i] = axis
                j_omega[:, i] = np.zeros(3)
            else:
                raise ValueError(f"Unexpected active joint type: {joint_type}")

        return j_pos, j_omega


if __name__ == "__main__":
    model = URDFRobotModel("g1_d.urdf", "torso_link", "right_dex1_tool_link")
    q = np.zeros(model.n)
    p, r = model.fk(q)
    j_pos, j_omega = model.jacobian(q)
    print("URDF chain loaded.")
    print("joint_names:", model.joint_names)
    print("joint_limits:\n", model.joint_limits())
    print("fk position:", p)
    print("fk rotation:\n", r)
    print("J_pos shape:", j_pos.shape)
    print("J_omega shape:", j_omega.shape)
