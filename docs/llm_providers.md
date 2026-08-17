# LLM providers

This project is not tied to one model vendor. `backend/src/services/llm/` defines a single
`LLMProvider` interface; RAG, structured extraction, and agent code (Phases 5-7) depend only on
that interface, never on an OpenAI/Anthropic/DeepSeek SDK directly. Swapping providers — or
letting different developers use different providers locally — is a config change, not a code
change.

## Supported providers

| Provider | Class | Wire format | Docs verified |
|---|---|---|---|
| DeepSeek | `DeepSeekProvider` | OpenAI-compatible | api-docs.deepseek.com, 2026-08 |
| OpenAI | `OpenAIProvider` | OpenAI-compatible (canonical) | — |
| Anthropic | `AnthropicProvider` | Messages API (distinct shape) | platform.claude.com/docs, 2026-08 |
| Any other OpenAI-compatible endpoint | `OpenAICompatibleProvider` | OpenAI-compatible | best-effort, backend-dependent |

DeepSeek, OpenAI, and the generic compatible provider all share one transport implementation
(`services/llm/openai_compatible.py`) because they speak the identical wire format — confirmed
by fetching DeepSeek's current API docs rather than assuming compatibility from memory, which
mattered here: DeepSeek deprecated its `deepseek-chat`/`deepseek-reasoner` model names on
2026-07-24 in favor of `deepseek-v4-flash`/`deepseek-v4-pro`, a change that postdates this
project's baseline knowledge and would have shipped a broken default silently if not checked.
They remain three distinct, separately-configured *classes* (not one generic class with a
vendor string) so config and logs always show which vendor is actually in use.

**Also found live:** DeepSeek's V4 models default to thinking mode *on*. A first connectivity
check with `max_tokens=5` came back with empty content and `finish_reason="length"` — the
entire budget went to hidden reasoning tokens. `DeepSeekProvider` now sends
`thinking: {"type": "disabled"}` on every request (per
`api-docs.deepseek.com/guides/thinking_mode`), restoring fast/deterministic responses. This is
DeepSeek-specific — the other providers don't need or get this field.

Anthropic's Messages API has a genuinely different shape (system prompt as a top-level field,
typed content blocks, `x-api-key`/`anthropic-version` headers) and is implemented directly
against that shape rather than shoehorned into the OpenAI-compatible transport.

## Configuration

One env var selects the active provider; only that provider's variables are required:

```
LLM_PROVIDER=deepseek   # deepseek | openai | anthropic | openai_compatible

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash

OPENAI_API_KEY=
OPENAI_MODEL=

ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=

OPENAI_COMPATIBLE_API_KEY=
OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_MODEL=
```

`services/llm/factory.py::get_llm_provider(settings)` reads `LLM_PROVIDER` and constructs the
matching adapter, raising `MissingAPIKeyError` (naming the exact missing variable) if that
provider's key or model isn't set, and `UnknownProviderError` for any other value. No model
name is hardcoded as a fallback default anywhere — an unset `*_MODEL` is a configuration error,
not a silent default.

## The interface

```python
class LLMProvider(ABC):
    name: str
    capabilities: Capabilities

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024) -> GenerateResult: ...
    def generate_structured(self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024) -> BaseModel: ...
    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]: ...
```

`ChatMessage`, `ToolDefinition`, `ToolCall`, `GenerateResult` (`services/llm/types.py`) are
plain Pydantic models — the only types callers need to know about.

## Capability differences — stated honestly, not papered over

Every provider declares `Capabilities(supports_structured_output, supports_tool_calling,
supports_streaming)`, but "supports structured output" doesn't mean *the same mechanism*:

- **DeepSeek / OpenAI / generic compatible:** JSON mode
  (`response_format: {"type": "json_object"}`) plus the target Pydantic schema embedded in a
  system message, then `schema.model_validate(json.loads(content))`. This is the subset
  guaranteed to work identically across DeepSeek and OpenAI. OpenAI's stricter,
  schema-constrained `response_format: {"type": "json_schema", ...}` mode is **not** used —
  portability across providers was chosen over squeezing out OpenAI-specific strictness.
- **Anthropic:** no JSON-mode equivalent. Implemented via a *forced single tool call* — the
  target schema becomes a synthetic tool (`emit_result`) and `tool_choice` forces the model to
  call it, which is Anthropic's own documented pattern for structured output. The tool call's
  `input` is validated against the schema the same way.

Both paths raise `services.llm.errors.StructuredOutputError` on failure — callers don't need to
know which mechanism produced it, but the mechanism itself is not pretended to be identical.

**Generic `OpenAICompatibleProvider`** declares capabilities best-effort (`True`/`True`/`True`)
because the actual backend is operator-configured and unknown to this codebase in advance — if
it doesn't actually support tool calling, that surfaces as a normal request failure, not a
capability-check false negative.

The agent layer (Phase 7) is expected to check `provider.capabilities` before relying on a
feature, rather than assuming every configured provider behaves identically.

## Secrets

Real API keys live only in the gitignored `.env` (never `.env.example`, never committed). This
project's operating rules require, before every commit touching configuration:
`git status`, `git check-ignore -v .env`, and a `git grep`/`git log --all -p` search for the
literal key value across the tracked tree and full history. No key is ever printed in full to
logs, README, screenshots, fixtures, or CI — CI configures no real LLM keys at all (see below).

## Testing

Every provider's request/response handling is tested against mocked HTTP (`pytest-httpx`) —
`tests/test_llm_openai_compatible_transport.py` (DeepSeek + OpenAI, parametrized since they
share a transport), `tests/test_llm_anthropic.py`, and `tests/test_llm_factory.py` (provider
selection, missing-key errors, unknown-provider errors, capability declarations). **No test
calls a live paid API** — CI never sets a real LLM key. The one live call this project makes is
a single manual connectivity check against DeepSeek during local development setup (see
`docs/engineering_decisions.md`), not part of the automated test suite.

## Adding another provider

1. Subclass `LLMProvider` (or `_OpenAICompatibleTransport` if the new provider is
   OpenAI-compatible) in `services/llm/<name>.py`.
2. Add its env vars to `.env.example` and `core/config.py`.
3. Add a branch in `factory.py::get_llm_provider`.
4. Add mocked-HTTP tests following the existing pattern — no live calls.

## Frontend / runtime provider switching

Not implemented yet (Phase 8+). Provider and secrets are server-side environment configuration
only for now. If a settings UI is added later to let a user pick a provider, the API key itself
must never be sent to or stored in the browser (no `localStorage`, no client-side exposure) —
any such feature should let the browser select a provider *name*, with the server resolving
that to a server-held key, not accept a key from the client.
