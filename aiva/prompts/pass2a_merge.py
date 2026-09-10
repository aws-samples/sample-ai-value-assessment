from aiva.prompts.common import INJECTION_GUARD, wrap_untrusted


class Pass2aMergePrompt:
    """Merge same-meaning clusters produced independently by different chunks."""

    def build_prompt(self, nonce, listing):
        return f"""You are merging BUSINESS USE CASE clusters that were produced independently from different batches of the same audit. Some describe the same real use case in different words and should be merged; others are genuinely distinct and must stay separate.

{INJECTION_GUARD}

## Clusters
{wrap_untrusted(chr(10).join(listing), nonce)}

## Your task
Return merge groups. Every cluster id must appear in exactly one group (a group with a single id means that cluster stays unmerged). The id values (cluster_N) are trusted identifiers - use them exactly as given. Respond in this exact JSON format:
{{
    "merged": [
        {{
            "name": "Short business use case name (tool-agnostic)",
            "description": "One sentence describing this use case",
            "cluster_ids": ["cluster_0", "cluster_3"]
        }}
    ]
}}"""
