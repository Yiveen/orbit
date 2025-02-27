# Copyright (c) 2022-2023, The ORBIT Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

"""Launch Isaac Sim Simulator first."""

import os

from omni.isaac.orbit.app import AppLauncher

# launch the simulator
app_experience = f"{os.environ['EXP_PATH']}/omni.isaac.sim.python.gym.headless.kit"
app_launcher = AppLauncher(headless=True, offscreen_render=True, experience=app_experience)
simulation_app = app_launcher.app

"""Rest everything follows."""


import gymnasium as gym
import os
import torch
import traceback
import unittest

import carb
import omni.usd

from omni.isaac.orbit.envs import RLTaskEnv, RLTaskEnvCfg

import omni.isaac.contrib_tasks  # noqa: F401
import omni.isaac.orbit_tasks  # noqa: F401
from omni.isaac.orbit_tasks.utils import parse_env_cfg


class TestRecordVideoWrapper(unittest.TestCase):
    """Test recording videos using the RecordVideo wrapper."""

    @classmethod
    def setUpClass(cls):
        # acquire all Isaac environments names
        cls.registered_tasks = list()
        for task_spec in gym.registry.values():
            if "Isaac" in task_spec.id:
                cls.registered_tasks.append(task_spec.id)
        # sort environments by name
        cls.registered_tasks.sort()
        # print all existing task names
        print(">>> All registered environments:", cls.registered_tasks)
        # directory to save videos
        cls.videos_dir = os.path.join(os.path.dirname(__file__), "output", "videos")

    def setUp(self) -> None:
        # common parameters
        self.num_envs = 16
        self.use_gpu = True
        # video parameters
        self.step_trigger = lambda step: step % 225 == 0
        self.video_length = 200

    def test_record_video(self):
        """Run random actions agent with recording of videos."""
        for task_name in self.registered_tasks:
            print(f">>> Running test for environment: {task_name}")
            # create a new stage
            omni.usd.get_context().new_stage()

            # parse configuration
            env_cfg: RLTaskEnvCfg = parse_env_cfg(task_name, use_gpu=self.use_gpu, num_envs=self.num_envs)

            # create environment
            env: RLTaskEnv = gym.make(task_name, cfg=env_cfg, render_mode="rgb_array")

            # directory to save videos
            videos_dir = os.path.join(self.videos_dir, task_name)
            # wrap environment to record videos
            env = gym.wrappers.RecordVideo(
                env, videos_dir, step_trigger=self.step_trigger, video_length=self.video_length, disable_logger=True
            )

            # reset environment
            env.reset()
            # simulate environment
            with torch.inference_mode():
                for _ in range(500):
                    # compute zero actions
                    actions = 2 * torch.rand(env.action_space.shape, device=env.unwrapped.device) - 1
                    # apply actions
                    _ = env.step(actions)

            # close the simulator
            env.close()


if __name__ == "__main__":
    try:
        unittest.main()
    except Exception as err:
        carb.log_error(err)
        carb.log_error(traceback.format_exc())
        raise
    finally:
        # close sim app
        simulation_app.close()
