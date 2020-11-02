from functools import partial
from logging import Logger
from trio import Nursery
from typing import Optional, Union, TYPE_CHECKING

from .base import AsyncApp

if TYPE_CHECKING:
    from flockwave.connection import (
        ConnectionSupervisor,
        ConnectionTask,
        SupervisionPolicy,
    )
    from flockwave.ext.manager import ExtensionAPIProxy, ExtensionManager

__all__ = ("DaemonApp",)


class DaemonApp(AsyncApp):
    """Base class for daemon apps that are designed to run several asynchronous
    tasks for an extended period of time and that must stop gracefully when
    Ctrl-C is pressed.

    Attributes:
        config: dictionary holding the configuration options of the application
        connection_supervisor: object that manages the set of connections that
            the daemon needs to maintain to other processes, and that tries to
            reopen these connections if they get closed due to IO errors
        debug: whether the app is in debug mode
        extension_manager: object that manages the loading and unloading of
            extension modules for the daemon
    """

    def __init__(
        self, name: str, package_name: str, *, log: Optional[Union[str, Logger]] = None
    ):
        """Constructor.

        Parameters:
            name: short, lowercase, human-readable name of the daemon
                application, without spaces. Used to derive the name of the
                root logger, the default configuration file and the environment
                variable that holds the configuration filename override.
            package_name: name of the Python package that holds the code of the
                daemon app. The default configuration of the app is assumed to
                be in a Python module named `config` within this package.
                Extensions corresponding to the daemon app are looked up in the
                `ext` subpackage of this package.
            log: name of the logger to use by the app; defaults to the
                application name. You may also pass a Logger instance here
        """
        self.connection_supervisor = None  # type: Optional[ConnectionSupervisor]
        self.extension_manager = None  # type: Optional[ExtensionManager]
        super().__init__(name, package_name, log=log)

    def _create_basic_components(self) -> None:
        try:
            from flockwave.connections import ConnectionSupervisor
        except ImportError:
            ConnectionSupervisor = None

        from flockwave.ext.manager import ExtensionManager

        self.extension_manager = ExtensionManager(self._package_name + ".ext")
        if ConnectionSupervisor:
            self.connection_supervisor = ConnectionSupervisor()

    async def _on_nursery_created(self, nursery: Nursery) -> None:
        await nursery.start(
            partial(
                self.extension_manager.run,
                configuration=self.config.get("EXTENSIONS", {}),
                app=self,
            )
        )

        if self.connection_supervisor:
            nursery.start_soon(self.connection_supervisor.run)

    def import_api(self, extension_name: str) -> "ExtensionAPIProxy":
        """Imports the API exposed by an extension.

        Extensions *may* have a dictionary named ``exports`` that allows the
        extension to export some of its variables, functions or methods.
        Other extensions may access the exported members of an extension by
        calling the `import_api`_ method of the application.

        This function supports "lazy imports", i.e. one may import the API
        of an extension before loading the extension. When the extension
        is not loaded, the returned API object will have a single property
        named ``loaded`` that is set to ``False``. When the extension is
        loaded, the returned API object will set ``loaded`` to ``True``.
        Attribute retrievals on the returned API object are forwarded to the
        API of the extension.

        Parameters:
            extension_name: the name of the extension whose API is to
                be imported

        Returns:
            ExtensionAPIProxy: a proxy object to the API of the extension
                that forwards attribute retrievals to the API, except for
                the property named ``loaded``, which returns whether the
                extension is loaded or not.

        Raises:
            KeyError: if the extension with the given name does not exist
        """
        return self.extension_manager.import_api(extension_name)

    async def supervise(
        self,
        connection,
        *,
        task: Optional["ConnectionTask"] = None,
        policy: Optional["SupervisionPolicy"] = None,
    ):
        """Shorthand to `self.connection_supervisor.supervise()`. See the
        details there.
        """
        if not self.connection_supervisor:
            raise RuntimeError(
                "You need to install flockwave-conn to use the connection supervisor"
            )

        await self.connection_supervisor.supervise(connection, task=task, policy=policy)

    async def teardown(self):
        """Called when the application is about to shut down. Calls all
        registered shutdown hooks and performs additional cleanup if needed.
        """
        await super().teardown()
        await self.extension_manager.teardown()
