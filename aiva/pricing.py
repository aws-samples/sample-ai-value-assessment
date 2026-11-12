# Per-1K-token on-demand prices (USD), sourced from the model catalog
# ($/1M / 1000). Keyed by the canonical model family token found in the
# model ID. Longest matching key wins so opus-4-8 never collides with a
# shorter opus prefix. Cache read/write are priced as ratios of the input
# rate (Bedrock/Anthropic 5-minute-TTL convention): read ~0.1x, write ~1.25x,
# unless a family sets its own "cache_read" rate.
#
# NOTE: on-demand estimate only. Provisioned throughput, batch (-50%), and
# 1-hour cache TTL (2x write) are NOT modelled - see get_pricing.
PRICING_PER_1K = {
    # 5.x family: Anthropic list prices; not in the AWS Pricing API yet
    # (2026-10-27). Opus 5.5 is confirmed against a real Bedrock bill:
    # us. profile billed $4.40 / $22 / $0.22 read / $5.50 write per 1M,
    # i.e. these rates x GEO_PREMIUM. The others are unconfirmed.
    "claude-fable-5-1": {"input": 0.010, "output": 0.050, "cache_read": 0.00025},
    "claude-fable-5": {"input": 0.010, "output": 0.050},
    "claude-opus-5-5": {"input": 0.004, "output": 0.020, "cache_read": 0.0002},
    "claude-opus-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-5-5": {"input": 0.002, "output": 0.010},
    "claude-sonnet-5": {"input": 0.002, "output": 0.010},
    "claude-opus-4-8": {"input": 0.005, "output": 0.025},
    "claude-opus-4-7": {"input": 0.005, "output": 0.025},
    "claude-opus-4-6": {"input": 0.005, "output": 0.025},
    "claude-opus-4-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005},
    # Bedrock on-demand list prices, us-east-1, spelled as the Bedrock model
    # IDs spell them. "claude-opus-4-2025" is the dated Opus 4 ID only, so a
    # new 4.x Opus is never silently priced at Opus 4 rates. Claude 3 Opus is
    # left unpriced on purpose: not offered on demand in us-east-1.
    "claude-opus-4-1": {"input": 0.015, "output": 0.075},
    "claude-opus-4-2025": {"input": 0.015, "output": 0.075},
    "claude-3-7-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-5-haiku": {"input": 0.0008, "output": 0.004},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "nova-pro": {"input": 0.0008, "output": 0.0032},
    "nova-lite": {"input": 0.00006, "output": 0.00024},
    "nova-micro": {"input": 0.000035, "output": 0.00014},
    "titan-text-express": {"input": 0.0002, "output": 0.0006},
}

CACHE_READ_MULTIPLIER = 0.1  # unless the family sets its own "cache_read" rate
CACHE_WRITE_MULTIPLIER = 1.25

# Claude 4.5 and later cost 10% more through a geographic ("us.", "eu.", ...)
# or in-region model ID than through a "global." inference profile (AWS
# Pricing API, us-west-2, 2026-10-27). The table above holds global rates.
# Sonnet 4 and older have one price. Opus 5.5 confirmed from a bill; other
# 4.7+ and 5.x families are assumed to follow.
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
