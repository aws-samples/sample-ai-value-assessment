# Per-1K-token on-demand prices (USD), sourced from the model catalog
# ($/1M / 1000). Keyed by the canonical model family token found in the
# model ID. Longest matching key wins so opus-4-8 never collides with a
# shorter opus prefix. Cache read/write are priced as ratios of the input
# rate (Bedrock/Anthropic 5-minute-TTL convention): read ~0.1x, write ~1.25x.
#
# NOTE: on-demand estimate only. Provisioned throughput, batch (-50%), and
# 1-hour cache TTL (2x write) are NOT modelled - see get_pricing.
PRICING_PER_1K = {
    "claude-opus-4-8": {"input": 0.005, "output": 0.025},
    "claude-opus-4-7": {"input": 0.005, "output": 0.025},
    "claude-opus-4-6": {"input": 0.005, "output": 0.025},
    "claude-opus-4-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005},
    "claude-haiku-3-5": {"input": 0.0008, "output": 0.004},
    "claude-haiku-3": {"input": 0.00025, "output": 0.00125},
    "nova-pro": {"input": 0.0008, "output": 0.0032},
    "nova-lite": {"input": 0.00006, "output": 0.00024},
    "nova-micro": {"input": 0.000035, "output": 0.00014},
    "titan-text-express": {"input": 0.0002, "output": 0.0006},
}

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# Fallback when a model ID matches no known family. Chosen to be obviously
# wrong-looking (0) so unpriced models surface as $0.00 rather than silently
# borrowing another tier's rate.
UNKNOWN_PRICING = {"input": 0.0, "output": 0.0}


def get_pricing(model_id):
    """Look up per-1K-token pricing by longest matching family token.

    Returns (pricing, priced) where priced is False when the model ID
    matched no known family (so callers can flag the cost as unreliable).
    Longest-match avoids opus-4-8 colliding with a shorter opus key.
    """
    model_lower = model_id.lower()

    matches = [key for key in PRICING_PER_1K if key in model_lower]
    if matches:
        best = max(matches, key=len)
        return PRICING_PER_1K[best], True

    return UNKNOWN_PRICING, False


def estimate_cost(input_tokens, output_tokens, model_id,
                  cache_read_tokens=0, cache_write_tokens=0):
    """Compute estimated cost in USD from token counts and model ID.

    Returns (cost_usd, priced) where priced is False when the model
    matched no known pricing family.
    """
    pricing, priced = get_pricing(model_id)
    cost = (input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"]
    cost += (cache_read_tokens / 1000) * pricing["input"] * CACHE_READ_MULTIPLIER
    cost += (cache_write_tokens / 1000) * pricing["input"] * CACHE_WRITE_MULTIPLIER
    return cost, priced
