from dataclasses import dataclass, field
from mppi_torch.mppi import MPPIConfig
from mppiisaac.planner.isaacgym_wrapper import IsaacGymConfig, ActorWrapper
from hydra.core.config_store import ConfigStore

from typing import List, Optional


@dataclass
class ExampleConfig:
    render: bool
    n_steps: int
    mppi: MPPIConfig
    isaacgym: IsaacGymConfig
    goal: List[float]
    nx: int
    actors: List[str]
    initial_actor_positions: List[List[float]]


cs = ConfigStore.instance()
cs.store(name="config_point_robot", node=ExampleConfig)
cs.store(name="config_multi_point_robot", node=ExampleConfig)
cs.store(name="config_heijn_robot", node=ExampleConfig)
cs.store(name="config_boxer_robot", node=ExampleConfig)
cs.store(name="config_jackal_robot", node=ExampleConfig)
cs.store(name="config_multi_jackal", node=ExampleConfig)
cs.store(name="config_panda", node=ExampleConfig)
cs.store(name="config_omnipanda", node=ExampleConfig)
cs.store(name="config_panda_push", node=ExampleConfig)
cs.store(name="config_heijn_push", node=ExampleConfig)
cs.store(name="config_heijn_reach", node=ExampleConfig)
cs.store(name="config_boxer_push", node=ExampleConfig)
cs.store(name="config_boxer_reach", node=ExampleConfig)
cs.store(name="config_panda_c_space_goal", node=ExampleConfig)
cs.store(group="mppi", name="base_mppi", node=MPPIConfig)
cs.store(group="isaacgym", name="base_isaacgym", node=IsaacGymConfig)


from hydra import compose, initialize
from omegaconf import OmegaConf
def load_isaacgym_config(name):
    with initialize(config_path="../../conf"):
        cfg = compose(config_name=name)
        print(OmegaConf.to_yaml(cfg))
    return cfg