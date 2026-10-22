"""The providers of an LLM profile that the OpenAI-compatible chat client serves.

A profile names its provider (``[llm.profiles.<name>] provider``).  The
providers ``ollama`` and ``openai_compatible`` need nothing more than a
URL.  A provider of this registry also gives the defaults and the parts of
a request that its API needs.  WHY a registry and not a branch in the
client: a new provider is one record here, and the client, the config
check and the documentation read the same record.

The values come from the measurements of 2026-10-20
(``plans/2026-10-20-volba-modelu-a-kontextu.md``):

* Each provider turns the reasoning of the model off.  A model that
  "thinks" bills the reasoning as output, and the answer of the analysis
  did not get better with it.
* OpenRouter sorts the endpoints by price, and keeps out the
  quantizations below 8 bits by default: the cheapest endpoint of DeepSeek
  V4.1 Flash runs ``fp4``, and its answers got 3.1 of 5, against 4.2 for
  ``fp8``.  ``unknown`` stays in: OpenRouter gives it for the models of
  OpenAI, and a filter of ``bf16`` and ``fp8`` only left no endpoint for
  them (HTTP 404, measured 2026-10-20).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderSpec:
    """One provider of the registry.

    Attributes:
        name: The value of ``provider`` in a profile.
        label: The name for a person.
        base_url: The URL of the API when the profile names none.
        key_env: The environment variable of the API key when the profile
            names none (``api_key_env``).
        reasoning_off: The part of the request that turns the reasoning
            of the model off.
        reasoning_low: The part of the request for a model that refuses
            ``reasoning_off`` (HTTP 400): the lowest reasoning, without its
            text in the answer.  Empty when the provider has no other form.
        options: The keys of the table ``routing`` of a profile, with their
            type for the config check (``settings._expected_type``).
        default_routing: The values of ``routing`` that apply when the
            profile does not set them.
        usage_in_request: The part of the request that asks for the usage
            and the cost in the answer.
        default_embed_routing: The values of ``embed_routing`` that apply to
            an embedding request when the profile does not set them.  Empty
            for a provider without an embedding endpoint.
        recommended_model: The model that the setup offers first.  It
            comes from the measurement of 2026-10-20; the setup does not
            show the grades (decision of the user: an internal rating).
    """

    name: str
    label: str
    base_url: str
    key_env: str
    reasoning_off: Mapping[str, object]
    reasoning_low: Mapping[str, object] = field(default_factory=dict)
    options: Mapping[str, str] = field(default_factory=dict)
    default_routing: Mapping[str, object] = field(default_factory=dict)
    usage_in_request: Mapping[str, object] = field(default_factory=dict)
    recommended_model: str = ""
    default_embed_routing: Mapping[str, object] = field(default_factory=dict)

    def request_fields(self, routing: Mapping[str, object] | None, *, low_reasoning: bool = False) -> dict[str, object]:
        """Return the fields that a chat request to this provider adds to the OpenAI payload."""
        fields: dict[str, object] = dict(self.reasoning_low if low_reasoning and self.reasoning_low else self.reasoning_off)
        fields.update(self.usage_in_request)
        if self.options:
            fields["provider"] = {**self.default_routing, **(routing or {})}
        return fields


#: The values of ``routing.sort`` of OpenRouter.
OPENROUTER_SORTS = ("price", "throughput", "latency")

#: The values of ``routing.data_collection`` of OpenRouter.
OPENROUTER_DATA_COLLECTION = ("allow", "deny")

#: The values of ``routing.quantizations`` of OpenRouter (its provider routing documentation).
OPENROUTER_QUANTIZATIONS = (
    "int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown",
)

#: The default filter: each quantization of 8 bits or more, and unknown.
OPENROUTER_DEFAULT_QUANTIZATIONS = ("int8", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown")

PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            name="openrouter",
            label="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            key_env="OPENROUTER_API_KEY",
            reasoning_off={"reasoning": {"enabled": False}},
            # Some models cannot turn the reasoning off (gpt-oss, for example).
            reasoning_low={"reasoning": {"effort": "low", "exclude": True}},
            options={"sort": "str", "quantizations": "list", "zdr": "bool", "data_collection": "str"},
            default_routing={"sort": "price", "quantizations": list(OPENROUTER_DEFAULT_QUANTIZATIONS)},
            usage_in_request={"usage": {"include": True}},
            recommended_model="openai/gpt-6-luna",
            # The embeddings send the code of each symbol and each search
            # query: only hosts that keep no data (decision of the user,
            # 2026-10-22).  A model without such a host gets HTTP 404.
            default_embed_routing={"zdr": True, "data_collection": "deny"},
        ),
        ProviderSpec(
            name="deepseek",
            label="DeepSeek",
            base_url="https://api.deepseek.com",
            key_env="DEEPSEEK_API_KEY",
            reasoning_off={"thinking": {"type": "disabled"}},
            # The name of the model list of the API (GET /models,
            # 2026-10-22); "deepseek-v4-flash" answered too, but the list
            # does not hold it, thus its context was not known.
            recommended_model="deepseek-flash",
        ),
    )
}


def provider_spec(name: str) -> ProviderSpec | None:
    """Return the record of the provider *name*, or None for ``ollama``, ``openai_compatible`` and an unknown name."""
    return PROVIDERS.get(name)
