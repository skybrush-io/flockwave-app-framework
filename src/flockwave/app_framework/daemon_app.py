from __future__ import annotations

from functools import partial
from logging import Logger
from trio import Nursery
from typing import Awaitable, Callable, Optional, Union, TYPE_CHECKING, TypeVar

from .base import AsyncApp
from .errors import ApplicationExit
from .sd_notify import Notifier

if TYPE_CHECKING:
    from flockwave.connections import (
        Connection,
        ConnectionSupervisor,
        SupervisionPolicy,
    )
    from flockwave.ext.manager import ExtensionAPIProxy, ExtensionManager

__all__ = ("DaemonApp",)


C = TypeVar("C", bound="Connection")


class DaemonApp(AsyncApp):
    """Base class for daemon apps that are designed to run several asynchronous
    tasks for an extended period of time and that must stop gracefully when
    Ctrl-C is pressed.

    Attributes:
        config: dictionary holding the configuration options of the application
        debug: whether the app is in debug mode
        connection_supervisor: object that manages the set of connections that
            the daemon needs to maintain to other processes, and that tries to
            reopen these connections if they get closed due to IO errors
        extension_manager: object that manages the loading and unloading of
            extension modules for the daemon
    """

    connection_supervisor: Optional["ConnectionSupervisor"]
    extension_manager: "ExtensionManager"

    def __init__(
        self,
        name: str,
        package_name: str,
        *,
        full_name: Optional[str] = None,
        log: Optional[Union[str, Logger]] = None,
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
            full_name: longer, human-readable name of the application, which
                may also contain spaces. Falls back to the short app name if
                not specified.
            log: name of the logger to use by the app; defaults to the
                application name. You may also pass a Logger instance here
        """
        self.connection_supervisor = None
        self.extension_manager = None  # type: ignore
        super().__init__(name, package_name, full_name=full_name, log=log)

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
        from flockwave.ext.errors import ApplicationExit as ExitRequestedFromExtension

        try:
            await nursery.start(
                partial(
                    self.extension_manager.run,
                    configuration=self.config.get("EXTENSIONS", {}),
                    app=self,
                )
            )  # type: ignore
        except ExitRequestedFromExtension as ex:
            raise ApplicationExit(
                str(ex) or "Application exit requested from extension"
            ) from None
        except Exception as ex:
            raise ApplicationExit(
                str(ex)
                or "Application exiting due to an unexpected error from the extension manager"
            ) from None

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
            a proxy object to the API of the extension that forwards attribute
            retrievals to the API, except for the property named ``loaded``,
            which returns whether the extension is loaded or not.

        Raises:
            KeyError: if the extension with the given name does not exist
        """
        return self.extension_manager.import_api(extension_name)

    async def supervise(
        self,
        connection: C,
        *,
        task: Optional[Callable[[C], Awaitable[None]]] = None,
        policy: Optional["SupervisionPolicy"] = None,
    ) -> None:
        """Shorthand to `self.connection_supervisor.supervise()`. See the
        details there.
        """
        if not self.connection_supervisor:
            raise RuntimeError(
                "You need to install flockwave-conn to use the connection supervisor"
            )

        await self.connection_supervisor.supervise(connection, task=task, policy=policy)

    async def ready(self) -> None:
        await super().ready()
        await Notifier.get_instance().send_ready_signal()

    async def teardown(self) -> None:
        """Called when the application is about to shut down. Calls all
        registered shutdown hooks and performs additional cleanup if needed.
        """
        await Notifier.get_instance().send_stopping_signal()
        await super().teardown()
        await self.extension_manager.teardown()
