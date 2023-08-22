from __future__ import annotations

import platform
import sys

from typing import ClassVar, Optional, TYPE_CHECKING

from .base import SyncApp

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace

__all__ = ("ASGIWebServerApp",)


class ASGIWebServerApp(SyncApp):
    """Base class for daemon apps that are designed to run a Python ASGI web
    application with a production-grade ASGI web server.

    Due to how ASGI servers work, this class must be derived from SyncApp_
    because it will be the ASGI server that enters the async world.
    """

    allow_public: ClassVar[bool] = True
    """Specifies whether public connections are allowed to this app. Override
    it in derived classes if you want to prevent public connections and you
    want to force the app to listen on localhost only.
    """

    _args: Optional[Namespace] = None
    """The parsed and post-processed command line arguments of the app;
    ``None`` if they have not been parsed yet.
    """

    _parser: ArgumentParser
    """Parser for the command line arguments of the app."""

    def _create_basic_components(self) -> None:
        self._parser = self._create_argument_parser()

    def _create_argument_parser(self) -> ArgumentParser:
        """Creates the command-line argument parser of the application.

        You may override this method in subclasses to customize the parser if
        needed. Make sure to call the superclass implementation if you do so.
        """
        from argparse import ArgumentParser

        parser = ArgumentParser(prog=self.app_name, description=self.app_full_name)

        parser.add_argument(
            "--version", action="version", version=f"%(prog)s version {self.version}"
        )
        parser.add_argument(
            "-p",
            "--port",
            metavar="PORT",
            help="port that the server should be listening on",
            default=8000,
            type=int,
        )

        if self.allow_public:
            parser.add_argument(
                "--host",
                metavar="HOST",
                help=(
                    "IP address of the host that the server should be listening "
                    "on; use 0.0.0.0 for public servers"
                ),
                default="127.0.0.1",
            )
            parser.add_argument(
                "--public",
                help=(
                    "listen on all IP addresses of the machine; takes precedence "
                    "over --host"
                ),
                action="store_true",
                default=False,
            )

        return parser

    def prepare(self, config: str | None = None, debug: bool = False) -> int | None:
        args = self._parser.parse_args()
        exit_code = self._process_arguments(args)
        if exit_code:
            return exit_code

        self._args = args

        return super().prepare(config, debug)

    def _process_arguments(self, args: Namespace) -> Optional[int]:
        """Post-processes the command line arguments parsed by the argument
        parser.

        Returns:
            a non-zero exit code if the app should be terminated, zero or
            ``None`` if the startup process may continue
        """
        if self.allow_public and args.public:
            args.host = "0.0.0.0"

    def print_banner(self) -> None:
        """Prints the banner of the application before starting the main task."""
        print(f"{self.app_full_name} version {self.version}")

    def ready(self) -> None:
        self.print_banner()
        print()

    def run_main(self) -> None:
        import uvicorn
        from uvicorn.config import LOGGING_CONFIG

        args = self._args
        assert args is not None

        host = getattr(args, "host", "127.0.0.1")

        frozen = bool(getattr(sys, "frozen", False))
        use_colors = not frozen or platform.system().lower() != "windows"

        uvicorn.run(
            f"{self._package_name}.app:app",
            host=host,
            port=args.port,
            reload=not frozen,
            log_config=LOGGING_CONFIG,
            use_colors=use_colors,
        )
