"""Run from the billing project root: python test_sdk.py.

Set the private ConfigLoader import in config/settings.py first.
This checks configuration only; it does not call a model or start MCP.
"""


def main():
    # settings owns SDK loading and get_agent_config("BillingAgent").
    # Importing the actual agent class is unnecessary for this configuration test.
    from config import settings

    print('Configuration file:', settings.CONFIG_PATH)
    print('Service:', settings.SERVICE_NAME)
    print('Agent:', settings.AGENT_NAME)
    print('SDK agent configuration type:', type(settings.AGENT_CONFIG).__name__)
    print('Framework:', settings.AGENT_FRAMEWORK)
    print('LLM identifier:', settings.LLM_IDENTIFIER)
    print('Model:', settings.AZURE_OPENAI_MODEL)
    print('Chat timeout:', settings.CHAT_TIMEOUT)
    print('SDK configuration loaded successfully. No model or MCP calls were made.')


if __name__ == '__main__':
    main()
