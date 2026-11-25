"""Safe, structured response failures shared by providers and feature validators."""


class OutputLimitError(ValueError):
    """A model answered but exhausted its response budget; this is not a provider outage."""

    def __init__(self, message="The AI response exceeded its output token limit", *, token_limit=0, response=None):
        super().__init__(message)
        self.token_limit = token_limit
        self.response = response


class InvalidResponseError(ValueError):
    """The model answered, but its assessment did not pass structural validation."""
