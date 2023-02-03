"""Base classes for implementing applications."""

from exceptiongroup import catch
from functools import partial
from importlib import import_module
from logging import getLogger, Logger
from trio import CancelScope, Nursery, open_nursery
from typing import Awaitable, Callable, List, Optional, Union

from .configurator import AppConfigurator, Configuration
from .errors import ApplicationExit

__all__ = ("AsyncApp",)


class AsyncApp:
    """Base class for apps that revolve around running several asynchronous
    tasks concurrently.

    Basically almost all of our apps except the most simple ones are based on
    this base class.
    """

    config: Configuration
    """Dictionary holding the configuration options of the application"""

    debug: bool
    """Whether the app is in debug mode"""

    log: Logger
    """The logger of the application"""

    _app_name: str
    _configurator: Optional[AppConfigurator]
    _nursery: Optional[Nursery]
    _package_name: str
    _pending_tasks: List[Callable[[], Awaitable[None]]]
    _prepared: bool

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
        if " " in name:
            raise ValueError("App name may not contain spaces")

        self._app_name = name
        self._configurator = None
        self._package_name = package_name
        self._prepared = False

        self.config = {}
        self.debug = False

        logger = log or self._app_name
        if hasattr(logger, "info"):
            self.log = logger  # type: ignore
        elif isinstance(logger, str):
            self.log = getLogger(logger)
        else:
            raise TypeError("Invalid logger type, expected str or Logger")

        # Placeholder for a nursery that parents all tasks in the app.
        # This will be set to a real nursery when the app starts.
        self._nursery = None
        self._pending_tasks = []

        self._create_basic_components()
        self._create_components()

    def _create_basic_components(self) -> None:
        """Creates the most basic components of the application.

        This function is called by the constructor once at construction time.
        You should not need to call it later.

        Typically, you should not override this function; override
        `_create_components()` instead. If you do override this function, make
        sure to call the superclass implementation.

        The configuration of the app is not loaded yet when this function is
        executed. Avoid querying the configuration of the app here because
        the settings will not be up-to-date yet. Use `prepare()` for any
        preparations that depend on the configuration.
        """
        pass

    def _setup_app_configurator(self, configurator: AppConfigurator) -> None:
        """Hook function that is called after the app constructs the
        AppConfigurator_ instance responsible for parsing configuration files,
        and that allows one to update some properties of the AppConfigurator_
        to tweak its behaviour.

        The default implementation does nothing; you can safely override this
        function in subclasses.
        """
        pass

    def prepare(
        self, config: Optional[str] = None, debug: bool = False
    ) -> Optional[int]:
        """Hook function that contains preparation steps that should be
        performed by the app before it starts running.

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
            default_filename=(
                self._app_name + ".cfg",
                self._app_name + ".jsonc",
                self._app_name + ".json",
            ),
            log=self.log,
            package_name=self._package_name,
        )
        self._setup_app_configurator(configurator)
        self._configurator = configurator
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
        """Runs the application."""

        if not self._prepared:
            self.prepare()

        # Helper function to ignore KeyboardInterrupt exceptions even if
        # they are wrapped in an exception group
        def ignore_keyboard_interrupt(exc) -> None:
            pass

        try:
            with catch({KeyboardInterrupt: ignore_keyboard_interrupt}):
                async with open_nursery() as nursery:
                    self._nursery = nursery

                    await self._on_nursery_created(nursery)

                    tasks = self._pending_tasks[:]
                    del self._pending_tasks[:]

                    for task in tasks:
                        nursery.start_soon(task)

        except ApplicationExit as ex:
            self.log.error(str(ex) or "Received request to stop application.")

        finally:
            self._nursery = None
            await self.teardown()

    async def teardown(self) -> None:
        """Called when the application is about to shut down.

        Make sure to call the superclass implementation if you override this
        method.
        """
        pass

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

    async def _on_nursery_created(self, nursery: Nursery) -> None:
        """Function that is called by the `run()` method when the task nursery
        was created and that allows us to launch certain important tasks in the
        nursery before we launch the rest.
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
    def configurator(self) -> AppConfigurator:
        """The configurator object of the application that was responsible for
        loading the configuration from various sources. Must be called only after
        the app was prepared with `prepare()`.

        Raises:
            RuntimeError: when called before the app was prepared
        """
        if self._configurator is None:
            raise RuntimeError("Application was not prepared yet")
        return self._configurator

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
