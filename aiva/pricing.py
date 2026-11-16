# Ballpark cost from token counts only, the same for every model and source.
# Each call is costed at Sonnet-class on-demand rates (the midpoint). Reports
# show a range from Haiku-class ($1 / $5 per 1M) to Opus-class ($5 / $25) rates.
# All three keep a 1:5 input:output ratio, so the range is a fixed multiple of
# the midpoint. Cache reads and writes are priced as ratios of the input rate.
INPUT_PER_1M = 3.0
OUTPUT_PER_1M = 15.0
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25
LOW_FACTOR = 1 / 3
HIGH_FACTOR = 5 / 3


def estimate_cost(input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
    """Midpoint cost estimate in USD."""
    input_equivalent = (input_tokens + cache_read_tokens * CACHE_READ_MULTIPLIER
                        + cache_write_tokens * CACHE_WRITE_MULTIPLIER)
    return (input_equivalent * INPUT_PER_1M + output_tokens * OUTPUT_PER_1M) / 1_000_000


def cost_range(cost):
    """(low, high) for a midpoint cost."""
    return cost * LOW_FACTOR, cost * HIGH_FACTOR


COST_NOTE = (f"Costs are estimated from token counts at Sonnet-class rates (${INPUT_PER_1M:g} input, "
             f"${OUTPUT_PER_1M:g} output per 1M tokens), whatever the model. The range uses Haiku-class to Opus-class rates. "
             "Negotiated rates, batch and provisioned throughput are not taken into account.")
