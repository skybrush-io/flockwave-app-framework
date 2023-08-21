"""Notifier class to communicate with a running ``systemd`` instance on
Linux from daemon apps.
"""

import os

from pathlib import Path
from typing import Awaitable, Callable, IO, Optional, Union

from trio import wrap_file
from trio.socket import socket, SOCK_DGRAM

__all__ = ("Notifier",)


Writer = Callable[[bytes], Awaitable[None]]
"""Type specification for writer functions that can write something to a
stream.
"""

_instance: Optional["Notifier"] = None
"""The default, global notifier instance."""


async def _dummy_writer(data: bytes) -> None:
    """Dummy writer function for platforms that do not support ``systemd``."""
    pass


class Notifier:
    """Notifier class to communicate with a running ``systemd`` instance on
    Linux from daemon apps.
    """

    _writer: Writer

    @classmethod
    def from_unix_domain_socket(cls, path: Union[Path, str]):
        """Constructs a notifier instance that will talk to the given UNIX
        domain socket.
        """
        try:
            from trio.socket import AF_UNIX
        except ImportError:
            raise RuntimeError(
                "UNIX domain sockets are not supported on this platform"
            ) from None

        sock = socket(family=AF_UNIX, type=SOCK_DGRAM)
        path = str(path)

        async def writer(data: bytes) -> None:
            await sock.sendto(data, path)

        return cls(writer)

    @classmethod
    def for_binary_stream(cls, fp: IO[bytes]):
        """Constructs a notifier instance that writes to the given binary stream."""
        async_fp = wrap_file(fp)
        return cls(async_fp.write)  # type: ignore

    @classmethod
    def for_text_stream(cls, fp: IO[str], *, encoding: str = "utf-8"):
        """Constructs a notifier instance that writes to the given text stream."""
        async_fp = wrap_file(fp)

        async def writer(data: bytes) -> None:
            await async_fp.write(data.decode(encoding))

        return cls(writer)

    @classmethod
    def get_instance(cls):
        """Returns a default notifier instance that attempts to talk to
        ``systemd`` via the UNIX domain socket provided by the ``NOTIFY_SOCKET``
        envvar.
        """
        global _instance

        if _instance is None:
            addr = os.getenv("NOTIFY_SOCKET")
            if addr:
                _instance = Notifier.from_unix_domain_socket(addr)
            else:
                _instance = Notifier(_dummy_writer)

        assert _instance is not None
        return _instance

    def __init__(self, writer_or_address: Writer):
        """Constructor.

        Parameters:
            writer_or_address: a function that can be called with the message to
                be sent to systemd, or the name of a UNIX domain socket where
                systemd is listening.
        """
        self._writer = writer_or_address
        # self.address = addr or os.getenv("NOTIFY_SOCKET")

    async def send_ready_signal(self) -> None:
        """Report to systemd that the service is now ready to be uesd."""
        await self._send("READY=1\n")

    async def send_reloading_signal(self) -> None:
        """Report to systemd that the service is being reloaded."""
        await self._send("RELOADING=1\n")

    async def send_status(self, msg: str) -> None:
        """Send a message to systemd that can serve as a status message for
        this service.
        """
        if "\n" in msg:
            raise RuntimeError("message may not contain a newline character")
        await self._send(f"STATUS={msg}\n")

    async def send_stopping_signal(self) -> None:
        """Report to systemd that the service is being stopped."""
        await self._send("STOPPING=1\n")

    async def reset_watchdog(self):
        """Resets the systemd watchdog of the service"""
        await self._send("WATCHDOG=1\n")

    async def _send(self, msg: Union[str, bytes]):
        await self._writer(msg.encode() if isinstance(msg, str) else msg)
