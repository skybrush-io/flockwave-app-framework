__all__ = ("ApplicationExit",)


class ApplicationExit(RuntimeError):
    """Exception that can be thrown during the application startup to
    request the application to terminate gracefully.
    """

    exit_code: int
    """Proposed exit code for the application."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)
