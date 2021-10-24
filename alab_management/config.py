import os
from types import MappingProxyType as FrozenDict
from typing import Any, Dict

import toml


def froze_config(config_: Dict[str, Any]) -> FrozenDict:
    """
    Convert the config dict to frozen config

    Args:
        config_: the dict of config data

    Returns:
        frozen_config, which is not allowed to modify
    """

    def _froze_collection(collection_or_element):
        """
        Convert a list to tuple, a dict to frozen_dict recursively
        """
        if isinstance(collection_or_element, list):
            return tuple(_froze_collection(element) for element in collection_or_element)
        elif isinstance(collection_or_element, dict):
            return FrozenDict({k: _froze_collection(v) for k, v in collection_or_element.items()})
        else:
            return collection_or_element

    return _froze_collection(config_)


class AlabConfig:
    """
    Class used for storing all the config data
    """

    def __init__(self):
        """
        Load a immutable toml config file from `config_path`
        """
        config_path = os.getenv("ALAB_CONFIG", None)

        if config_path is None:
            config_path = "config.toml"
        _config = toml.load(open(config_path, "r", encoding="utf-8"))
        self._config = froze_config(_config)

    def __getitem__(self, item):
        return self._config.__getitem__(item)

    def __str__(self):
        return self._config.__repr__()

    __repr__ = __str__

    def __hash__(self):
        return self._config.__hash__()


config = AlabConfig()
