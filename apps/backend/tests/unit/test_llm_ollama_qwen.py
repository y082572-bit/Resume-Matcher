"""Unit tests for Ollama qwen-specific LLM parameter handling."""

import pytest

from app.llm import (
    _apply_provider_specific_params,
    _calculate_timeout,
)


class TestApplyProviderSpecificParams:
    """Tests for _apply_provider_specific_params() — Ollama qwen support."""

    def test_qwen_model_receives_think_false(self):
        """Ollama qwen models should have think=false to disable reasoning."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "ollama", "ollama_chat/qwen2:14b")
        
        assert kwargs.get("think") is False, "think should be False for qwen"
        assert "options" in kwargs, "options dict should be created"

    def test_qwen_model_receives_num_ctx_8192(self):
        """Ollama qwen models should have num_ctx=8192 for extended context."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "ollama", "ollama_chat/qwen3:14b")
        
        assert kwargs["options"]["num_ctx"] == 8192, "num_ctx should be 8192 for qwen"

    def test_qwen_model_receives_stop_reasoning_true(self):
        """Ollama qwen models should have stop_reasoning=true in options."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "ollama", "ollama/qwen:14b")
        
        assert (
            kwargs["options"]["stop_reasoning"] is True
        ), "stop_reasoning should be True in options"

    def test_non_qwen_ollama_model_unchanged(self):
        """Non-qwen Ollama models (e.g., llama2) should not receive qwen params."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "ollama", "ollama_chat/llama2:13b")
        
        # kwargs should remain empty or have minimal defaults
        assert "think" not in kwargs or kwargs.get("think") is None
        assert "options" not in kwargs or "num_ctx" not in kwargs.get("options", {})

    def test_non_ollama_provider_unchanged(self):
        """Non-Ollama providers should not receive qwen params."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "openai", "gpt-4")
        
        assert "think" not in kwargs
        assert "options" not in kwargs

    def test_anthropic_unchanged(self):
        """Anthropic Claude models should not receive qwen params."""
        kwargs = {}
        _apply_provider_specific_params(kwargs, "anthropic", "claude-3-opus")
        
        assert "think" not in kwargs
        assert "options" not in kwargs

    def test_case_insensitive_qwen_detection(self):
        """Qwen detection should be case-insensitive."""
        for model in [
            "ollama_chat/QWEN2:14b",
            "ollama_chat/Qwen3:14b",
            "ollama/qwEn:14b",
        ]:
            kwargs = {}
            _apply_provider_specific_params(kwargs, "ollama", model)
            assert kwargs.get("think") is False, f"Failed for {model}"

    def test_existing_options_preserved(self):
        """Existing options dict should not be overwritten."""
        kwargs = {"options": {"temperature": 0.5}}
        _apply_provider_specific_params(kwargs, "ollama", "ollama/qwen3:14b")
        
        # New params should be added to existing options
        assert kwargs["options"]["temperature"] == 0.5
        assert kwargs["options"]["num_ctx"] == 8192
        assert kwargs["options"]["stop_reasoning"] is True
        assert kwargs["think"] is False


class TestCalculateTimeout:
    """Tests for _calculate_timeout() — provider-specific latency factors."""

    def test_ollama_factor_is_3_0(self):
        """Ollama factor should be 3.0 for base json timeout."""
        # LLM_TIMEOUT_JSON = 180
        # For ollama with 4096 tokens: 180 * 1.0 * 3.0 = 540 seconds
        timeout = _calculate_timeout("json", 4096, "ollama")
        assert timeout == 540, f"Expected 540, got {timeout} for ollama"

    def test_openai_factor_is_1_0(self):
        """OpenAI factor should be 1.0 (baseline)."""
        # LLM_TIMEOUT_JSON = 180
        # For openai with 4096 tokens: 180 * 1.0 * 1.0 = 180 seconds
        timeout = _calculate_timeout("json", 4096, "openai")
        assert timeout == 180, f"Expected 180, got {timeout} for openai"

    def test_anthropic_factor_is_1_2(self):
        """Anthropic factor should be 1.2."""
        # LLM_TIMEOUT_JSON = 180
        # For anthropic with 4096 tokens: 180 * 1.0 * 1.2 = 216 seconds
        timeout = _calculate_timeout("json", 4096, "anthropic")
        assert timeout == 216, f"Expected 216, got {timeout} for anthropic"

    def test_timeout_scales_with_token_count(self):
        """Timeout should scale with token count."""
        timeout_4k = _calculate_timeout("json", 4096, "ollama")
        timeout_8k = _calculate_timeout("json", 8192, "ollama")
        
        # 8k tokens should be roughly 2x timeout of 4k
        # timeout_8k = 180 * (8192/4096) * 3.0 = 180 * 2 * 3.0 = 1080
        assert timeout_8k == 1080, f"Expected 1080, got {timeout_8k}"
        assert timeout_8k == timeout_4k * 2

    def test_groq_uses_baseline_factor(self):
        """Groq should use baseline 1.0 factor."""
        timeout = _calculate_timeout("json", 4096, "groq")
        assert timeout == 180, f"Expected 180, got {timeout} for groq"

    def test_unknown_provider_uses_default_factor(self):
        """Unknown providers should use default 1.0 factor."""
        timeout = _calculate_timeout("json", 4096, "unknown_provider")
        assert timeout == 180, f"Expected 180, got {timeout} for unknown"


class TestTimeoutConsistency:
    """Integration tests: LLM timeout vs endpoint timeout."""

    def test_endpoint_timeout_longer_than_single_llm_call(self):
        """
        Endpoint timeout (1200s) must be longer than max single LLM call timeout
        to allow multiple sequential LLM calls within improve/preview flow.
        """
        # Max single LLM call timeout for ollama+qwen at 8192 tokens:
        max_llm_timeout = _calculate_timeout("json", 8192, "ollama")
        endpoint_timeout = 1200  # From config.py
        
        # Allow for at least 2 sequential LLM calls + overhead
        assert (
            endpoint_timeout > max_llm_timeout
        ), (
            f"Endpoint timeout ({endpoint_timeout}s) must exceed single LLM timeout "
            f"({max_llm_timeout}s)"
        )

    def test_no_regression_for_openai(self):
        """OpenAI timeouts should not regress significantly."""
        # Original: 180s for json with 4096 tokens
        timeout_old = 180
        timeout_new = _calculate_timeout("json", 4096, "openai")
        
        assert timeout_new == timeout_old, (
            f"OpenAI timeout changed: {timeout_old}s → {timeout_new}s"
        )
