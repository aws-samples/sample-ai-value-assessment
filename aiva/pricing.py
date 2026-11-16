# Ballpark cost from token counts only, the same for every model and source.
# The input and output rates are set in the config (input_per_1m,
# output_per_1m). Cache reads and writes are priced as ratios of the input rate.
DEFAULT_INPUT_PER_1M = 3.0
DEFAULT_OUTPUT_PER_1M = 15.0
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0,
                  input_per_1m=DEFAULT_INPUT_PER_1M, output_per_1m=DEFAULT_OUTPUT_PER_1M):
    """Estimated cost in USD."""
    input_equivalent = (input_tokens + cache_read_tokens * CACHE_READ_MULTIPLIER
                        + cache_write_tokens * CACHE_WRITE_MULTIPLIER)
    return (input_equivalent * input_per_1m + output_tokens * output_per_1m) / 1_000_000


def cost_note(input_per_1m, output_per_1m):
    """One line for the reports on how cost was estimated."""
    return (f"Costs are estimated from token counts at ${input_per_1m:g} input and ${output_per_1m:g} "
            "output per 1M tokens, whatever the model. Set input_per_1m and output_per_1m in the "
            "config to use your own rates. Negotiated rates, batch and provisioned throughput are "
            "not taken into account.")
