from typing import List, Optional, Sequence, Tuple, TYPE_CHECKING

from .base import AsyncApp

if TYPE_CHECKING:
    from urwid import MainLoop, Widget
    from urwid.main_loop import EventLoop
    from urwid_uikit.menus import MenuOverlay, MenuItemSpecification


__all__ = ("Palette", "TerminalApp")

Palette = List[Tuple[str, str, str]]
"""Typing specification for urwid color palettes"""


class TerminalApp(AsyncApp):
    """Base class for apps that are designed to provide a terminal-based used
    interface.

    Attributes:
        debug: whether the app is in debug mode
    """

    _menu_overlay: Optional["MenuOverlay"]
    _root_widget: Optional["Widget"]
    _ui_event_loop: Optional["EventLoop"]
    _ui_main_loop: Optional["MainLoop"]

    def __init__(self, *args, **kwds):
        self._menu_overlay = None
        self._root_widget = None
        self._ui_main_loop = None
        self._ui_event_loop = None
        super().__init__(*args, **kwds)

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
        self.run_in_background(self._run_ui)

    def _create_palette(self) -> Palette:
        """Creates the main palette that the app uses.

        The palette is a mapping from "semantic" color names to their actual
        foreground-background color combinations.

        The default implementation of this method returns a sensible palette
        with commonly used semantic color names. Typically, you should override
        this function, call the superclass implementation and then extend the
        returned list with your own semantic color names.
        """
        from urwid_uikit.app import Application

        return list(Application.palette)

    def _create_ui_event_loop(self) -> "EventLoop":
        """Creates a new instance of the UI event loop that the app uses.

        Normally you should not need to override this function. You should not
        call it manually either; `get_ui_event_loop()` will call it once when
        the UI event loop is about to be constructed.
        """
        from urwid import TrioEventLoop

        return TrioEventLoop()

    def _create_ui_main_loop(self) -> "MainLoop":
        """Creates a new instance of the UI main loop that the app uses.

        Normally you should not need to override this function. You should not
        call it manually either; `_create_basic_components()` will call it once
        when the UI main loop is about to be constructed.
        """
        from urwid import MainLoop
        from urwid_uikit.menus import MenuOverlay

        self._root_widget = self.create_root_widget()
        self._menu_overlay = MenuOverlay(self._root_widget)

        kwds = {
            "palette": self._create_palette(),
            "event_loop": self.get_ui_event_loop(),
            "unhandled_input": self.on_input,
        }

        self._update_ui_main_loop_kwargs(kwds)

        return MainLoop(self._menu_overlay, **kwds)

    async def _run_ui(self) -> None:
        """Async task that keeps the main UI running."""
        self._ui_main_loop = self._create_ui_main_loop()
        with self._ui_main_loop.start():
            await self._ui_event_loop.run_async()  # type: ignore
        if self._nursery:
            self._nursery.cancel_scope.cancel()

    def _update_ui_main_loop_kwargs(self, kwds) -> None:
        """Hook function that allows the user to modify the keyword arguments
        passed to `urwid.MainLoop()` when it is constructed.

        This is a highly advanced function; typically you do not need to
        override this unless you want to do extra fancy stuff like rendering
        the app on a different TTY where it is invoked from.

        Parameters:
            kwds: the keyword arguments passed to `urwid.MainLoop()`; it must be
                modified in-place
        """
        pass

    def create_root_widget(self) -> "Widget":
        """Creates the top-level UI widget that the application will show.

        Typically you need to override this method in your application.
        """
        from urwid import Filler, Text

        return Filler(
            Text("Override the create_top_level_widget() method first."), "top"
        )

    def get_ui_event_loop(self) -> "EventLoop":
        """Returns the UI event loop that the app uses.

        The UI event loop originates from the underlying `urwid` library and
        it ties itself into the main async event loop of the app.
        """
        if self._ui_event_loop is None:
            self._ui_event_loop = self._create_ui_event_loop()
            if self._ui_event_loop is None:
                raise RuntimeError(
                    "_create_ui_event_loop() did not create an event loop"
                )
        return self._ui_event_loop

    def invoke_menu(self) -> bool:
        """Invokes the main menu of the application.

        Returns:
            whether the main menu was shown. If the application has no attribute
            named `on_menu_invoked()`, returns ``False`` as there is no main
            menu associated to the application.
        """
        assert self._menu_overlay

        items = self.on_menu_invoked()
        if items is not None:
            self._menu_overlay.open_menu(items, title="Main menu")
            return True
        else:
            return False

    def on_input(self, input) -> None:
        """Callback method that is called by ``urwid`` for unhandled
        keyboard input.

        The default implementation treats ``q`` and ``Q`` as a command to quit
        the main application so it terminates the main loop. ``Esc`` will open
        the main menu of the application if it has one, otherwise it will also
        quit the main application.
        """
        if input in ("q", "Q"):
            self.quit()
        elif input == "esc":
            self.invoke_menu() or self.quit()

    def on_menu_invoked(self) -> Optional[Sequence["MenuItemSpecification"]]:
        """Method that must be overridden if the terminal app wishes to provide
        a menu that is triggered with the `invoke_menu()` function.

        Returns:
            a sequence of menu item specifications (as accepted by the `open_menu()`
            function of MenuOverlay_), or ``None`` if no menu needs to be provided.
        """
        pass

    @property
    def root_widget(self) -> Optional["Widget"]:
        """Returns the top-level widget of the app."""
        return self._root_widget

    def quit(self) -> None:
        """Instructs the UI main loop to terminate.

        Override this method to add confirmation before quitting. The default
        implementation simply raises an ExitMainLoop_ exception that triggers
        the main UI loop to stop.
        """
        from urwid import ExitMainLoop

        raise ExitMainLoop
