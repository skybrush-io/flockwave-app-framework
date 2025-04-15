"""Helper classes and functions for handling configurations from multiple
sources.
"""

import errno
import os

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from inspect import ismodule
from json import load as load_json
from json5 import load as load_json5, loads as load_json5_from_string
from logging import Logger
from re import sub
from types import ModuleType
from typing import Any, Callable, Dict, IO, Iterable, List, Optional, Tuple, Union

try:
    from tomllib import load as load_toml
except ImportError:
    from tomli import load as load_toml  # type: ignore


__alL__ = ("AppConfigurator", "Configuration")

Configuration = Dict[str, Any]
"""Type specification for configuration objects"""


def _always_false(*args, **kwds) -> bool:
    """Dummy function that returns `False` for any input."""
    return False


def _always_true(*args, **kwds) -> bool:
    """Dummy function that returns `True` for any input."""
    return True


def _merge_dicts(source, into) -> None:
    """Merges a source dictionary into a target dictionary recursively."""
    for key, value in source.items():
        if isinstance(value, dict):
            existing_value = into.get(key)
            if isinstance(existing_value, dict):
                _merge_dicts(value, into=existing_value)
            else:
                into[key] = value
        else:
            into[key] = value


def _prune_dict(first, second) -> None:
    """Compares two dictionaries recursively and removes those items from the
    first one that are identical to the items with the same key from the second
    dictionary.

    After pruning, merging the pruned dictionary into `second` should yield a
    dictionary that is identical to `first`.
    """
    to_delete: List[str] = []

    for key, value in first.items():
        if key in second:
            second_value = second[key]
            if isinstance(value, dict) and isinstance(second_value, dict):
                _prune_dict(value, second_value)
                if not value:
                    to_delete.append(key)
            elif value == second_value:
                to_delete.append(key)

    for key in to_delete:
        del first[key]


def load_json5_with_hash_mark_styled_comments(
    input: IO[bytes], *, encoding: str = "utf-8"
) -> Any:
    """Helper function to load JSON5 files while dealing with the hashmark-style
    comments that were commonly found in earlier versions of our configuration
    files.
    """
    lines = [sub(rb"^\s*#.*", b"", line) for line in input]  # type: ignore
    return load_json5_from_string(b"".join(lines).decode(encoding))


class ConfigurationFormat(Enum):
    """Enum representing the configuration file formats that we support."""

    JSON = "json"
    JSONC = "jsonc"
    JSON5 = "json5"
    PYTHON = "python"
    TOML = "toml"


@dataclass(frozen=True)
class LoadedConfigurationFile:
    """Simple data class that encapsulates the name of a loaded configuration
    file and the format it was stored in.
    """

    name: str
    """Name of the file that was loaded."""

    format: ConfigurationFormat
    """Format of the file that was loaded."""

    pre_snapshot: Configuration
    """A snapshot of the configuration that was in effect _before_ the file
    was loaded. This can be used to derive the changes that loading the
    file has made to the configuration using `jsondiff`.
    """


class AppConfigurator:
    """Helper object that manages loading the configuration of the app from
    various sources.
    """

    _config: Configuration
    _default_filenames: Tuple[str, ...]
    _environment_variable: Optional[str]
    _key_filter: Callable[[str], bool]
    _loaded_files: List[LoadedConfigurationFile]
    _merge_keys: Callable[[str], bool]
    _log: Optional[Logger]
    _package_name: Optional[str]
    _safe: bool

    def __init__(
        self,
        config: Optional[Configuration] = None,
        *,
        default_filename: Optional[Union[str, Iterable[str]]] = None,
        environment_variable: Optional[str] = None,
        log: Optional[Logger] = None,
        package_name: Optional[str] = None,
        safe: bool = False,
    ):
        """Constructor.

        Parameters:
            config: the configuration object that the configurator will
                populate. May contain default values.
            default_filename: name of the default configuration file that the
                configurator will look for in the current working directory.
                Multiple filenames may also be given in a list or tuple; the
                first configuration file found will be used as the default.
            environment_variable: name of the environment variable in which
                the configurator will look for the name of an additional
                configuration file to load
            package_name: name of the package to import the base configuration
                of the app from
            safe: specifies whether the configurator should run in safe mode.
                In safe mode, the parsers used to parse configuration files are
                forced to run in safe mode that aims to prevent arbitrary code
                execution and freezing on maliciously crafted input. It is
                highly advised to set this parameter to `True`.
        """
        self._config = config if config is not None else {}
        if default_filename is None:
            self._default_filenames = ()
        elif isinstance(default_filename, str):
            self._default_filenames = (default_filename,)
        else:
            self._default_filenames = tuple(default_filename)
        self._environment_variable = environment_variable
        self._key_filter = _always_true
        self._merge_keys = _always_false
        self._loaded_files = []
        self._log = log
        self._package_name = package_name
        self._safe = bool(safe)

    def configure(self, filename: Optional[str] = None) -> bool:
        """Configures the application.

        Parameters:
            filename: name of the configuration file to load, passed from the
                command line

        Returns:
            whether the configuration sources were processed successfully
        """
        return self._load_configuration(filename)

    def minimize_configuration(
        self, config: Configuration, defaults: Configuration
    ) -> None:
        """Returns a minimal representation of the given configuration object
        by comparing it to a set of defaults and omitting keys whose values are
        identical to their defaults.

        The configuration object in the first argument will be modified
        _in place_! Make sure to create a deep copy first if you do not own
        the configuration object. The second argument will not be modified.
        """
        for top_level_key in list(config.keys()):
            if not self.key_filter(top_level_key):
                # This top-level key may not appear in the configuration so
                # remove it
                del config[top_level_key]
                continue

            if top_level_key not in defaults:
                # This top-level key does not appear in the defaults so it must
                # be kept
                continue

            top_level_default_value = defaults[top_level_key]
            top_level_value = config[top_level_key]

            if not self.merge_keys(top_level_key):
                # This top-level key is replaced when the user provides a new
                # value so just compare the values
                if top_level_default_value == top_level_value:
                    del config[top_level_key]
            else:
                # This top-level key is merged with any new values from the
                # configuration files, so compare the subkeys recursively and
                # include only those branches where the configurations are different
                if isinstance(top_level_value, dict) and isinstance(
                    top_level_default_value, dict
                ):
                    _prune_dict(top_level_value, top_level_default_value)

    @property
    def key_filter(self) -> Callable[[str], bool]:
        """Key filter function that decides whether a top-level key from a
        configuration source should be considered (`True`) or ignored (`False`).
        You can use this, e.g., to force that only uppercase keys get merged
        into the configuration object from the individual sources.

        When setting this property, you may also use `None` to indicate
        "don't filter, just accept everything".
        """
        return self._key_filter

    @key_filter.setter
    def key_filter(self, value: Callable[[str], bool]) -> None:
        self._key_filter = value or _always_true

    @property
    def loaded_files(self) -> List[LoadedConfigurationFile]:
        """Returns the list of loaded configuration files."""
        return self._loaded_files

    @property
    def merge_keys(self) -> Callable[[str], bool]:
        """Function that decides whether a new value for top-level key from a
        configuration source should _overwrite_ the previous value already in
        the configuration (`False`) or should be merged into the previous value
        (`True`). The latter makes sense only if both the old and the new value
        of the property is a dictionary. Merging is done recursively.

        When setting this property, `None` and `False` means "never merge keys",
        `True` means "always merge keys if possible", and a list of keys means
        "these keys should be merged, everything else should be overwritten".
        """
        return self._merge_keys

    @merge_keys.setter
    def merge_keys(self, value: Any) -> None:
        if not value:
            self._merge_keys = _always_false
        elif callable(value):
            self._merge_keys = value  # type: ignore
        else:
            self._merge_keys = set(value).__contains__

    @property
    def result(self) -> Configuration:
        """Returns the result of the configuration process."""
        return self._config

    @property
    def safe(self) -> bool:
        """Returns whether the configurator object is in safe mode."""
        return self._safe

    @safe.setter
    def safe(self, value: bool) -> None:
        self._safe = bool(value)

    def _load_base_configuration(self) -> None:
        """Loads the default configuration of the application from the
        `.config` submodule of the associated package.

        The contents of the module will be deep-copied.
        """
        if not self._package_name:
            module = None
        else:
            try:
                module = import_module(".config", self._package_name)
            except ModuleNotFoundError:
                module = None

        if module:
            self._load_configuration_from_module(module, deep_copy=True)

    def _load_configuration(self, config: Optional[str] = None) -> bool:
        """Loads the configuration of the application from the following
        sources, in the following order:

        - The default configuration in the `.config` module of the current
          package, if there is one.

        - The configuration file referred to by the `config` argument,
          if present. If it is `None` and one or more default configuration
          filenames were specified at construction time, the first default
          configuration file that exists will be used instead.

        - The configuration file referred to by the environment variable
          provided at construction time, if it is specified.

        Parameters:
            config: name of the configuration file to load

        Returns:
            whether all configuration files were processed successfully
        """
        self._load_base_configuration()

        config_files = []

        if config:
            config_files.append((config, True))
        else:
            for default_filename in self._default_filenames:
                try:
                    if os.path.isfile(default_filename):
                        config_files.append((default_filename, False))
                        break
                except Exception:
                    pass

        if self._environment_variable:
            config_files.append((os.environ.get(self._environment_variable), True))

        return all(
            self._load_configuration_from_file(config_file, mandatory)
            for config_file, mandatory in config_files
            if config_file
        )

    def _load_configuration_from_file(
        self, filename: str, mandatory: bool = True
    ) -> bool:
        """Loads configuration settings from the given file.

        Parameters:
            filename: name of the configuration file to load. Relative
                paths are resolved from the current directory.
            mandatory: whether the configuration file must exist.
                If this is ``False`` and the file does not exist, this
                function will not print a warning about the missing file
                and pretend that loading the file was successful.

        Returns:
            whether the configuration was loaded successfully
        """
        filename = os.path.abspath(filename)

        config = {}

        exists = True
        cfg_format: Optional[ConfigurationFormat] = None
        try:
            with open(filename, mode="rb") as config_file:
                if filename.endswith(".json"):
                    config = load_json(config_file)
                    cfg_format = ConfigurationFormat.JSON
                elif filename.endswith(".cjson") or filename.endswith(".jsonc"):
                    config = load_json5_with_hash_mark_styled_comments(config_file)
                    cfg_format = ConfigurationFormat.JSONC
                elif filename.endswith(".json5"):
                    config = load_json5(config_file)
                    cfg_format = ConfigurationFormat.JSON5
                elif filename.endswith(".toml"):
                    config = load_toml(config_file)
                    cfg_format = ConfigurationFormat.TOML
                elif not self._safe:
                    exec(compile(config_file.read(), filename, "exec"), config)
                    self._remove_python_builtins_from_config(config)
                    cfg_format = ConfigurationFormat.PYTHON
        except IOError as e:
            if e.errno in (errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                exists = False
            else:
                raise

        if exists and cfg_format is None:
            if self._log:
                message = f"Configuration file {filename!r} is in an unknown format"
                if mandatory:
                    self._log.error(message)
                else:
                    self._log.warn(message)
            return False

        snapshot = deepcopy(self._config)
        self._load_configuration_from_dict(config)

        if not exists and mandatory:
            if self._log:
                self._log.error(f"Cannot load configuration from {filename!r}")
            return False
        elif exists:
            if cfg_format is not None:
                self._record_loaded_configuration_file(
                    filename, cfg_format, pre_snapshot=snapshot
                )
            if self._log:
                self._log.info(
                    f"Loaded configuration from {filename!r}",
                    extra={"semantics": "success"},
                )

        return True

    def _load_configuration_from_dict(self, config: Dict[str, Any]) -> None:
        """Loads configuration settings from the given Python dictionary.

        Parameters:
            config: the configuration dict to load. Items in the dictionary are
                used as-is, _without_ depp-copying them.
        """
        for key, value in config.items():
            if self._key_filter(key):
                if isinstance(value, dict) and self._merge_keys(key):
                    existing_value = self._config.get(key)
                    if isinstance(existing_value, dict):
                        _merge_dicts(value, into=existing_value)
                    else:
                        self._config[key] = value
                else:
                    self._config[key] = value

    def _load_configuration_from_module(
        self, config: ModuleType, *, deep_copy: bool = False
    ) -> None:
        """Loads configuration settings from the given Python module.

        Parameters:
            config: the configuration object to load.
            deep_copy: whether to deep-copy the contents of the module.
                ``False`` will create a shallow copy (i.e. top-level keys of
                the module will be copied, values will be referenced).
                ``True`` will create a deep copy.
        """
        contents = {k: getattr(config, k) for k in dir(config)}
        self._remove_python_builtins_from_config(contents)

        if deep_copy:
            # Exclude modules from deep-copying
            contents = deepcopy({k: v for k, v in contents.items() if not ismodule(v)})

        return self._load_configuration_from_dict(contents)

    def _load_configuration_from_object(self, config: Any) -> None:
        """Loads configuration settings from the given Python object.

        Parameters:
            config: the configuration object to load.
        """
        for key in dir(config):
            if self._key_filter(key):
                value = getattr(config, key)
                if isinstance(value, dict) and self._merge_keys(key):
                    existing_value = self._config.get(key)
                    if isinstance(existing_value, dict):
                        _merge_dicts(value, into=existing_value)
                    else:
                        self._config[key] = value
                else:
                    self._config[key] = value

    def _record_loaded_configuration_file(
        self, name: str, cfg_format: ConfigurationFormat, *, pre_snapshot: Configuration
    ) -> None:
        self._loaded_files.append(
            LoadedConfigurationFile(
                name=name, format=cfg_format, pre_snapshot=pre_snapshot
            )
        )

    @staticmethod
    def _remove_python_builtins_from_config(config: Configuration) -> None:
        """Modifies the given configuration dictionary in-place and removes all
        top-level keys that look like Python builtins.
        """
        to_remove = []
        to_remove = [k for k in config.keys() if k.startswith("__")]
        for k in to_remove:
            del config[k]
