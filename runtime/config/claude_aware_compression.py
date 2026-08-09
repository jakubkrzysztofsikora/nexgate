"""Claude Code-aware prompt compression.

Unlike litellm's built-in compression_interception, this callback does NOT
inject a foreign `litellm_content_retrieve` tool. Instead it safely shrinks
the conversation by truncating long tool_result blocks, dropping image
blocks from old messages, and, if still over budget, dropping the oldest
non-system messages. This keeps Claude Code's own tool list intact and avoids
the hallucinations caused by stubbed context.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.token_counter import token_counter
from litellm.types.utils import CallTypes

_logger = logging.getLogger("claude_aware_compression")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _logger.addHandler(_handler)
    _logger.propagate = False

_DEFAULT_IMAGE_TOKEN_ESTIMATE = 1000
_DEFAULT_MAX_REQUEST_BYTES = 5_000_000


class ClaudeAwareCompression(CustomLogger):
    """CustomLogger that compresses Anthropic Messages without adding tools."""

    def __init__(
        self,
        enabled: bool = True,
        compression_trigger: int = 100,
        compression_target: int = 50,
        max_tool_result_chars: int = 8000,
        min_recent_messages: int = 6,
        image_token_estimate: int = _DEFAULT_IMAGE_TOKEN_ESTIMATE,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        drop_old_images: bool = True,
        drop_old_messages: bool = True,
    ):
        super().__init__()
        self.enabled = enabled
        self.compression_trigger = compression_trigger
        self.compression_target = compression_target
        self.max_tool_result_chars = max_tool_result_chars
        self.min_recent_messages = min_recent_messages
        self.image_token_estimate = image_token_estimate
        self.max_request_bytes = max_request_bytes
        self.drop_old_images = drop_old_images
        self.drop_old_messages = drop_old_messages
        self._init_prom_counters()

    def _init_prom_counters(self) -> None:
        try:
            from prometheus_client import Counter, REGISTRY
            if not hasattr(ClaudeAwareCompression, "_prom_compressed_saved"):
                if "litellm_compressed_tokens_saved" in REGISTRY._names_to_collectors:
                    ClaudeAwareCompression._prom_compressed_saved = REGISTRY._names_to_collectors["litellm_compressed_tokens_saved"]
                else:
                    ClaudeAwareCompression._prom_compressed_saved = Counter(
                        "litellm_compressed_tokens_saved",
                        "Total tokens saved by local Claude-aware context compression",
                        ["model"],
                    )
                if "litellm_compression_events" in REGISTRY._names_to_collectors:
                    ClaudeAwareCompression._prom_compression_events = REGISTRY._names_to_collectors["litellm_compression_events"]
                else:
                    ClaudeAwareCompression._prom_compression_events = Counter(
                        "litellm_compression_events",
                        "Total context compression events executed",
                        ["model"],
                    )
                path = os.environ.get("CLAUDE_COMPRESSION_SAVINGS_LOG", "/host/logs/token_savings.jsonl")
                if os.path.exists(path):
                    by_model_saved = {}
                    by_model_events = {}
                    with open(path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                                model = entry.get("model", "claude-sonnet-5")
                                saved = entry.get("saved_tokens") or entry.get("tokens_saved") or 0
                                by_model_saved[model] = by_model_saved.get(model, 0) + int(saved)
                                by_model_events[model] = by_model_events.get(model, 0) + 1
                            except Exception:
                                pass
                    for model, saved in by_model_saved.items():
                        ClaudeAwareCompression._prom_compressed_saved.labels(model=model).inc(saved)
                    for model, evts in by_model_events.items():
                        ClaudeAwareCompression._prom_compression_events.labels(model=model).inc(evts)
        except Exception:
            pass

    def _model_context_window(
        self, model: str, model_info: Optional[Dict[str, Any]] = None
    ) -> int:
        """Best-effort context window lookup for model-aware thresholds.

        Order of precedence: explicit model_info (deployment stage) > built-in
        heuristic (knows the user's configured aliases) > litellm.model_cost
        (often stale, e.g. reports 128k for claude-opus-4-8 even with the 1M
        beta header).
        """
        if isinstance(model_info, dict):
            try:
                cw = int(model_info.get("context_window", 0) or 0)
                if cw > 0:
                    return cw
            except (TypeError, ValueError):
                pass
        model_lower = model.lower()
        base = model_lower.split("[")[0].split("/")[-1]
        base = base.replace("anthropic.", "", 1)
        if base.startswith(("claude-opus-4-8", "claude-opus-4-7")):
            return 1_000_000
        if base.startswith((
            "claude-sonnet-5",
            "claude-sonnet-4.6",
            "claude-sonnet-4-6",
            "claude-sonnet-4.5",
            "claude-sonnet-4-5",
        )):
            return 1_000_000
        if "claude-opus" in model_lower or "claude-sonnet" in model_lower or "claude" in model_lower:
            return 200_000
        if "glm-5.2" in model_lower:
            return 250_000
        if "glm-5.1" in model_lower:
            return 200_000
        if "kimi" in model_lower:
            return 262_144
        if "deepseek" in model_lower:
            return 1_000_000
        try:
            import litellm

            cw = int(litellm.model_cost.get(model, {}).get("max_tokens", 0))
            if cw > 0:
                return cw
        except Exception:
            pass
        return 100_000

    def _effective_thresholds(
        self, model: str, model_info: Optional[Dict[str, Any]] = None, is_first: bool = False
    ) -> tuple[int, int]:
        """Return trigger and target, scaling up for large-context models.

        For the very first request (no assistant turn yet) compression is
        disabled so the initial task and system prompt pass through untouched.
        Subsequent requests compress aggressively so context growth is capped
        from prompt #2 onward.
        """
        context_window = self._model_context_window(model, model_info)
        if is_first:
            return context_window * 2, context_window
        # Tests and controlled rollouts may set a lower threshold; the production
        # default remains 100, but silently flooring it breaks that contract.
        trigger = max(self.compression_trigger, 1)
        target = max(self.compression_target, trigger // 2)
        return trigger, target

    async def async_pre_call_hook(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = self._extract_data(args, kwargs)
        if data is None:
            return None
        call_type = args[3] if len(args) >= 4 else kwargs.get("call_type")
        return self._compress(data, call_type)

    async def async_pre_call_deployment_hook(
        self, kwargs: Dict[str, Any], call_type: Optional[CallTypes]
    ) -> Optional[Dict[str, Any]]:
        return self._compress(kwargs, call_type)

    def _extract_data(
        self, args: tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if len(args) >= 3 and isinstance(args[2], dict):
            return args[2]
        if args and isinstance(args[0], dict):
            return args[0]
        if isinstance(kwargs.get("data"), dict):
            return kwargs["data"]
        return None

    @staticmethod
    def _is_native_chatgpt_request(data: Dict[str, Any]) -> bool:
        """Keep native Responses tool results intact at this interception point."""
        provider = str(data.get("custom_llm_provider") or "").lower()
        model = str(data.get("model") or "").lower()
        if provider == "chatgpt" or model.startswith("chatgpt/"):
            return True
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            return any(
                str(metadata.get(key) or "").lower().startswith("chatgpt/")
                for key in ("ccproxy_provider_model", "ccproxy_litellm_model")
            )
        return False

    def _safe_token_count(self, model: str, messages: List[Dict[str, Any]]) -> int:
        try:
            return int(token_counter(model=model, messages=messages))
        except Exception as exc:
            _logger.debug("ClaudeAwareCompression: token_counter failed, using fallback: %s", exc)
            return self._fallback_token_count(messages)

    def _fallback_token_count(self, messages: List[Dict[str, Any]]) -> int:
        chars = 0
        images = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        chars += len(block.get("text", "") or "")
                    elif btype == "tool_use":
                        chars += len(str(block.get("input", "")))
                    elif btype == "tool_result":
                        chars += len(str(block.get("content", "")))
                    elif btype == "image":
                        images += 1
        return chars // 4 + images * self.image_token_estimate

    def _strip_images_for_counting(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        stripped: List[Dict[str, Any]] = []
        for msg in messages:
            new_msg = self._strip_images_from_message(msg)
            if new_msg is msg:
                stripped.append(msg)
            else:
                stripped.append(new_msg)
        return stripped

    def _strip_images_from_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        content = msg.get("content")
        if not isinstance(content, list):
            return msg
        new_blocks = [self._strip_images_from_block(b) for b in content]
        new_blocks = [b for b in new_blocks if b is not None]
        if new_blocks == content:
            return msg
        return {**msg, "content": new_blocks}

    def _strip_images_from_block(self, block: Any) -> Any:
        if not isinstance(block, dict):
            return block
        if block.get("type") == "image":
            return None
        inner = block.get("content")
        if isinstance(inner, list):
            cleaned = [self._strip_images_from_block(b) for b in inner]
            cleaned = [b for b in cleaned if b is not None]
            if cleaned != inner:
                return {**block, "content": cleaned}
        return block

    def _estimate_request_bytes(
        self, data: Dict[str, Any], messages: List[Dict[str, Any]], model: str
    ) -> int:
        try:
            import json

            payload = {
                "model": model,
                "messages": messages,
                "system": data.get("system"),
                "tools": data.get("tools"),
                "tool_choice": data.get("tool_choice"),
                "max_tokens": 1,
            }
            return len(json.dumps(payload, default=str).encode("utf-8"))
        except Exception:
            return 0

    @staticmethod
    def _already_compressed(data: Dict[str, Any], messages: List[Dict[str, Any]]) -> bool:
        if getattr(messages, "_claude_aware_compressed", False):
            return True
        meta = data.get("litellm_metadata") or {}
        if isinstance(meta, dict) and meta.get("_claude_aware_compressed"):
            return True
        return False

    def _mark_compressed(self, data: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
        try:
            setattr(messages, "_claude_aware_compressed", True)
        except Exception:
            pass
        meta = data.setdefault("litellm_metadata", {})
        if isinstance(meta, dict):
            meta["_claude_aware_compressed"] = True

    def _record_savings(
        self, model: str, original_tokens: int, final_tokens: int
    ) -> None:
        try:
            import json as _json
            import os as _os
            from datetime import datetime as _dt
            path = _os.environ.get(
                "CLAUDE_COMPRESSION_SAVINGS_LOG", "/tmp/token_savings.jsonl"
            )
            _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
            saved = max(0, int(original_tokens) - int(final_tokens))
            entry = {
                "kind": "compression",
                "timestamp": _dt.utcnow().isoformat() + "Z",
                "model": model,
                "original_tokens": int(original_tokens),
                "final_tokens": int(final_tokens),
                "saved_tokens": saved,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry) + "\n")
        except Exception:
            pass

        try:
            from prometheus_client import Counter
            if not hasattr(ClaudeAwareCompression, "_prom_compressed_saved"):
                ClaudeAwareCompression._prom_compressed_saved = Counter(
                    "litellm_compressed_tokens_saved_total",
                    "Total tokens saved by local Claude-aware context compression",
                    ["model"],
                )
                ClaudeAwareCompression._prom_compression_events = Counter(
                    "litellm_compression_events_total",
                    "Total context compression events executed",
                    ["model"],
                )
            ClaudeAwareCompression._prom_compressed_saved.labels(model=model).inc(saved)
            ClaudeAwareCompression._prom_compression_events.labels(model=model).inc()
        except Exception:
            pass

    def _normalize_system_messages(
        self, data: Dict[str, Any], messages: List[Dict[str, Any]]
    ) -> None:
        """Move all Anthropic system-role messages out of the messages array.

        Anthropic's Messages API requires the system prompt as the top-level
        ``system`` parameter. The API rejects a ``role: system`` message inside
        ``messages`` (see Anthropic docs and Claude Code issue #63423). We
        collect every system message, merge its content into the top-level
        ``system`` list, promote the first ``output_config`` block, and remove
        the system messages from ``messages``.
        """
        if not messages:
            return

        system_messages = [
            m for m in messages if isinstance(m, dict) and m.get("role") == "system"
        ]
        if not system_messages:
            return

        first = system_messages[0]
        output_config = first.get("output_config")
        if output_config is not None and data.get("output_config") is None:
            data["output_config"] = output_config

        new_blocks: List[Dict[str, Any]] = []
        for msg in system_messages:
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    new_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        new_blocks.append(block)

        current = data.get("system")
        if new_blocks:
            data["system"] = self._system_blocks(current) + new_blocks

        messages[:] = [
            m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")
        ]

    def _system_blocks(self, current: Any) -> List[Dict[str, Any]]:
        """Normalize the existing top-level ``system`` value to content blocks."""
        if current is None:
            return []
        if isinstance(current, str):
            return [{"type": "text", "text": current}] if current else []
        if isinstance(current, list):
            blocks: List[Dict[str, Any]] = []
            for item in current:
                if isinstance(item, str):
                    if item:
                        blocks.append({"type": "text", "text": item})
                elif isinstance(item, dict):
                    blocks.append(item)
            return blocks
        return [{"type": "text", "text": str(current)}]

    def _compress(
        self, data: Dict[str, Any], call_type: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        if self._is_native_chatgpt_request(data):
            _logger.debug(
                "ClaudeAwareCompression: bypassing native ChatGPT Responses request model=%s",
                data.get("model"),
            )
            return None
        allowed_call_types = {
            getattr(CallTypes, "anthropic_messages", "anthropic_messages"),
            getattr(CallTypes, "acompletion", "acompletion"),
            getattr(CallTypes, "completion", "completion"),
            "anthropic_messages",
            "acompletion",
            "completion",
        }
        if call_type is not None and call_type not in allowed_call_types:
            return None
        if int(data.get("_agentic_loop_depth", 0) or 0) > 0:
            return None

        messages = data.get("messages")
        model = data.get("model")
        if not isinstance(messages, list) or not isinstance(model, str):
            return None
        if self._already_compressed(data, messages):
            return None

        original_len = len(messages)
        is_anthropic = call_type == CallTypes.anthropic_messages or "anthropic" in str(data.get("custom_llm_provider") or "").lower() or "claude" in model.lower()
        if is_anthropic:
            self._normalize_system_messages(data, messages)
        if not messages:
            return None

        try:
            model_info = data.get("model_info")
            is_first = not any(
                isinstance(m, dict) and m.get("role") == "assistant" for m in messages
            )
            trigger, target = self._effective_thresholds(model, model_info, is_first)
            count_input = self._strip_images_for_counting(messages)
            original_tokens = self._safe_token_count(model, count_input)
            original_bytes = self._estimate_request_bytes(data, messages, model)
            compressed = messages
            did_compress = False
            if original_tokens > trigger:
                compressed = self._drop_old_images(messages)
                non_system_indices = [
                    i for i, m in enumerate(compressed) if m.get("role") != "system"
                ]
                protected_indices: set[int] = set()
                if non_system_indices:
                    protected_indices.add(non_system_indices[0])
                # Deliberately NOT protecting a rolling window of recent
                # messages here. A trailing window moves by one message every
                # turn, so the message that just left it gets truncated for the
                # first time -- mutating the prompt *behind* the `cache_control`
                # breakpoint and forcing a full cache miss on every single turn.
                # Truncating on first sight is deterministic: once a tool result
                # is cut it stays cut identically forever, so the prefix is
                # byte-stable and cache reads (0.1x) compose with the token
                # saving instead of cancelling it out.
                compressed = self._truncate_tool_results(compressed, protected_indices)
                # Dropping messages shifts the whole sequence behind the
                # `cache_control` breakpoint at index -1, invalidating the
                # Anthropic trailing-turn cache (0.1x reads -> 1.25x write).
                if self.drop_old_messages:
                    compressed = self._drop_old_messages(compressed, model, target)
                did_compress = True

            changed = did_compress or len(messages) != original_len
            if original_bytes > self.max_request_bytes:
                compressed = self._shrink_to_byte_budget(data, compressed, model)
                compressed_bytes = self._estimate_request_bytes(data, compressed, model)
                if compressed_bytes < original_bytes:
                    changed = True

            if not changed:
                return None

            count_final = self._strip_images_for_counting(compressed)
            final_tokens = self._safe_token_count(model, count_final)
            removed = len(messages) - len(compressed)

            _logger.info(
                "ClaudeAwareCompression: model=%s original=%d final=%d removed=%d",
                model,
                original_tokens,
                final_tokens,
                removed,
            )
            self._record_savings(model, original_tokens, final_tokens)
            data["messages"] = compressed
            self._mark_compressed(data, data["messages"])
            return data
        except Exception:
            _logger.exception("ClaudeAwareCompression: failed to compress, passing through")
            return None

    def _truncate_tool_results(
        self,
        messages: List[Dict[str, Any]],
        protected_indices: Optional[set[int]] = None,
    ) -> List[Dict[str, Any]]:
        protected = protected_indices or set()
        truncated: List[Dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if idx in protected:
                truncated.append(msg)
                continue
            if msg.get("role") != "user":
                truncated.append(msg)
                continue

            content = msg.get("content")
            if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                truncated.append(self._truncate_string_message(msg, content))
                continue

            if not isinstance(content, list):
                truncated.append(msg)
                continue

            new_blocks: List[Any] = []
            changed = False
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue

                block_type = block.get("type")
                if block_type == "tool_result":
                    text, path, non_text = self._extract_tool_result_text(block)
                    if text and len(text) > self.max_tool_result_chars:
                        block = self._replace_tool_result_text(
                            block, path, self._summarize(text), non_text
                        )
                        changed = True
                elif block_type == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and len(text) > self.max_tool_result_chars:
                        block = {**block, "text": self._summarize(text)}
                        changed = True

                new_blocks.append(block)

            if changed:
                truncated.append({**msg, "content": new_blocks})
            else:
                truncated.append(msg)
        return truncated

    def _drop_old_images(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.drop_old_images:
            return messages
        non_system_indices = [i for i, m in enumerate(messages) if m.get("role") != "system"]
        if len(non_system_indices) <= self.min_recent_messages:
            return messages
        protected: set[int] = set()
        if non_system_indices:
            protected.add(non_system_indices[0])
        protected.update(non_system_indices[-self.min_recent_messages :])
        stripped: List[Dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            content = msg.get("content")
            if idx in protected or not isinstance(content, list):
                stripped.append(msg)
                continue
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") == "image")]
            if len(kept) != len(content):
                stripped.append({**msg, "content": kept})
            else:
                stripped.append(msg)
        return stripped

    def _drop_old_messages(
        self, messages: List[Dict[str, Any]], model: str, target: int
    ) -> List[Dict[str, Any]]:
        non_system_indices = [i for i, m in enumerate(messages) if m.get("role") != "system"]
        if len(non_system_indices) <= self.min_recent_messages:
            return messages

        pairs = self._pair_tool_exchange_indices(messages)
        first_non_system_idx = non_system_indices[0]

        kept_indices: set[int] = set()
        current_tokens = 0

        # Protect the first user/assistant message: it usually carries the task.
        if first_non_system_idx is not None:
            kept_indices.add(first_non_system_idx)
            current_tokens += self._message_token_count(model, messages[first_non_system_idx])
            # If the first message is a tool_use, Anthropic requires its result.
            for partner_idx in pairs.get(first_non_system_idx, set()):
                if partner_idx not in kept_indices:
                    kept_indices.add(partner_idx)
                    current_tokens += self._message_token_count(model, messages[partner_idx])

        for idx in reversed(non_system_indices):
            if idx in kept_indices:
                continue
            partner_indices = pairs.get(idx, set())
            required = {idx} | partner_indices
            new_indices = required - kept_indices
            if not new_indices:
                continue
            msg_tokens = sum(
                self._message_token_count(model, messages[i]) for i in new_indices
            )
            # first_non_system_idx is already seeded, so we require
            # min_recent_messages *additional* recent messages.
            if len(kept_indices) - 1 < self.min_recent_messages or current_tokens + msg_tokens <= target:
                kept_indices.update(required)
                current_tokens += msg_tokens
            else:
                break

        kept_indices = self._enforce_alternation(messages, kept_indices)
        return [messages[i] for i in range(len(messages)) if i in kept_indices]

    def _enforce_alternation(
        self, messages: List[Dict[str, Any]], kept_indices: set[int]
    ) -> set[int]:
        """Remove kept messages that would make two consecutive roles identical.

        Removes the older message of the pair so the newer turn (usually the
        user's current request) is preserved, unless the older message is the
        first non-system message, which is kept by design.
        """
        pairs = self._pair_tool_exchange_indices(messages)
        first_non_system_idx = next(
            (i for i, m in enumerate(messages) if m.get("role") != "system"), None
        )
        sorted_indices = sorted(kept_indices)
        changed = True
        while changed:
            changed = False
            sorted_indices = [i for i in sorted_indices if i in kept_indices]
            for i in range(len(sorted_indices) - 1):
                idx_a = sorted_indices[i]
                idx_b = sorted_indices[i + 1]
                if messages[idx_a].get("role") == messages[idx_b].get("role"):
                    remove_idx = idx_b if idx_a == first_non_system_idx else idx_a
                    to_remove = {remove_idx}
                    to_remove.update(pairs.get(remove_idx, set()))
                    kept_indices.difference_update(to_remove)
                    changed = True
                    break
        return kept_indices

    def _pair_tool_exchange_indices(self, messages: List[Dict[str, Any]]) -> Dict[int, set[int]]:
        tool_uses: Dict[str, int] = {}
        tool_results: Dict[str, int] = {}
        for idx, msg in enumerate(messages):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and "id" in block:
                    tool_uses[block["id"]] = idx
                elif block.get("type") == "tool_result" and "tool_use_id" in block:
                    tool_results[block["tool_use_id"]] = idx
        pairs: Dict[int, set[int]] = {}
        for tool_id, use_idx in tool_uses.items():
            result_idx = tool_results.get(tool_id)
            if result_idx is not None:
                pairs.setdefault(use_idx, set()).add(result_idx)
                pairs.setdefault(result_idx, set()).add(use_idx)
        return pairs

    def _message_token_count(self, model: str, msg: Dict[str, Any]) -> int:
        return self._safe_token_count(
            model, [self._strip_images_for_counting([msg])[0]]
        )

    def _shrink_to_byte_budget(
        self,
        data: Dict[str, Any],
        messages: List[Dict[str, Any]],
        model: str,
    ) -> List[Dict[str, Any]]:
        non_system = [i for i, m in enumerate(messages) if m.get("role") != "system"]
        if len(non_system) <= self.min_recent_messages:
            return messages

        protected: set[int] = set()
        if non_system:
            protected.add(non_system[0])
        protected.update(non_system[-self.min_recent_messages :])

        for idx in non_system[: -self.min_recent_messages]:
            if idx in protected:
                continue
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            kept = [
                b
                for b in content
                if not (isinstance(b, dict) and b.get("type") == "image")
            ]
            if len(kept) != len(content):
                messages[idx] = {**msg, "content": kept}
            if self._estimate_request_bytes(data, messages, model) <= self.max_request_bytes:
                break
        return messages

    @staticmethod
    def _extract_tool_result_text(
        block: Dict[str, Any],
    ) -> tuple[Optional[str], str, List[Any]]:
        if isinstance(block.get("text"), str):
            return block["text"], "text", []
        content = block.get("content")
        if isinstance(content, str):
            return content, "content", []
        if isinstance(content, list):
            parts = []
            non_text = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    non_text.append(item)
            if parts:
                return "\n".join(parts), "content_list", non_text
        return None, "", []

    def _replace_tool_result_text(
        self,
        block: Dict[str, Any],
        path: str,
        summary: str,
        non_text: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        extras = non_text or []
        new_block = dict(block)
        if path == "text":
            new_block["text"] = summary
        elif path == "content":
            new_block["content"] = summary
        elif path == "content_list":
            new_block["content"] = [{"type": "text", "text": summary}] + extras
        return new_block

    def _truncate_string_message(self, msg: Dict[str, Any], text: str) -> Dict[str, Any]:
        return {**msg, "content": self._summarize(text)}

    def _summarize(self, text: str) -> str:
        if len(text) <= self.max_tool_result_chars:
            return text
        # Prefer JSON-aware truncation so structured tool outputs (e.g. `gh`,
        # `kubectl`, MCP servers) stay parseable; a half-cut JSON blob in the
        # conversation poisons the model's context and triggers the
        # "Invalid tool parameters" loop in Claude Code.
        parsed = None
        if len(text) <= 1_000_000:
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
        was_json = parsed is not None and isinstance(parsed, (dict, list))
        if was_json:
            try:
                self._truncate_json_strings(parsed, self.max_tool_result_chars)
            except RecursionError:
                parsed = None
            if parsed is not None:
                try:
                    dumped = json.dumps(parsed, ensure_ascii=False)
                    if len(dumped) <= self.max_tool_result_chars:
                        return dumped
                except (TypeError, ValueError):
                    pass
                # Structured fallback: keep first/last elements so the output is
                # still valid JSON rather than a half-cut string.
                try:
                    pruned = self._prune_json_to_budget(parsed, self.max_tool_result_chars)
                except RecursionError:
                    pruned = None
                if pruned is not None:
                    try:
                        dumped = json.dumps(pruned, ensure_ascii=False)
                        if len(dumped) <= self.max_tool_result_chars:
                            return dumped
                    except (TypeError, ValueError):
                        pass
            # The original was JSON but every structured fallback failed;
            # return a valid JSON placeholder instead of invalid head/tail.
            placeholder = json.dumps({"truncated": True, "original_length": len(text)})
            if len(placeholder) <= self.max_tool_result_chars:
                return placeholder
        truncated = len(text) - self.max_tool_result_chars
        import hashlib
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if not hasattr(ClaudeAwareCompression, "_truncated_cache"):
            ClaudeAwareCompression._truncated_cache = {}
        cache = ClaudeAwareCompression._truncated_cache
        if len(cache) >= 500:
            cache.pop(next(iter(cache)), None)
        cache[sha] = text
        marker = f"\n\n... ({truncated} characters truncated; sha256_hash: {sha}) ...\n\n"
        budget = max(0, self.max_tool_result_chars - len(marker))
        half = budget // 2
        prefix = text[:half]
        suffix = text[-half:] if half else ""
        return f"{prefix}{marker}{suffix}"

    def _truncate_json_strings(self, data: Any, budget: int) -> None:
        """Recursively shrink long string leaves of a parsed JSON value.

        Operates in-place. Strings longer than ``max(64, budget // 4)`` are
        replaced by a head + ellipsis + tail window; surrounding keys, brackets,
        and non-string scalars are left intact so the re-serialised value is
        still valid JSON.
        """
        window = max(64, budget // 4)
        if isinstance(data, dict):
            for key in list(data.keys()):
                value = data[key]
                if isinstance(value, str) and len(value) > window:
                    head = value[:window]
                    tail_window = min(window, len(value) - window)
                    tail = value[-tail_window:] if tail_window else ""
                    truncated = len(value) - window - tail_window
                    data[key] = (
                        f"{head}\n... ({truncated} chars truncated) ...\n{tail}"
                    )
                elif isinstance(value, (dict, list)):
                    self._truncate_json_strings(value, budget)
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, str) and len(item) > window:
                    head = item[:window]
                    tail_window = min(window, len(item) - window)
                    tail = item[-tail_window:] if tail_window else ""
                    truncated = len(item) - window - tail_window
                    data[idx] = (
                        f"{head}\n... ({truncated} chars truncated) ...\n{tail}"
                    )
                elif isinstance(item, (dict, list)):
                    self._truncate_json_strings(item, budget)

    def _prune_json_to_budget(self, data: Any, budget: int) -> Any:
        """Truncate wide arrays/dicts so the re-serialised value stays valid JSON.

        Keeps the first and last ``keep`` elements and replaces the middle with
        an ellipsis string. ``keep`` is reduced until the result fits, so the
        output is always parseable (unlike the raw head/tail fallback).
        """

        def _fits(value: Any) -> bool:
            try:
                return len(json.dumps(value, ensure_ascii=False)) <= budget
            except (TypeError, ValueError):
                return False

        if isinstance(data, list):
            if len(data) <= 2:
                return data if _fits(data) else data[:1] if data else data
            keep = max(1, budget // 200)
            while keep > 0:
                middle = f"... ({len(data) - 2 * keep} items truncated) ..."
                pruned = data[:keep] + [middle] + data[-keep:]
                if _fits(pruned):
                    return pruned
                keep //= 2
            fallback = [data[0], "...", data[-1]]
            return fallback if _fits(fallback) else [data[0]]
        if isinstance(data, dict):
            keys = list(data.keys())
            if len(keys) <= 2:
                return data if _fits(data) else ({keys[0]: data[keys[0]]} if keys else {})
            keep = max(1, budget // 200)
            while keep > 0:
                kept = keys[:keep] + keys[-keep:]
                pruned = {k: data[k] for k in kept}
                if _fits(pruned):
                    return pruned
                keep //= 2
            fallback = {keys[0]: data[keys[0]], keys[-1]: data[keys[-1]], "...": "..."}
            if _fits(fallback):
                return fallback
            single = {keys[0]: data[keys[0]]}
            return single if _fits(single) else {"...": "truncated"}
        return data


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


claude_aware_compression = ClaudeAwareCompression(
    enabled=_env_bool("CLAUDE_COMPRESSION_ENABLED", True),
    compression_trigger=_env_int("CLAUDE_COMPRESSION_TRIGGER", 100),
    compression_target=_env_int("CLAUDE_COMPRESSION_TARGET", 50),
    max_tool_result_chars=_env_int("CLAUDE_COMPRESSION_MAX_TOOL_RESULT_CHARS", 8000),
    min_recent_messages=_env_int("CLAUDE_COMPRESSION_MIN_RECENT_MESSAGES", 6),
    image_token_estimate=_env_int("CLAUDE_COMPRESSION_IMAGE_TOKEN_ESTIMATE", _DEFAULT_IMAGE_TOKEN_ESTIMATE),
    max_request_bytes=_env_int("CLAUDE_COMPRESSION_MAX_REQUEST_BYTES", _DEFAULT_MAX_REQUEST_BYTES),
    drop_old_images=_env_bool("CLAUDE_COMPRESSION_DROP_OLD_IMAGES", True),
    drop_old_messages=_env_bool("CLAUDE_COMPRESSION_DROP_OLD_MESSAGES", True),
)
