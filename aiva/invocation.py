"""The data model every source adapter produces and the store accepts: an
Invocation per model call, plus Attachments for context reported apart from it."""

from dataclasses import dataclass, field


@dataclass
class Invocation:
    """One model call. Types are coerced here, once, so a null or string
    token count from any source cannot reach pricing, the store or reports."""

    timestamp: str = ""
    model: str = "unknown"
    caller: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: float = 0.0
    system_prompt: str = ""
    tools: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    response_text: str = ""
    max_tokens: int | None = None
    user_agent: str = ""
    operation: str = "unknown"
    request_id: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            setattr(self, name, as_int(getattr(self, name)))
        self.max_tokens = as_int(self.max_tokens) or None
        self.estimated_cost_usd = float(self.estimated_cost_usd or 0.0)
        for name in ("timestamp", "system_prompt", "response_text", "user_agent", "request_id"):
            value = getattr(self, name)
            setattr(self, name, value if isinstance(value, str) else "")
        if not isinstance(self.tools, list):
            self.tools = []
        if not isinstance(self.messages, list):
            self.messages = []
        if not isinstance(self.metadata, dict):
            self.metadata = {}


def as_int(value):
    """int(value), or 0 for None and anything unparseable."""
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


@dataclass
class Attachment:
    """Context a source reports apart from the model call: what the user
    asked, what came back, or which tool ran, for one prompt. Never counted
    or priced. aiva.attachments puts it on the calls with the same prompt_id
    (the Invocation's metadata["prompt_id"]) when a session is loaded."""

    prompt_id: str
    kind: str  # "prompt", "response" or "tool"
    text: str = ""
    request_id: str = ""  # set on responses, to match the exact call
    timestamp: str = ""
