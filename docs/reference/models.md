# Recommended Models

Mori uses three model roles with different requirements:

| Role | Used for | Tier 1 | Tier 2 | Why |
|------|----------|--------|--------|-----|
| Dream | Session distillation | Kimi K2.6 · Claude Opus 4 · GPT-5 | GLM 5.1 | Reasoning depth, large context, synthesis quality |
| Consult | Strategic guidance | Kimi K2.6 · Claude Opus 4 · GPT-5 | Gemini 3.5 Flash | Reasoning matters, latency acceptable |
| Fast | Contradiction scan, freshness checks | Gemma 4 31B | DeepSeek V4 Flash | Speed over reasoning, binary classification |

Provider recommendations: Novita, Parasail, DeepInfra, Nebius for open-weight models. Anthropic and OpenAI APIs directly for Claude and GPT.

Works with any OpenAI-compatible endpoint — Ollama locally, or a gateway like Bifrost for routing, fallbacks, and cost visibility.
