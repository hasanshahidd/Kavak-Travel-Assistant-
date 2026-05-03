"""Provider-agnostic LLM substrate, prompt loading, tracing, citation verification."""

from app.llm.client import (
    AnthropicClient,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMTransientError,
    LLMUsage,
    LLMValidationError,
    MockClient,
    OpenAIClient,
    compute_cost,
    get_llm_client,
)
from app.llm.embeddings import (
    EMBEDDING_DIM,
    EmbeddingsClient,
    MockEmbeddingsClient,
    OpenAIEmbeddingsClient,
    get_embeddings_client,
)
from app.llm.prompt_loader import (
    PromptFrontmatter,
    PromptLoadError,
    PromptTemplate,
    clear_cache,
    load_prompt,
    load_prompt_from_text,
    parse_prompt_text,
)
from app.llm.tracing import Tracer, read_trace, redact_pii
from app.llm.verifier import REFUSAL_TEMPLATE, VerificationReport, verify_citations

__all__ = [
    "EMBEDDING_DIM",
    "REFUSAL_TEMPLATE",
    "AnthropicClient",
    "EmbeddingsClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMTransientError",
    "LLMUsage",
    "LLMValidationError",
    "MockClient",
    "MockEmbeddingsClient",
    "OpenAIClient",
    "OpenAIEmbeddingsClient",
    "PromptFrontmatter",
    "PromptLoadError",
    "PromptTemplate",
    "Tracer",
    "VerificationReport",
    "clear_cache",
    "compute_cost",
    "get_embeddings_client",
    "get_llm_client",
    "load_prompt",
    "load_prompt_from_text",
    "parse_prompt_text",
    "read_trace",
    "redact_pii",
    "verify_citations",
]
