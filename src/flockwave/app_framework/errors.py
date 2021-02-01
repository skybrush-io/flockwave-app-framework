__all__ = ("ApplicationExit",)


class ApplicationExit(RuntimeError):
    """Exception that can be thrown during the application startup to
    request the application to terminate gracefully.
    """

    pass
