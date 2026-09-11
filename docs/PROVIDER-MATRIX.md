# Provider And Model Matrix

NexGate retains the portable model aliases and compatibility behavior from
the established stack. `make up` renders an alias only when every listed
environment variable has a non-placeholder value in the ignored `.env`.
The ChatGPT subscription and virtual router aliases additionally require the
NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true switch. This prevents a new stack from
starting an OAuth device flow before the operator has deliberately configured it.
Do not add credentials to this file.

The `quick-research` A2A spike is an agent registry entry, not a model catalog
alias. It is not enabled by the example overlay. Its candidate runtime is
LiteLLM `1.100.1` with `a2a-sdk==1.1.2`; see the architecture compatibility gate
before enabling it. The integration upstream provides deterministic responses
and proves gateway transport, not a live research provider's answer quality.

| Alias | Required environment variables |
| --- | --- |
| `qwencloud/qwen3.8-max` | `NEXGATE_QWENCLOUD_QWEN3_8_MAX_API_BASE`, `QWENCLOUD_API_KEY` |
| `qwencloud/qwen3.7-plus` | `NEXGATE_QWENCLOUD_QWEN3_7_PLUS_API_BASE`, `QWENCLOUD_API_KEY` |
| `qwencloud/deepseek-v4-flash` | `NEXGATE_QWENCLOUD_DEEPSEEK_V4_FLASH_API_BASE`, `QWENCLOUD_API_KEY` |
| `qwencloud/deepseek-v4-pro` | `NEXGATE_QWENCLOUD_DEEPSEEK_V4_PRO_API_BASE`, `QWENCLOUD_API_KEY` |
| `qwencloud-payg/qwen3.7-plus-2026-05-26` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_PLUS_2026_05_26_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-plus` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_PLUS_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-max` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_MAX_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-max-2026-06-08` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_MAX_2026_06_08_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-max-2026-05-20` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_MAX_2026_05_20_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-max-2026-05-17` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_MAX_2026_05_17_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.7-max-preview` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_7_MAX_PREVIEW_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/qwen3.8-max` | `NEXGATE_QWENCLOUD_PAYG_QWEN3_8_MAX_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/deepseek-v4-flash` | `NEXGATE_QWENCLOUD_PAYG_DEEPSEEK_V4_FLASH_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/deepseek-v4-flash-2` | `NEXGATE_QWENCLOUD_PAYG_DEEPSEEK_V4_FLASH_2_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/deepseek-v4-pro` | `NEXGATE_QWENCLOUD_PAYG_DEEPSEEK_V4_PRO_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/kimi-k2.7-code` | `NEXGATE_QWENCLOUD_PAYG_KIMI_K2_7_CODE_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud/glm-5.2` | `NEXGATE_QWENCLOUD_GLM_5_2_API_BASE`, `QWENCLOUD_API_KEY` |
| `qwencloud-payg/glm-5.2` | `NEXGATE_QWENCLOUD_PAYG_GLM_5_2_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `qwencloud-payg/glm-5.1` | `NEXGATE_QWENCLOUD_PAYG_GLM_5_1_API_BASE`, `QWEN_PAYASYOUGO_API_KEY` |
| `deepseek-v4-flash` | `DEEPSEEK_API_KEY`, `NEXGATE_DEEPSEEK_V4_FLASH_API_BASE` |
| `deepseek-v4-pro` | `DEEPSEEK_API_KEY`, `NEXGATE_DEEPSEEK_V4_PRO_API_BASE` |
| `deepseek-v4-pro[1m]` | `DEEPSEEK_API_KEY`, `NEXGATE_DEEPSEEK_V4_PRO_1M_API_BASE` |
| `gemma` | `NEXGATE_GEMMA_API_BASE` |
| `bielik` | `NEXGATE_BIELIK_API_BASE` |
| `psyllm-4b` | `NEXGATE_PSYLLM_4B_API_BASE`, `PSYLLM_SERVER_API_KEY` |
| `glm-5.1` | `NEXGATE_GLM_5_1_API_BASE`, `ZAI_API_KEY` |
| `glm-5.2` | `NEXGATE_GLM_5_2_API_BASE`, `ZAI_API_KEY` |
| `glm-5.3` | `NEXGATE_GLM_5_3_API_BASE`, `ZAI_API_KEY` |
| `glm-5.3-flash` | `NEXGATE_GLM_5_3_FLASH_API_BASE`, `ZAI_API_KEY` |
| `claude-opus-4-8[1m]` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_8_1M_API_BASE` |
| `claude-opus-4-8` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_8_API_BASE` |
| `claude-opus-4-7[1m]` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_7_1M_API_BASE` |
| `claude-opus-4-7` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_7_API_BASE` |
| `claude-sonnet-5[1m]` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_SONNET_5_1M_API_BASE` |
| `claude-sonnet-4-6` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_SONNET_4_6_API_BASE` |
| `claude-sonnet-5` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_SONNET_5_API_BASE` |
| `sonnet` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_SONNET_API_BASE` |
| `claude-fable-5-1` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_FABLE_5_1_API_BASE` |
| `claude-opus-5[1m]` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_5_1M_API_BASE` |
| `claude-opus-5` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_OPUS_5_API_BASE` |
| `claude-haiku-4-5-20251001` | `CLAUDE_CODE_OAUTH_TOKEN`, `NEXGATE_CLAUDE_HAIKU_4_5_20251001_API_BASE` |
| `kimi` | `KIMI_CODE_API_BASE`, `KIMI_CODE_API_KEY` |
| `kimi-k3` | `KIMI_CODE_API_BASE`, `KIMI_CODE_API_KEY` |
| `minimax-m3` | `MINIMAX_API_KEY`, `NEXGATE_MINIMAX_M3_API_BASE` |
| `mistral` | `MISTRAL_API_KEY` |
| `scaleway/gpt-oss` | `NEXGATE_SCALEWAY_GPT_OSS_API_BASE`, `SCW_SECRET_KEY` |
| `scaleway/glm-5.2` | `NEXGATE_SCALEWAY_GLM_5_2_API_BASE`, `SCW_SECRET_KEY` |
| `scaleway/gemma-4` | `NEXGATE_SCALEWAY_GEMMA_4_API_BASE`, `SCW_SECRET_KEY` |
| `scaleway/qwen3.6` | `NEXGATE_SCALEWAY_QWEN3_6_API_BASE`, `SCW_SECRET_KEY` |
| `scaleway-devstral` | `NEXGATE_SCALEWAY_DEVSTRAL_API_BASE`, `SCW_SECRET_KEY` |
| `chatgpt/gpt-5.6-sol` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `chatgpt/gpt-5.6-terra` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `chatgpt/gpt-5.6-luna` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `chatgpt/gpt-6-astra` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `gpt-6-astra` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `gpt-5.6-terra` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `gpt-5.6-sol` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `gpt-5.6-luna` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `gpt-5.6` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `circit-gateway` | `CIRCIT_CLOUDFLARE_API_KEY`, `NEXGATE_CIRCIT_GATEWAY_API_BASE` |
| `smart-router` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `smart-router-claude` | `NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true` |
| `cf/llama-3.3` | `CLOUDFLARE_API_KEY`, `NEXGATE_CF_LLAMA_3_3_API_BASE` |
| `cf/qwen-coder` | `CLOUDFLARE_API_KEY`, `NEXGATE_CF_QWEN_CODER_API_BASE` |
| `cf/llama-3.1-8b` | `CLOUDFLARE_API_KEY`, `NEXGATE_CF_LLAMA_3_1_8B_API_BASE` |
| `or/nemotron-ultra-free` | `NEXGATE_OR_NEMOTRON_ULTRA_FREE_API_BASE`, `OPENROUTER_API_KEY` |
| `or/gemma-4-26b-free` | `NEXGATE_OR_GEMMA_4_26B_FREE_API_BASE`, `OPENROUTER_API_KEY` |
| `or/gpt-oss-20b-free` | `NEXGATE_OR_GPT_OSS_20B_FREE_API_BASE`, `OPENROUTER_API_KEY` |
| `qwencloud/kimi-k2.7-code` | `NEXGATE_QWENCLOUD_KIMI_K2_7_CODE_API_BASE`, `QWENCLOUD_API_KEY` |
| `mistral/glm-5.2` | `MISTRAL_API_KEY` |
| `opencode/big-pickle` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/deepseek-v4-flash` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/deepseek-v4-pro` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/glm-5.3-flash` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/glm-5.3` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/glm-5.2` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/glm-5.1` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/glm-5` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/gpt-5.6-luna` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/gpt-5.6-sol` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/gpt-5.6-terra` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/gpt-5.6` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/grok-4.5` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/grok-4.6` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/grok` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/grok-build` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/kimi-k3` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/kimi-k2.7-code` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/kimi-k2.6` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/minimax-m3` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/minimax-m2.7` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/qwen3.8-max` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/qwen3.7-max` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/qwen3.7-plus` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/qwen3.6-plus` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/mimo-v2.5` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/mimo-v2.5-pro` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/mimo-2.5-pro` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/mimo-2.5` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `opencode/mimo` | `NEXGATE_OPENCODE_API_BASE`, `OPENCODE_API_KEY` |
| `fallback-claude-opus-5[1m]` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_5_1M_API_BASE` |
| `fallback-claude-opus-5` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_5_API_BASE` |
| `fallback-claude-opus-4-8[1m]` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_8_1M_API_BASE` |
| `fallback-claude-opus-4-8` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_8_API_BASE` |
| `fallback-claude-opus-4-7[1m]` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_7_1M_API_BASE` |
| `fallback-claude-opus-4-7` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_OPUS_4_7_API_BASE` |
| `fallback-claude-sonnet-5[1m]` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_SONNET_5_1M_API_BASE` |
| `fallback-claude-sonnet-5` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_SONNET_5_API_BASE` |
| `fallback-claude-sonnet-4-6` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_SONNET_4_6_API_BASE` |
| `fallback-claude-fable-5-1` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_FABLE_5_1_API_BASE` |
| `fallback-claude-haiku-4-5-20251001` | `ANTHROPIC_FALLBACK_TOKEN`, `NEXGATE_CLAUDE_HAIKU_4_5_20251001_API_BASE` |

For a local model, point the relevant `*_API_BASE` variable at any
OpenAI-compatible server you operate. NexGate does not prescribe a local
runtime, hardware platform, or model download.
