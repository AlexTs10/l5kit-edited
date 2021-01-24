import warnings
from typing import Callable, Tuple

import numpy as np

from ..geometry import rotation33_as_yaw, yaw_as_rotation33
from .ackerman_steering_model import fit_ackerman_model_exact
from .perturbation import Perturbation

#  if the offset or norm val is below this, we don't apply perturbation.
NUMERICAL_THRESHOLD = 0.00001


# TODO add docstrings for functions in the module
def get_offset_at_idx(
    trajectory: np.ndarray, perturbation_idx: int, lateral_offset_m: float, longitudinal_offset_m: float,
) -> np.ndarray:
    num_frames = trajectory.shape[0]
    if num_frames <= perturbation_idx + 1:
        # we need at least 1 trajectory point after the perturbation index to compute lateral direction.
        return np.array([0.0, 0.0], dtype=np.float32)

    point_to_be_perturbed = trajectory[perturbation_idx, :]
    # we use this to find the local lateral direction
    next_point_in_trajectory = trajectory[perturbation_idx + 1, :]

    longitudinal_direction = next_point_in_trajectory - point_to_be_perturbed
    #  the minimum distance between two consecutive trajectory points to go forward with direction computing.
    consecutive_point_distance_threshold = 0.00001
    #  the trajectory in the perturbation point may be a stationary point, so there's no direction info.
    #  in this case, we just look at the start and end of the array to get the overall motion direction.
    #  if that fails, we just don't perturb.

    if np.linalg.norm(longitudinal_direction) < consecutive_point_distance_threshold:
        longitudinal_direction = trajectory[-1, :] - trajectory[0, :]
        if np.linalg.norm(longitudinal_direction) < consecutive_point_distance_threshold:
            longitudinal_direction = np.array([0.0, 0.0], dtype=np.float32)
        else:
            longitudinal_direction /= np.linalg.norm(longitudinal_direction)
    else:
        longitudinal_direction /= np.linalg.norm(longitudinal_direction)

    lateral_direction = np.array([longitudinal_direction[1], -longitudinal_direction[0]])

    return lateral_offset_m * lateral_direction + longitudinal_offset_m * longitudinal_direction[:2]


def _get_history_and_future_frames_as_joint_trajectory(
    history_frames: np.ndarray, future_frames: np.ndarray
) -> np.ndarray:
    num_history_frames = len(history_frames)
    num_future_frames = len(future_frames)
    total_trajectory_length = num_history_frames + num_future_frames

    combined_trajectory = np.zeros((total_trajectory_length, 3), dtype=np.float32)

    # Note that history frames go backward in time from the anchor frame.
    combined_trajectory[:num_history_frames, :2] = history_frames["ego_translation"][::-1, :2]
    combined_trajectory[:num_history_frames, 2] = [
        rotation33_as_yaw(rot) for rot in history_frames["ego_rotation"][::-1]
    ]

    combined_trajectory[num_history_frames:, :2] = future_frames["ego_translation"][:, :2]
    combined_trajectory[num_history_frames:, 2] = [rotation33_as_yaw(rot) for rot in future_frames["ego_rotation"]]

    return combined_trajectory


def _compute_speeds_from_positions(trajectory: np.ndarray) -> np.ndarray:
    xs = trajectory[:, 0]
    ys = trajectory[:, 1]
    speeds = np.zeros(xs.shape)

    speeds[:-1] = np.sqrt((ys[1:] - ys[:-1]) ** 2 + (xs[1:] - xs[:-1]) ** 2)
    speeds[-1] = speeds[-2]
    return speeds


class AckermanPerturbation(Perturbation):
    def __init__(self, random_offset_generator: Callable, perturb_prob: float):
        """
        Apply Ackerman to get a feasible trajectory with probability perturb_prob.

        Args:
            random_offset_generator (RandomGenerator): a callable that yields 2 values
            perturb_prob (float): probability between 0 and 1 of applying the perturbation
        """
        self.perturb_prob = perturb_prob
        self.random_offset_generator = random_offset_generator
        if perturb_prob == 0:
            warnings.warn(
                "Consider replacing this object with None if no perturbation is intended", RuntimeWarning, stacklevel=2
            )

    def perturb(
        self, history_frames: np.ndarray, future_frames: np.ndarray, **kwargs: dict
    ) -> Tuple[np.ndarray, np.ndarray]:
        if np.random.rand() >= self.perturb_prob:
            return history_frames.copy(), future_frames.copy()

        lateral_offset_m, longitudinal_offset_m, yaw_offset_rad = self.random_offset_generator()

        if np.abs(lateral_offset_m) < NUMERICAL_THRESHOLD:
            warnings.warn("ack not applied because of low lateral_distance", RuntimeWarning, stacklevel=2)
            return history_frames.copy(), future_frames.copy()

        num_history_frames = len(history_frames)
        num_future_frames = len(future_frames)
        total_trajectory_length = num_history_frames + num_future_frames
        if total_trajectory_length < 2:  # TODO is this an error?
            #  we need at least 2 frames to compute speed and steering rate.
            return history_frames.copy(), future_frames.copy()

        reference_traj_sample = _get_history_and_future_frames_as_joint_trajectory(history_frames, future_frames)

        # laterally move the anchor frame
        offset_m = get_offset_at_idx(
            reference_traj_sample, num_history_frames - 1, lateral_offset_m, longitudinal_offset_m
        )

        trajectory_with_offset_applied = reference_traj_sample.copy()

        trajectory_with_offset_applied[num_history_frames - 1, :2] += offset_m

        # laterally rotate the anchor frame
        trajectory_with_offset_applied[num_history_frames - 1, 2] += yaw_offset_rad

        #  perform ackerman steering model fitting
        #  TODO(sms): Replace the call below to a cleaned up implementation

        gx = trajectory_with_offset_applied[:, 0].reshape((-1,))
        gy = trajectory_with_offset_applied[:, 1].reshape((-1,))
        gr = trajectory_with_offset_applied[:, 2].reshape((-1,))
        gv = _compute_speeds_from_positions(trajectory_with_offset_applied[:, :2]).reshape((-1,))

        x0 = trajectory_with_offset_applied[0, 0]
        y0 = trajectory_with_offset_applied[0, 1]
        r0 = trajectory_with_offset_applied[0, 2]
        v0 = gv[0]

        wgx = np.ones(total_trajectory_length)
        wgx[num_history_frames - 1] = 5
        wgy = np.ones(total_trajectory_length)
        wgy[num_history_frames - 1] = 5
        wgr = np.zeros(total_trajectory_length)
        wgv = np.zeros(total_trajectory_length)

        new_xs, new_ys, new_yaws, new_vs, new_acc, new_steer = fit_ackerman_model_exact(
            x0,
            y0,
            r0,
            v0,
            gx,
            gy,
            gr,
            gv,
            wgx,
            wgy,
            wgr,
            wgv,
        )

        new_trajectory = np.array(list(zip(new_xs, new_ys, new_yaws)))

        new_yaws_as_rotations = np.array([yaw_as_rotation33(pos_yaw[2]) for pos_yaw in new_trajectory])
        new_history_frames = history_frames.copy()
        new_future_frames = future_frames.copy()

        new_history_frames["ego_translation"][::-1, :2] = new_trajectory[:num_history_frames, :2]
        new_history_frames["ego_rotation"][::-1] = new_yaws_as_rotations[:num_history_frames]

        new_future_frames["ego_translation"][:, :2] = new_trajectory[num_history_frames:, :2]
        new_future_frames["ego_rotation"] = new_yaws_as_rotations[num_history_frames:]

        return new_history_frames, new_future_frames
