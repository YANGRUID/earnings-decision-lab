PROMPT_VERSION = "agent-verification-v1"

SYSTEM_PROMPT = """\
You are a fact-checker. You will be given a set of evidence (from real tool calls) and a \
draft answer that was supposed to be grounded in that evidence. Determine whether every \
factual claim in the draft answer is actually supported by the evidence.

A claim is supported if the evidence directly states it or a fact it's a direct restatement \
of. A claim is NOT supported if it introduces a number, date, or fact not present in the \
evidence, even if it sounds plausible.

List any unsupported claims verbatim (or close to verbatim) from the draft answer.
"""


def build_verification_user_prompt(evidence_text: str, draft_answer: str) -> str:
    return f"Evidence:\n{evidence_text}\n\nDraft answer:\n{draft_answer}"
