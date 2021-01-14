"""Helper classes and functions for handling configurations from multiple
sources.
"""

import errno
import os

from commentjson import load as load_jsonc
from importlib import import_module
from json import load as load_json
from logging import Logger
from typing import Any, Callable, Dict, Iterable, Optional, Union

__alL__ = ("AppConfigurator", "Configuration")

#: Type specification for configuration objects
Configuration = Dict[str, Any]


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


class AppConfigurator:
    """Helper object that manages loading the configuration of the app from
    various sources.
    """

    def __init__(
        self,
        config: Optional[Configuration] = None,
        *,
        default_filename: Optional[Union[str, Iterable[str]]] = None,
        environment_variable: Optional[str] = None,
        log: Optional[Logger] = None,
        package_name: str = None
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
        """
        self._config = config if config is not None else {}
        if default_filename is None:
            self._default_filenames = ()
        elif isinstance(default_filename, str):
            self._default_filenames = (default_filename, )
        else:
            self._default_filenames = tuple(default_filename)
        self._environment_variable = environment_variable
        self._key_filter = _always_true
        self._merge_keys = _always_false
        self._log = log
        self._package_name = package_name

    def configure(self, filename: Optional[str] = None) -> Configuration:
        """Configures the application.

        Parameters:
            filename: name of the configuration file to load, passed from the
                command line

        Returns:
            bool: whether the configuration sources were processed successfully
        """
        return self._load_configuration(filename)

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
    def key_filter(self, value) -> None:
        self._key_filter = value or _always_true

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
    def merge_keys(self, value) -> None:
        if not value:
            self._merge_keys = _always_false
        elif callable(value):
            self._merge_keys = value
        else:
            self._merge_keys = set(value).__contains__

    @property
    def result(self) -> Configuration:
        """Returns the result of the configuration process."""
        return self._config

    def _load_base_configuration(self) -> None:
        """Loads the default configuration of the application from the
        `flockctrl.server.config` module.
        """
        if not self._package_name:
            config = None
        else:
            try:
                config = import_module(".config", self._package_name)
            except ModuleNotFoundError:
                config = None

        if config:
            self._load_configuration_from_object(config)

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
            bool: whether all configuration files were processed successfully
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
        original, filename = filename, os.path.abspath(filename)

        config = {}

        exists = True
        try:
            with open(filename, mode="rb") as config_file:
                if filename.endswith(".json"):
                    config = load_json(config_file)
                elif filename.endswith(".cjson") or filename.endswith(".jsonc"):
                    config = load_jsonc(config_file)
                else:
                    exec(compile(config_file.read(), filename, "exec"), config)
        except IOError as e:
            if e.errno in (errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                exists = False
            else:
                raise

        self._load_configuration_from_dict(config)

        if not exists and mandatory:
            if self._log:
                self._log.warn("Cannot load configuration from {0!r}".format(original))
            return False
        elif exists:
            if self._log:
                self._log.info(
                    "Loaded configuration from {0!r}".format(original),
                    extra={"semantics": "success"},
                )

        return True

    def _load_configuration_from_dict(self, config: Dict[str, Any]) -> None:
        """Loads configuration settings from the given Python dictionary.

        Parameters:
            config: the configuration dict to load.
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
