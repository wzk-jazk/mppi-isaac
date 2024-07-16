from isaacgym import gymapi
from isaacgym import gymtorch
from dataclasses import dataclass, field
import torch
import numpy as np
from enum import Enum
from typing import List, Optional, Any


@dataclass
class IsaacGymConfig(object):
    dt: float = 0.05
    substeps: int = 2
    use_gpu_pipeline: bool = True
    num_client_threads: int = 0
    viewer: bool = False
    num_obstacles: int = 10
    spacing: float = 6.0


def parse_isaacgym_config(cfg: IsaacGymConfig, device: str = "cuda:0") -> gymapi.SimParams:
    sim_params = gymapi.SimParams()
    sim_params.dt = cfg.dt
    sim_params.substeps = cfg.substeps
    sim_params.use_gpu_pipeline = device == "cuda:0"
    sim_params.num_client_threads = cfg.num_client_threads

    sim_params.up_axis = gymapi.UP_AXIS_Z
    sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)
    sim_params.physx.solver_type = 1
    sim_params.physx.num_position_iterations = 6
    sim_params.physx.num_velocity_iterations = 1
    sim_params.physx.contact_offset = 0.01
    sim_params.physx.rest_offset = 0.0
    sim_params.physx.friction_offset_threshold = 0.01
    sim_params.physx.friction_correlation_distance = 0.001

    # return the configured params
    return sim_params


class SupportedActorTypes(Enum):
    Axis = 1
    Robot = 2
    Sphere = 3
    Box = 4


@dataclass
class ActorWrapper:
    type: SupportedActorTypes
    name: str
    dof_mode: str = "velocity"
    init_pos: List[float] = field(default_factory=lambda: [0, 0, 0])
    init_ori: List[float] = field(default_factory=lambda: [0, 0, 0, 1])
    size: List[float] = field(default_factory=lambda: [0.1, 0.1, 0.1])
    mass: float = 1.0  # kg
    color: List[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    fixed: bool = False
    collision: bool = True
    friction: float = 1.0
    handle: Optional[int] = None
    flip_visual: bool = False
    urdf_file: str = None
    visualize_link: str = None
    gravity: bool = True
    differential_drive: bool = False
    init_joint_pose: List[float] = None
    wheel_radius: Optional[float] = None
    wheel_base: Optional[float] = None
    wheel_count: Optional[float] = None
    left_wheel_joints: Optional[List[str]] = None
    right_wheel_joints: Optional[List[str]] = None
    caster_links: Optional[List[str]] = None
    noise_sigma_size: Optional[List[float]] = None
    noise_percentage_mass: float = 0.0
    noise_percentage_friction: float = 0.0


from mppiisaac.utils.isaacgym_utils import load_asset, add_ground_plane, load_actor_cfgs


class IsaacGymWrapper:
    def __init__(
        self,
        cfg: IsaacGymConfig,
        actors: List[str],
        init_positions: List[List[float]] = None,
        num_envs: int = 1,
        viewer: bool = False,
        device: str = "cuda:0",
        interactive_goal = True
    ):
        self._gym = gymapi.acquire_gym()
        self.env_cfg = load_actor_cfgs(actors)
        self.device = device

        # TODO: make sure there are no actors with duplicate names
        # TODO: check for initial position collisions of actors

        robots = [a for a in self.env_cfg if a.type == "robot"]
        if init_positions is not None:
            assert len(robots) == len(init_positions)

            for init_pos, actor_cfg in zip(init_positions, robots):
                actor_cfg.init_pos = init_pos

        self.cfg = cfg
        if viewer:
            self.cfg.viewer = viewer
        self.interactive_goal = interactive_goal
        self.num_envs = num_envs
        self.restarted = 1
        self.start_sim()

    def initialize_keyboard_listeners(self):
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_A, "left")
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_S, "down")
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_D, "right")
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_W, "up")
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_E, "high")
        self._gym.subscribe_viewer_keyboard_event(self.viewer, gymapi.KEY_Q, "low")

    def start_sim(self):
        self._sim = self._gym.create_sim(
            compute_device=0,
            graphics_device=0,
            type=gymapi.SIM_PHYSX,
            params=parse_isaacgym_config(self.cfg, self.device),
        )

        if self.cfg.viewer:
            self.viewer = self._gym.create_viewer(self._sim, gymapi.CameraProperties())
            if self.interactive_goal:
                self.initialize_keyboard_listeners()
        else:
            self.viewer = None

        add_ground_plane(self._gym, self._sim)

        # Always add dummy obst at the end
        if self.restarted == 2:
            self.env_cfg.append(
            ActorWrapper(
                **{
                    "type": "sphere",
                    "name": "dummy",
                    "handle": None,
                    "size": [0.1],
                    "fixed": True,
                    "init_pos": [0, 0, -10],
                    "collision": False
                }
            )
            )
            self.restarted += 1
        else:
            self.restarted += 1

        # Load / create assets for all actors in the envs
        env_actor_assets = []
        for actor_cfg in self.env_cfg:
            asset = load_asset(self._gym, self._sim, actor_cfg)
            env_actor_assets.append(asset)

        # Create envs and fill with assets
        self.envs = []
        for env_idx in range(self.num_envs):
            env = self._gym.create_env(
                self._sim,
                gymapi.Vec3(-self.cfg.spacing, 0.0, -self.cfg.spacing),
                gymapi.Vec3(self.cfg.spacing, self.cfg.spacing, self.cfg.spacing),
                int(self.num_envs**0.5),
            )

            for actor_asset, actor_cfg in zip(env_actor_assets, self.env_cfg):
                actor_cfg.handle = self._create_actor(
                    env, env_idx, actor_asset, actor_cfg
                )
            self.envs.append(env)

        self._visualize_link_present = any([a.visualize_link for a in self.env_cfg])

        self._gym.prepare_sim(self._sim)

        self._root_state = gymtorch.wrap_tensor(
            self._gym.acquire_actor_root_state_tensor(self._sim)
        ).view(self.num_envs, -1, 13)
        self.saved_root_state = None
        self._dof_state = gymtorch.wrap_tensor(
            self._gym.acquire_dof_state_tensor(self._sim)
        ).view(self.num_envs, -1)
        self._rigid_body_state = gymtorch.wrap_tensor(
            self._gym.acquire_rigid_body_state_tensor(self._sim)
        ).view(self.num_envs, -1, 13)

        self._net_contact_force = gymtorch.wrap_tensor(
            self._gym.acquire_net_contact_force_tensor(self._sim)
        ).view(self.num_envs, -1, 3)

        # save buffer of ee states
        if self._visualize_link_present:
            self.visualize_link_buffer = []

        # helpfull slices
        self.robot_indices = torch.tensor([i for i, a in enumerate(self.env_cfg) if a.type == "robot"], device=self.device)
        self.obstacle_indices = torch.tensor([i for i, a in enumerate(self.env_cfg) if (a.type in ["sphere", "box"] and a.name != "dummy")], device=self.device)

        if self._visualize_link_present:
            self.visualize_link_pos = self._rigid_body_state[
                :, self.robot_rigid_body_viz_idx, 0:3
            ]  # [x, y, z]

        self._gym.refresh_actor_root_state_tensor(self._sim)
        self._gym.refresh_dof_state_tensor(self._sim)
        self._gym.refresh_rigid_body_state_tensor(self._sim)
        self._gym.refresh_net_contact_force_tensor(self._sim)

        # set initial joint poses
        robots = [a for a in self.env_cfg if a.type == "robot"]
        for robot in robots:
            dof_state = []
            if robot.init_joint_pose:
                dof_state += robot.init_joint_pose
                print(dof_state)
            else:
                dof_state += (
                    [0] * 2 * self._gym.get_actor_dof_count(self.envs[0], robot.handle)
                )
        dof_state = (
            torch.tensor(dof_state, device=self.device)
            .type(torch.float32)
            .repeat(self.num_envs, 1)
        )
        self._gym.set_dof_state_tensor(self._sim, gymtorch.unwrap_tensor(dof_state))
        self._gym.refresh_dof_state_tensor(self._sim)

    def reset_to_initial_poses(self):
        for actor in self.env_cfg:
            actor_state = torch.tensor(
                [*actor.init_pos, *actor.init_ori, *[0] * 6], device=self.device
            )
            self._root_state[:, actor.handle] = actor_state

        self._gym.set_actor_root_state_tensor(
            self._sim, gymtorch.unwrap_tensor(self._root_state)
        )

        # set initial joint poses
        robots = [a for a in self.env_cfg if a.type == "robot"]
        for robot in robots:
            dof_state = []
            if robot.init_joint_pose:
                dof_state += robot.init_joint_pose
                print(dof_state)
            else:
                dof_state += (
                    [0] * 2 * self._gym.get_actor_dof_count(self.envs[0], robot.handle)
                )
        dof_state = (
            torch.tensor(dof_state, device=self.device)
            .type(torch.float32)
            .repeat(self.num_envs, 1)
        )
        self._gym.set_dof_state_tensor(self._sim, gymtorch.unwrap_tensor(dof_state))
        self._gym.refresh_dof_state_tensor(self._sim)

    @property
    def num_robots(self):
        return len(self._robot_indices)

    @property
    def robot_positions(self):
        return torch.index_select(self._root_state, 1, self._robot_indices)[:, :, 0:3]

    @property
    def robot_velocities(self):
        return torch.index_select(self._root_state, 1, self._robot_indices)[:, :, 7:10]

    @property
    def obstacle_positions(self):
        return torch.index_select(self._root_state, 1, self._obstacle_indices)[
            :, :, 0:3
        ]

    @property
    def ostacle_velocities(self):
        return torch.index_select(self._root_state, 1, self._obstacle_indices)[
            :, :, 7:10
        ]

    def _get_actor_index_by_name(self, name: str):
        return torch.tensor([a.name for a in self.env_cfg].index(name), device=self.device)

    def _get_actor_index_by_robot_index(self, robot_idx: int):
        return self._robot_indices[robot_idx]

    # Getters
    def get_actor_position_by_actor_index(self, actor_idx: int):
        return torch.index_select(self._root_state, 1, actor_idx)[:, 0, 0:3]

    def get_actor_position_by_name(self, name: str):
        actor_idx = self._get_actor_index_by_name(name)
        return self.get_actor_position_by_actor_index(actor_idx)

    def get_actor_position_by_robot_index(self, robot_idx: int):
        actor_idx = self._get_actor_index_by_robot_index(robot_idx)
        return self.get_actor_position_by_actor_index(actor_idx)

    def get_actor_velocity_by_actor_index(self, idx: int):
        return torch.index_select(self._root_state, 1, idx)[:, 0, 7:10]

    def get_actor_velocity_by_name(self, name: str):
        actor_idx = self._get_actor_index_by_name(name)
        return self.get_actor_velocity_by_actor_index(actor_idx)

    def get_actor_velocity_by_robot_index(self, robot_idx: int):
        actor_idx = self._get_actor_index_by_robot_index(robot_idx)
        return self.get_actor_velocity_by_actor_index(actor_idx)

    def get_actor_orientation_by_actor_index(self, idx: int):
        return torch.index_select(self._root_state, 1, idx)[:, 0, 3:7]

    def get_actor_orientation_by_name(self, name: str):
        actor_idx = self._get_actor_index_by_name(name)
        return self.get_actor_orientation_by_actor_index(actor_idx)

    def get_actor_orientation_by_robot_index(self, robot_idx: int):
        actor_idx = self._get_actor_index_by_robot_index(robot_idx)
        return self.get_actor_orientation_by_actor_index(actor_idx)

    def get_rigid_body_by_rigid_body_index(self, rigid_body_idx: int):
        return torch.index_select(self._rigid_body_state, 1, rigid_body_idx)[:, 0, :]

    def get_actor_link_by_name(self, actor_name: str, link_name: str):
        actor_idx = self._get_actor_index_by_name(actor_name)
        rigid_body_idx = torch.tensor(
            self._gym.find_actor_rigid_body_index(
                self.envs[0], actor_idx, link_name, gymapi.IndexDomain.DOMAIN_ENV
            ),
            device=self.device,
        )
        return self.get_rigid_body_by_rigid_body_index(rigid_body_idx)

    def get_actor_contact_forces_by_name(self, actor_name: str, link_name: str):
        actor_idx = self._get_actor_index_by_name(actor_name)
        rigid_body_idx = torch.tensor(
            self._gym.find_actor_rigid_body_index(
                self.envs[0], actor_idx, link_name, gymapi.IndexDomain.DOMAIN_ENV
            ),
            device=self.device,
        )
        return self._net_contact_force[:, rigid_body_idx]

    # torch.index_select(self._net_contact_force, 1, rigid_body_idx)
    # self._net_contact_force[:, rigid_body_idx]

    # NOTE: we're using the tensor api everywhere so it works parallelized for the number of envs
    # Setters
    def set_actor_position_by_actor_index(
        self, position: List[float], actor_idx: int
    ) -> None:
        self._root_state[:, actor_idx, :3] = position
        self._gym.set_actor_root_state_tensor_indexed(
            self._sim, gymtorch.unwrap_tensor(self._root_state), gymtorch.unwrap_tensor(torch.tensor([actor_idx], dtype=torch.int32, device=self.device)), 1
        )

    def set_actor_position_by_name(self, position: List[float], name: str) -> None:
        actor_idx = [a.name for a in self.env_cfg].index(name)
        self.set_actor_position_by_actor_index(position, actor_idx)

    def set_actor_position_by_robot_index(
        self, position: List[float], robot_idx: str
    ) -> None:
        actor_idx = self._robot_indices[robot_idx]
        self.set_actor_position_by_actor_index(position, actor_idx)

    def set_actor_velocity_by_actor_index(
        self, velocity: List[float], actor_idx: int
    ) -> None:
        self._root_state[:, actor_idx, 7:10] = velocity
        self._gym.set_actor_root_state_tensor_indexed(
            self._sim, gymtorch.unwrap_tensor(self._root_state), gymtorch.unwrap_tensor(torch.tensor([actor_idx], dtype=torch.int32, device=self.device)), 1
        )

    def set_actor_velocity_by_name(self, velocity: List[float], name: str) -> None:
        actor_idx = [a.name for a in self.env_cfg].index(name)
        self.set_actor_velocity_by_actor_index(torch.tensor(velocity), actor_idx)

    def set_actor_velocity_by_robot_index(
        self, velocity: List[float], robot_idx: str
    ) -> None:
        actor_idx = self._robot_indices[robot_idx]
        self.set_actor_velocity_by_actor_index(velocity, actor_idx)

    def set_actor_dof_state(self, state):
        self._gym.set_dof_state_tensor(self._sim, gymtorch.unwrap_tensor(state))

    def set_dof_velocity_target_tensor(self, u):
        self._gym.set_dof_velocity_target_tensor(self._sim, gymtorch.unwrap_tensor(u))

    def set_dof_actuation_force_tensor(self, u):
        self._gym.set_dof_actuation_force_tensor(self._sim, gymtorch.unwrap_tensor(u))

    # # Note: difficult because the number of dofs can change per actor. thus we cannot simply use view to rearange the dof_state_tensor for easy access.
    # # We have to lookup the exact indices of the dofs for the given actor name
    # def set_robot_position_by_name(self, position: List[float], name: str):
    #     actor_dof_count = self._gym.get_actor_dof_count(self.envs[0], actor.handle)
    #     dof_dict = self._gym.get_actor_dof_dict(self.envs[0], actor.handle)
    #     robot_idx = [a.name for a in self.env_cfg].index(name)
    #     return torch.index_select(self._root_state, 1, robot_idx)[:, :, 0:3]

    def stop_sim(self):
        if self.viewer:
            self.gym.destroy_viewer(self.viewer)
        for env_idx in range(self.num_envs):
            self.gym.destroy_env(self.envs[env_idx])
        self.gym.destroy_sim(self.sim)

    def add_to_envs(self, additions):
        for a in additions:
            self.env_cfg.append(ActorWrapper(**a))
        self.stop_sim()
        self.start_sim()

    def _create_actor(self, env, env_idx, asset, actor: ActorWrapper) -> int:
        if actor.noise_sigma_size is not None:
            asset = load_asset(self._gym, self._sim, actor)

        pose = gymapi.Transform()
        pose.p = gymapi.Vec3(*actor.init_pos)
        pose.r = gymapi.Quat(*actor.init_ori)
        handle = self._gym.create_actor(
            env=env,
            asset=asset,
            pose=pose,
            name=actor.name,
            group=env_idx if actor.collision else env_idx + self.num_envs,
        )

        if actor.noise_sigma_size:
            actor.color = np.random.rand(3)

        self._gym.set_rigid_body_color(
            env, handle, 0, gymapi.MESH_VISUAL_AND_COLLISION, gymapi.Vec3(*actor.color)
        )
        props = self._gym.get_actor_rigid_body_properties(env, handle)
        actor_mass_noise = np.random.uniform(
            -actor.noise_percentage_mass * actor.mass,
            actor.noise_percentage_mass * actor.mass,
        )
        props[0].mass = actor.mass + actor_mass_noise
        self._gym.set_actor_rigid_body_properties(env, handle, props)

        body_names = self._gym.get_actor_rigid_body_names(env, handle)
        body_to_shape = self._gym.get_actor_rigid_body_shape_indices(env, handle)
        caster_shapes = [
            b.start
            for body_idx, b in enumerate(body_to_shape)
            if actor.caster_links is not None
            and body_names[body_idx] in actor.caster_links
        ]

        props = self._gym.get_actor_rigid_shape_properties(env, handle)
        for i, p in enumerate(props):
            actor_friction_noise = np.random.uniform(
                -actor.noise_percentage_friction * actor.friction,
                actor.noise_percentage_friction * actor.friction,
            )
            p.friction = actor.friction + actor_friction_noise
            p.torsion_friction = np.random.uniform(0.001, 0.01)
            p.rolling_friction = actor.friction + actor_friction_noise

            if i in caster_shapes:
                p.friction = 0
                p.torsion_friction = 0
                p.rolling_friction = 0

        self._gym.set_actor_rigid_shape_properties(env, handle, props)

        if actor.type == "robot":
            # TODO: Currently the robot_rigid_body_viz_idx is only supported for a single robot case.
            if actor.visualize_link:
                self.robot_rigid_body_viz_idx = self._gym.find_actor_rigid_body_index(
                    env, handle, actor.visualize_link, gymapi.IndexDomain.DOMAIN_ENV
                )

            props = self._gym.get_asset_dof_properties(asset)
            if actor.dof_mode == "effort":
                props["driveMode"].fill(gymapi.DOF_MODE_EFFORT)
                props["stiffness"].fill(0.0)
                props["armature"].fill(0.0)
                props["damping"].fill(10.0)
            elif actor.dof_mode == "velocity":
                props["driveMode"].fill(gymapi.DOF_MODE_VEL)
                props["stiffness"].fill(0.0)
                props["damping"].fill(600.0)
            elif actor.dof_mode == "position":
                props["driveMode"].fill(gymapi.DOF_MODE_POS)
                props["stiffness"].fill(80.0)
                props["damping"].fill(0.0)
            else:
                raise ValueError("Invalid dof_mode")
            self._gym.set_actor_dof_properties(env, handle, props)
        return handle

    def _ik(self, actor, u):
        r = actor.wheel_radius
        L = actor.wheel_base
        # wheel_sets = actor.wheel_count // 2

        # Diff drive fk
        u_left_wheel = (u[:, 0] / r) - ((L * u[:, 1]) / (2 * r))
        u_right_wheel = (u[:, 0] / r) + ((L * u[:, 1]) / (2 * r))

        # if wheel_sets > 1:
        #     u_ik = u_ik.repeat(1, wheel_sets)

        return u_left_wheel, u_right_wheel

    def apply_robot_cmd(self, u_desired):
        if len(u_desired.size()) == 1:
            u_desired = u_desired.unsqueeze(0)

        dof_shape = list(self._dof_state.size())
        dof_shape[1] = dof_shape[1] // 2
        u = torch.zeros(dof_shape, device=self.device)

        u_desired_idx = 0
        dof_mode = None
        for actor in self.env_cfg:
            if actor.type != "robot":
                continue
            dof_mode = actor.dof_mode

            if dof_mode is not None and dof_mode != actor.dof_mode:
                raise ValueError("All robots must have the same dof_mode")

            actor_dof_count = self._gym.get_actor_dof_count(self.envs[0], actor.handle)
            dof_dict = self._gym.get_actor_dof_dict(self.envs[0], actor.handle)

            # use first two u_desired values for differential drive (vel, yaw_rate)
            if actor.differential_drive:
                u_left_desired, u_right_desired = self._ik(
                    actor, u_desired[:, :2]
                )
                u_desired_idx += 2

            for name, i in dof_dict.items():
                if actor.differential_drive and name in actor.left_wheel_joints:
                    u[:, i] = u_left_desired
                elif actor.differential_drive and name in actor.right_wheel_joints:
                    u[:, i] = u_right_desired
                else:
                    u[:, i] = u_desired[:, u_desired_idx]
                    u_desired_idx += 1

            if actor.name == 'panda_gripper':
                u[u[:, actor_dof_count -1] > 0.0, actor_dof_count-1] = 0.1
                u[u[:, actor_dof_count -1] >= 0.0, actor_dof_count-1] = -0.1
                u[u[:, actor_dof_count -1] > 0.0, actor_dof_count-2] = 0.1
                u[u[:, actor_dof_count -1] >= 0.0, actor_dof_count-2] = -0.1

        if dof_mode == "effort":
            self.set_dof_actuation_force_tensor(u)
        elif dof_mode == "velocity":
            self.set_dof_velocity_target_tensor(u)
        elif dof_mode == "position":
            self.set_actor_dof_state(u)

    def reset_robot_state(self, q, qdot):
        """
        This function is mainly used for compatibility with gym_urdf_envs pybullet _sim.
        """

        q_idx = 0

        dof_state = []
        for actor in self.env_cfg:
            if actor.type != "robot":
                continue

            actor_dof_count = self._gym.get_actor_dof_count(self.envs[0], actor.handle)

            if actor.differential_drive:
                actor_q_count = actor_dof_count - (actor.wheel_count - 3)
            else:
                actor_q_count = actor_dof_count

            actor_q = q[q_idx : q_idx + actor_q_count]
            actor_qdot = qdot[q_idx : q_idx + actor_q_count]

            if actor.differential_drive:
                pos = actor_q[:3]
                vel = actor_qdot[:3]

                self.set_state_tensor_by_pos_vel(actor.handle, pos, vel)

                # assuming wheels at the back of the dof tensor
                actor_q = list(actor_q[3:]) + [0] * actor.wheel_count
                actor_qdot = list(actor_qdot[3:]) + [0] * actor.wheel_count

            for _q, _qdot in zip(actor_q, actor_qdot):
                dof_state.append(_q)
                dof_state.append(_qdot)

            q_idx += actor_q_count

        dof_state_tensor = torch.tensor(dof_state).type(torch.float32).to(self.device)

        dof_state_tensor = dof_state_tensor.repeat(self.num_envs, 1)
        self.set_actor_dof_state(dof_state_tensor)

        self._gym.set_actor_root_state_tensor(
            self._sim, gymtorch.unwrap_tensor(self._root_state)
        )

    def interactive_goal_update(self):
        for e in self._gym.query_viewer_action_events(self.viewer):
            goal_pos = self.get_actor_position_by_name("goal")
            delta_pos = 0.1
            if e.action == "up":
                goal_pos[0, 1] -= delta_pos
            if e.action == "down":
                goal_pos[0, 1] += delta_pos
            if e.action == "left":
                goal_pos[0, 0] += delta_pos
            if e.action == "right":
                goal_pos[0, 0] -= delta_pos
            if e.action == "high":
                goal_pos[0, 2] += delta_pos
            if e.action == "low":
                goal_pos[0, 2] -= delta_pos
            self.set_actor_position_by_name(position=goal_pos, name="goal")

    def step(self):
        self._gym.simulate(self._sim)
        self._gym.fetch_results(self._sim, True)
        self._gym.refresh_actor_root_state_tensor(self._sim)
        self._gym.refresh_dof_state_tensor(self._sim)
        self._gym.refresh_rigid_body_state_tensor(self._sim)
        self._gym.refresh_net_contact_force_tensor(self._sim)

        if self.viewer is not None:
            self._gym.step_graphics(self._sim)
            self._gym.draw_viewer(self.viewer, self._sim, False)

        if self._visualize_link_present:
            self.visualize_link_buffer.append(self.visualize_link_pos.clone())

        if self.interactive_goal:
            self.interactive_goal_update()


    def set_root_state_tensor_by_actor_idx(self, state_tensor, idx):
        for i in range(self.num_envs):
            self._root_state[i, idx] = state_tensor

    def save_root_state(self):
        self.saved_root_state = self._root_state.clone()

    def get_saved_root_state(self):
        return self.saved_root_state

    def reset_root_state(self):
        if self._visualize_link_present:
            self.visualize_link_buffer = []

        if self.saved_root_state is not None:
            self._gym.set_actor_root_state_tensor(
                self._sim, gymtorch.unwrap_tensor(self.saved_root_state)
            )

    def set_state_tensor_by_pos_vel(self, handle, pos, vel):
        roll = 0
        pitch = 0
        yaw = pos[2]
        orientation = [
            np.sin(roll / 2) * np.cos(pitch / 2) * np.cos(yaw / 2)
            - np.cos(roll / 2) * np.sin(pitch / 2) * np.sin(yaw / 2),
            np.cos(roll / 2) * np.sin(pitch / 2) * np.cos(yaw / 2)
            + np.sin(roll / 2) * np.cos(pitch / 2) * np.sin(yaw / 2),
            np.cos(roll / 2) * np.cos(pitch / 2) * np.sin(yaw / 2)
            - np.sin(roll / 2) * np.sin(pitch / 2) * np.cos(yaw / 2),
            np.cos(roll / 2) * np.cos(pitch / 2) * np.cos(yaw / 2),
        ]

        self.root_state[:, handle, :2] = torch.tensor(pos[:2], device=self.device)
        self.root_state[:, handle, 3:7] = torch.tensor(orientation, device=self.device)
        self.root_state[:, handle, 7:10] = torch.tensor(vel, device=self.device)

    def update_root_state_tensor_by_obstacles(self, obstacles):
        """
        Note: obstacles param should be a list of obstacles,
        where each obstacle is a list of the following order [position, velocity, type, size]
        """
        env_cfg_changed = False

        for i, obst in enumerate(list(obstacles.values())):
            pos = obst["position"]
            vel = obst["velocity"]
            o_type = "sphere"
            o_size = obst["size"]
            name = f"{o_type}{i}"
            try:
                obst_idx = [
                    idx for idx, actor in enumerate(self.env_cfg) if actor.name == name
                ][0]
            except:
                self.env_cfg.append(
                    ActorWrapper(
                        **{
                            "type": o_type,
                            "name": name,
                            "handle": None,
                            "size": o_size,
                            "fixed": True,
                        }
                    )
                )
                env_cfg_changed = True
                continue

            obst_state = torch.tensor(
                [*pos, 0, 0, 0, 1, *vel, 0, 0, 0], device=self.device
            )

            # Note: reset simulator if size changed, because this cannot be done at runtime.
            if not all([a == b for a, b in zip(o_size, self.env_cfg[obst_idx].size)]):
                env_cfg_changed = True
                self.env_cfg[obst_idx].size = o_size

            for j, env in enumerate(self.envs):
                self._root_state[j, obst_idx] = obst_state

        # restart _sim for env changes
        if env_cfg_changed:
            self.stop_sim()
            self.start_sim()

        self._gym.set_actor_root_state_tensor(
            self._sim, gymtorch.unwrap_tensor(self._root_state)
        )

    def update_root_state_tensor_by_obstacles_tensor(self, obst_tensor):
        for o_tensor in obst_tensor:
            obst_idx = [
                idx for idx, actor in enumerate(self.env_cfg) if (actor.type != 'robot' and not actor.fixed)
            ][0]

            self.root_state[:, obst_idx] = o_tensor.repeat(self.num_envs, 1)

        self._gym.set_actor_root_state_tensor(
            self._sim, gymtorch.unwrap_tensor(self._root_state)
        )

    def draw_lines(self, lines, env_idx=0):
        # convert list of vertices into line segments
        line_segments = (
            torch.concat((lines[:-1], lines[1:]), axis=-1)
            .flatten(end_dim=-2)
            .cpu()
            .numpy()
            .astype(np.float32)
        )
        num_lines = line_segments.shape[0]
        colors = np.zeros((num_lines, 3), dtype=np.float32)
        colors[:, 1] = 255
        self._gym.add_lines(
            self.viewer, self.envs[env_idx], num_lines, line_segments, colors
        )
