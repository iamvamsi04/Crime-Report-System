# Project configuration template. The application still reads its existing .env.
# Map these sections to your private SDK's documented YAML schema and loader.
# *_env fields name environment variables containing secrets; they are not
# automatically resolved by YAML. Your configuration loader must resolve them.
# Relative storage paths are relative to the project root, not this directory.

app:
  api_host: "127.0.0.1"
  api_port: 8000
  api_url: "http://127.0.0.1:8000"

model:
  provider: "azure_openai"
  name: "gpt-5.4-mini"
  azure_endpoint: "https://YOUR-RESOURCE.openai.azure.com"
  azure_deployment: "REPLACE_WITH_YOUR_AZURE_DEPLOYMENT"
  azure_api_version: "REPLACE_WITH_YOUR_SUPPORTED_API_VERSION"
  api_key_env: "AZURE_OPENAI_API_KEY"
  # Empty preserves the deployment's default reasoning behavior.
  reasoning_effort: ""

access:
  api_key_env: "BILLING_API_KEY"
  admin_key_env: "BILLING_ADMIN_API_KEY"
  cashier_key_env: "BILLING_CASHIER_API_KEY"

storage:
  billing_db: "database/billing.db"
  context_db: "database/context.db"
  invoice_dir: "database/invoices"

execution:
  chat_timeout_seconds: 180
  max_tool_iterations: 12
  sql_max_rows: 200

billing:
  return_window_days: 30
  cashier_refund_limit: "100.00"
  cashier_refund_currency: "USD"

# Existing local operational dashboard and background storage.
observability:
  enabled: true
  db: "database/observability.db"
  queue_size: 1000
  retention_days: 90

# Private SDK telemetry settings, separate from the local dashboard above.
# Set exporter to a value supported by your SDK before enabling SDK telemetry.
# null is an unconfigured placeholder, not a claimed SDK exporter option.
telemetry:
  enable: false
  exporter: null
  collect_system_metrics: false
