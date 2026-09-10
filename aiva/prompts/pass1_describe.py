from aiva.prompts.common import INJECTION_GUARD, wrap_untrusted


class Pass1DescribePrompt:
    """Identify the underlying business task behind a single session."""

    def build_prompt(self, nonce, user_block, resp_block, all_tools,
                     invocation_count, total_input, total_output):
        tools_str = ', '.join(list(all_tools)[:15]) if all_tools else '(none)'

        return f"""You are analysing AI usage logs to understand what BUSINESS TASK a person was actually accomplishing.

{INJECTION_GUARD}

IMPORTANT: These logs may come from an agentic harness (Claude Code, Codex, Amazon Q Developer, LibreChat, or similar). The harness is just the tool - it is NOT the use case. Ignore all framing about "CLI agent", "sub-agent", "system-reminder", tool schemas, and permission modes. Look THROUGH the tool to the real work.

Bad answer: "Coding agent session" (describes the tool, not the task)
Good answer: "AWS RDS pricing research", "Building a security audit tool", "Meeting security-posture capture"

JUDGE BY BOTH SIDES. The input may be messy while the output is clearly valuable. In particular:
- Auto-transcribed meeting audio or dictation is often disfluent, garbled, or full of filler ("uh", crosstalk, swearing) BUT is a legitimate high-value use case. If the input looks like a raw transcript and the OUTPUT is a structured summary, brief, or assessment, classify it by the VALUABLE OUTPUT (e.g. "Meeting notes / customer discovery capture"), NOT as noise.
- When the user message is incoherent but the response is a coherent, structured, useful artefact, name the task from the output.

## What the user said (may be raw/disfluent input)
{wrap_untrusted(user_block, nonce)}

## What the AI produced (the output/artefact)
{wrap_untrusted(resp_block, nonce)}

## Signals
Tools available in session: {tools_str}
Invocations: {invocation_count} | Input tokens: {total_input:,} | Output tokens: {total_output:,}

## Your task
Identify the underlying business task. Respond in this exact JSON format:
{{
    "activity": "Short name for the real-world task (3-6 words, tool-agnostic)",
    "description": "One sentence: what was being achieved (from input OR output, whichever is coherent)?",
    "business_value": "One sentence: why a business would care about this task",
    "confidence": "high | medium | low"
}}

Only if BOTH the input and the output are incoherent, empty, or a broken/init call with no discernible artefact: set activity to "Task not identifiable from logs" and confidence to "low". Do NOT claim something has no value just because the input text is messy - check the output first."""
