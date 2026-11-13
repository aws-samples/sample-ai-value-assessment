"""The one record type every source adapter produces and the store accepts."""

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
    cost_priced: bool = False
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
        self.cost_priced = bool(self.cost_priced)
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
