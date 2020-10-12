from functools import partial
from importlib import import_module
from logging import getLogger
from trio import CancelScope, MultiError, Nursery, open_nursery
from typing import Optional

from .configurator import AppConfigurator, Configuration

MYPY = False

if MYPY:
    from flockwave.connection import (
        ConnectionSupervisor,
        ConnectionTask,
        SupervisionPolicy,
    )
    from flockwave.ext.manager import ExtensionAPIProxy, ExtensionManager

__all__ = ("DaemonApp",)


class DaemonApp:
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

    def __init__(self, name: str, package_name: str):
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
        """
        if " " in name:
            raise ValueError("App name may not contain spaces")

        self._app_name = name
        self._package_name = package_name
        self._prepared = False

        self.config = {}  # type: Configuration
        self.debug = False
        self.connection_supervisor = None  # type: Optional[ConnectionSupervisor]
        self.extension_manager = None  # type: Optional[ExtensionManager]

        # Placeholder for a nursery that parents all tasks in the daemon.
        # This will be set to a real nursery when the server starts.
        self._nursery = None  # type: Optional[Nursery]
        self._pending_tasks = []  # type: list

        self._create_basic_components()
        self._create_components()

    def _create_basic_components(self) -> None:
        """Creates the most basic components of the application such as the
        extension manager and the connection supervisor.

        This function is called by the constructor once at construction time.
        You should not need to call it later.

        Typically, you should not override this function; override
        `_create_components()` instead. If you do override this function, make
        sure to call the superclass implementation.

        The configuration of the server is not loaded yet when this function is
        executed. Avoid querying the configuration of the server here because
        the settings will not be up-to-date yet. Use `prepare()` for any
        preparations that depend on the configuration.
        """
        try:
            from flockwave.connections import ConnectionSupervisor
        except ImportError:
            ConnectionSupervisor = None

        from flockwave.ext.manager import ExtensionManager

        self.log = getLogger(self._app_name)
        self.extension_manager = ExtensionManager(self._package_name + ".ext")
        if ConnectionSupervisor:
            self.connection_supervisor = ConnectionSupervisor()

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

    def prepare(
        self, config: Optional[str] = None, debug: bool = False
    ) -> Optional[int]:
        """Hook function that contains preparation steps that should be
        performed by the daemon before it starts running its background tasks.

        Parameters:
            config: name of the configuration file to load
            debug: whether to force the app into debug mode

        Returns:
            error code to terminate the app with if the preparation was not
            successful; ``None`` if the preparation was successful
        """
        configurator = AppConfigurator(
            self.config,
            environment_variable=(self._app_name.upper() + "_SETTINGS"),
            default_filename=(self._app_name + ".cfg"),
            log=self.log,
            package_name=self._package_name,
        )
        if not configurator.configure(config):
            return 1

        if debug or self.config.get("DEBUG"):
            self.debug = True

        result = self._process_configuration(self.config)
        self._prepared = True
        return result

    def request_shutdown(self) -> None:
        """Requests tha application to shut down in a clean way.

        Has no effect if the main nursery of the app is not running.
        """
        if self._nursery:
            self._nursery.cancel_scope.cancel()

    def run_in_background(
        self, func, *args, cancellable: bool = False, protected: bool = False
    ) -> Optional[CancelScope]:
        """Runs the given function as a background task in the application.

        Parameters:
            cancellable: whether the task is cancellable
            protected: whether the task should be executed in protected mode,
                i.e. in a way that prevents the main nursery of the application
                from being cancelled if the task dies. When this is `True`,
                unexpected exceptions from the task will be logged and then
                swallowed

        Returns:
            an optional cancel scope that can be used to cancel the background
            task if it is cancellable
        """
        scope = CancelScope() if cancellable or hasattr(func, "_cancellable") else None
        if scope is not None:
            func = partial(func, cancel_scope=scope)

        if protected:
            func = partial(self._run_protected, func)

        if self._nursery:
            self._nursery.start_soon(func, *args)
        else:
            self._pending_tasks.append(partial(func, *args))

        return scope

    async def run(self) -> None:
        """Runs the daemon application."""

        if not self._prepared:
            self.prepare()

        # Helper function to ignore KeyboardInterrupt exceptions even if
        # they are wrapped in a Trio MultiError
        def ignore_keyboard_interrupt(exc):
            return None if isinstance(exc, KeyboardInterrupt) else exc

        try:
            with MultiError.catch(ignore_keyboard_interrupt):
                async with open_nursery() as nursery:
                    self._nursery = nursery

                    await nursery.start(
                        partial(
                            self.extension_manager.run,
                            configuration=self.config.get("EXTENSIONS", {}),
                            app=self,
                        )
                    )

                    if self.connection_supervisor:
                        nursery.start_soon(self.connection_supervisor.run)

                    tasks = self._pending_tasks[:]
                    del self._pending_tasks[:]

                    for task in tasks:
                        nursery.start_soon(task)

        finally:
            self._nursery = None
            await self.teardown()

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
        await self.extension_manager.teardown()

    def _create_components(self) -> None:
        """Creates the components of the application.

        This function is called by the constructor once at construction time.
        You should not need to call it later.

        The default implementation of this function does nothing; you can safely
        override it in derived classes without calling the superclass implementation.

        The configuration of the server is not loaded yet when this function is
        executed. Avoid querying the configuration of the server here because
        the settings will not be up-to-date yet. Use `_process_configuration()`
        for any preparations that depend on the configuration.
        """
        pass

    def _process_configuration(self, config: Configuration) -> Optional[int]:
        """Processes the configuration of the application after it was
        configured.

        The default implementation of this function does nothing; you can safely
        override it in derived classes without calling the superclass implementation.

        Returns:
            error code to terminate the app with if there was an error while
            processing the configuration, or ``None`` if the processing was
            successful
        """
        pass

    async def _run_protected(self, func, *args) -> None:
        """Runs the given function in a "protected" mode that prevents exceptions
        emitted from it to crash the nursery that the function is being executed
        in.
        """
        try:
            return await func(*args)
        except Exception:
            self.log.exception(
                f"Unexpected exception caught from background task {func.__name__}"
            )

    @property
    def version(self) -> str:
        """Returns the version number of the application.

        The version number is imported from the `.version` module of the
        application package. Returns `0.0.0` if there is no such module.
        """
        if self._version is None:
            try:
                version_module = import_module(".version", self._package_name)
            except ImportError:
                version_module = None
            self._version = str(getattr(version_module, "__version__", "0.0.0"))
        return self._version
