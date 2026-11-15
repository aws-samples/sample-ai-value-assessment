# Per-1K-token on-demand prices (USD) for Anthropic models on Bedrock
# ($/1M / 1000), global inference profile rates. Checked against the AWS
# Price List API (AmazonBedrockFoundationModels, us-east-1, 2026-11-15).
# Other providers are not priced and show as unpriced in the report.
# Keyed by the canonical model family token found in the model ID. Longest
# matching key wins so opus-4-8 never collides with a shorter opus prefix.
# Cache read/write are priced as ratios of the input rate (5-minute TTL):
# read 0.1x, write 1.25x, unless a family sets its own "cache_read" rate.
#
# NOTE: on-demand estimate only. Provisioned throughput, batch (-50%), and
# 1-hour cache TTL (2x write) are NOT modelled - see get_pricing.
PRICING_PER_1K = {
    "claude-fable-5-1": {"input": 0.010, "output": 0.050, "cache_read": 0.00025},
    "claude-fable-5": {"input": 0.010, "output": 0.050},
    "claude-opus-5-5": {"input": 0.004, "output": 0.020, "cache_read": 0.0002},
    "claude-opus-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-5-5": {"input": 0.002, "output": 0.010, "cache_read": 0.0001},
    "claude-sonnet-5": {"input": 0.002, "output": 0.010},
    "claude-opus-4-8": {"input": 0.005, "output": 0.025},
    "claude-opus-4-7": {"input": 0.005, "output": 0.025},
    "claude-opus-4-6": {"input": 0.005, "output": 0.025},
    "claude-opus-4-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005},
    # Bedrock names the 3.x models version first: anthropic.claude-3-5-haiku-...
    "claude-3-5-haiku": {"input": 0.0008, "output": 0.004},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
}

CACHE_READ_MULTIPLIER = 0.1  # unless the family sets its own "cache_read" rate
CACHE_WRITE_MULTIPLIER = 1.25

# Claude 4.5 and later cost 10% more through a geographic ("us.", "eu.", ...)
# or in-region model ID than through a "global." inference profile. The table
# above holds global rates. Sonnet 4 and older have one price. Checked for
# every family against the Price List API (2026-11-15).
GEO_PREMIUM = 1.1
GEO_PREMIUM_FAMILIES = {
    "claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-opus-4-5",
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5",
    "claude-sonnet-5-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1",
}

# Fallback when a model ID matches no known family. Chosen to be obviously
# wrong-looking (0) so unpriced models surface as $0.00 rather than silently
# borrowing another tier's rate.
UNKNOWN_PRICING = {"input": 0.0, "output": 0.0}


def get_pricing(model_id):
    """Look up per-1K-token pricing by longest matching family token.

    Returns (pricing, priced) where priced is False when the model ID
    matched no known family (so callers can flag the cost as unreliable).
    Longest-match avoids opus-4-8 colliding with a shorter opus key.
    Rates come back with the geographic premium applied when it applies.
    """
    model_lower = model_id.lower()

    matches = [key for key in PRICING_PER_1K if key in model_lower]
    if not matches:
        return UNKNOWN_PRICING, False

    best = max(matches, key=len)
    base = PRICING_PER_1K[best]
    pricing = {
        "input": base["input"],
        "output": base["output"],
        "cache_read": base.get("cache_read", base["input"] * CACHE_READ_MULTIPLIER),
        "cache_write": base["input"] * CACHE_WRITE_MULTIPLIER,
    }
    if best in GEO_PREMIUM_FAMILIES and "global." not in model_lower:
        pricing = {k: v * GEO_PREMIUM for k, v in pricing.items()}
    return pricing, True


def estimate_cost(input_tokens, output_tokens, model_id,
                  cache_read_tokens=0, cache_write_tokens=0):
    """Compute estimated cost in USD from token counts and model ID.

    Returns (cost_usd, priced) where priced is False when the model
    matched no known pricing family.
    """
    pricing, priced = get_pricing(model_id)
    if not priced:
        return 0.0, False
    cost = (input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"]
    cost += (cache_read_tokens / 1000) * pricing["cache_read"]
    cost += (cache_write_tokens / 1000) * pricing["cache_write"]
    return cost, True
