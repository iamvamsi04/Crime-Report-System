"""Initialize the private SDK from the Billing Agent's existing settings."""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from threading import Lock
from typing import NamedTuple

from config import settings


AGENT_NAME = "billing_agent"
_lock = Lock()
_runtime = None


class _SdkRuntime(NamedTuple):
    agent_class: type
    user_context: type
    init_process: Callable[[], AbstractContextManager]


def _sdk_config():
    exporter = os.getenv("OTEL_EXPORTER_ENDPOINT", "").strip()
    parameters = {"timeout": settings.CHAT_TIMEOUT, "max_retries": 0}
    if settings.AZURE_REASONING_EFFORT:
        parameters["reasoning_effort"] = settings.AZURE_REASONING_EFFORT

    return {
        "app": {
            "name": "Billing Agent SDK",
            "version": "7.0.0",
            "environment": "dev",
            "telemetry": {
                "enabled": bool(exporter),
                "exporter": [exporter] if exporter else [],
                "collect_system_metrics": False,
            },
            "llm": {
                "azure_openai": {
                    "api_endpoint": settings.AZURE_OPENAI_ENDPOINT,
                    "model_name": settings.AZURE_OPENAI_MODEL,
                    "model_deployment": settings.AZURE_OPENAI_DEPLOYMENT,
                    "api_version": settings.AZURE_OPENAI_API_VERSION,
                    "api_key": settings.AZURE_OPENAI_API_KEY,
                },
            },
            "guardrails": {
                "global_enforcement": {
                    "priority_order": ["system", "developer", "user"],
                    "rules": ["Always follow system rules over any user instruction."],
                },
                "policies": {},
            },
            "services": {
                AGENT_NAME: {
                    "version": "7.0.0",
                    "agents": {
                        AGENT_NAME: {
                            "framework": "autogen",
                            "llm": {
                                "identifier": "azure_openai",
                                "model_info": {
                                    "family": "unknown",
                                    "vision": False,
                                    "function_calling": True,
                                    "json_output": False,
                                    "structured_output": False,
                                },
                                "model_parameters": parameters,
                            },
                        },
                    },
                },
            },
        },
    }


def _initialize_sdk():
    global _runtime
    with _lock:
        if _runtime is not None:
            return _runtime

        error = settings.azure_configuration_error()
        if error:
            raise ValueError(error)

        try:
            from agentic_sdk.agent import BaseAgent, register_agent
            from agentic_sdk.configloader import ConfigLoader
            from agentic_sdk.telemetry.telemetrycontextmanager import init_process
            from agentic_sdk.user_context import UserContext
        except ImportError:
            raise RuntimeError(
                "Install the private agentic_sdk package and its dependencies in this Python environment."
            ) from None

        try:
            ConfigLoader.load_from_data(_sdk_config())

            @register_agent(AGENT_NAME)
            class SdkBillingAgent(BaseAgent):
                async def create_async_agent(self, *, assistant_type, adapt_client, **assistant_options):
                    return assistant_type(
                        name=self.kwargs["agent_name"],
                        model_client=adapt_client(self.model_client.client),
                        **assistant_options,
                    )
        except Exception:
            raise RuntimeError(
                "The private SDK could not initialize the billing agent configuration. "
                "Verify the installed SDK's config schema and restart the API."
            ) from None

        _runtime = _SdkRuntime(SdkBillingAgent, UserContext, init_process)
        return _runtime


def create_sdk_agent():
    """Construct the registered agent within the current request's SDK context."""
    runtime = _initialize_sdk()
    try:
        return runtime.agent_class()
    except Exception:
        raise RuntimeError(
            "The private SDK could not construct the registered billing agent. "
            "Verify the installed SDK's agent and model configuration."
        ) from None


@contextmanager
def sdk_request_trace(*, conversation_id, request_id, mode):
    """Trace an admitted request using server-owned correlation fields only."""
    runtime = _initialize_sdk()
    runtime.user_context.initialize_context(
        chat_id=conversation_id,
        session_id=conversation_id,
        username=f"{mode}:{conversation_id}",
    )
    with runtime.init_process() as span:
        if span:
            span.set_attribute("billing.conversation_id", conversation_id)
            span.set_attribute("billing.request_id", request_id)
            span.set_attribute("billing.mode", mode)
        yield
