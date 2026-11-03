"""YAML-based settings preserving the existing application's setting names.

Replace load_yaml_config() with your private SDK's YAML-loading call when ready.
The supplied reader works with PyYAML; it does not claim to call the private SDK.
YAML controls ordinary settings. The .env/environment supplies referenced secrets.
"""
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'config' / 'app.yaml'
# These select exact YAML entries; do not silently select the first service/agent.
SERVICE_NAME = 'billing'
AGENT_NAME = 'BillingAgent'

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass


def load_yaml_config(path: Path) -> Mapping:
    """SDK connection point: return the loaded YAML as a Python mapping.

    Replace the body below with your already-working SDK loader. If it returns
    an SDK settings object, convert it using that SDK's documented mapping API.
    """
    import yaml
    with path.open(encoding='utf-8') as stream:
        return yaml.safe_load(stream)


CONFIG = load_yaml_config(CONFIG_PATH)
if not isinstance(CONFIG, Mapping):
    raise ValueError('Configuration must contain a YAML mapping at its root.')


def _mapping(parent, key, location):
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f'{location} must contain a mapping.')
    return value


SERVICES = _mapping(CONFIG, 'services', 'services')
SERVICE_CONFIG = _mapping(SERVICES, SERVICE_NAME, f'services.{SERVICE_NAME}')
AGENTS = _mapping(SERVICE_CONFIG, 'agents', f'services.{SERVICE_NAME}.agents')
AGENT_CONFIG = _mapping(AGENTS, AGENT_NAME, f'services.{SERVICE_NAME}.agents.{AGENT_NAME}')
SERVICE_VERSION = SERVICE_CONFIG.get('version')
if not isinstance(SERVICE_VERSION, str) or not SERVICE_VERSION.strip():
    raise ValueError('Service version must be a nonempty quoted string.')
AGENT_FRAMEWORK = AGENT_CONFIG.get('framework', 'autogen')
if AGENT_FRAMEWORK != 'autogen':
    raise ValueError('BillingAgent currently requires framework: autogen.')
APP_CONFIG = _mapping(CONFIG, 'app', 'app')
LLM_REFERENCE = _mapping(AGENT_CONFIG, 'llm', f'services.{SERVICE_NAME}.agents.{AGENT_NAME}.llm')
LLM_IDENTIFIER = LLM_REFERENCE.get('identifier')
if not isinstance(LLM_IDENTIFIER, str) or not LLM_IDENTIFIER.strip():
    raise ValueError('Agent llm.identifier must reference an entry in the top-level llm section.')
LLM_CONFIG = _mapping(_mapping(CONFIG, 'llm', 'llm'), LLM_IDENTIFIER, f'llm.{LLM_IDENTIFIER}')


def _value(section, key, default):
    if section == 'llm':
        return LLM_CONFIG.get(key, default)
    if section == 'app':
        return APP_CONFIG.get(key, default)
    if section in ('telemetry', 'tools_fabric'):
        values = APP_CONFIG.get(section, {})
        if not isinstance(values, Mapping):
            raise ValueError(f'app.{section} must be a mapping.')
        return values.get(key, default)
    source = AGENT_CONFIG if section == 'execution' else SERVICE_CONFIG
    values = source.get(section, {})
    if not isinstance(values, Mapping):
        raise ValueError(f'Configuration section {section} must be a mapping.')
    return values.get(key, default)


def _text(section, key, default=''):
    value = _value(section, key, default)
    if not isinstance(value, str):
        raise ValueError(f'{section}.{key} must be text; quote numeric-looking strings in YAML.')
    return value.strip()


def _integer(section, key, default, minimum=0, maximum=None):
    value = _value(section, key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f'{section}.{key} must be an integer.')
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f'{section}.{key} must be an integer.') from None
    if number < minimum or maximum is not None and number > maximum:
        raise ValueError(f'{section}.{key} is outside its allowed range.')
    return number


def _boolean(section, key, default):
    value = _value(section, key, default)
    if type(value) is not bool:
        raise ValueError(f'{section}.{key} must be YAML true or false, without quotes.')
    return value


def _secret(section, key, default_env):
    name = _text(section, key, default_env)
    if not name:
        raise ValueError(f'{section}.{key} must name the environment variable containing the secret.')
    return os.environ.get(name, '')


def path_setting(name: str, default: str) -> Path:
    # Preserve this public helper for existing callers.
    names = {'BILLING_DB': ('storage', 'billing_db'),
             'CONTEXT_DB': ('storage', 'context_db'),
             'INVOICE_DIR': ('storage', 'invoice_dir'),
             'OBSERVABILITY_DB': ('observability', 'db')}
    section, key = names[name]
    value = _text(section, key, default)
    if not value:
        raise ValueError(f'{section}.{key} must contain a path.')
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


BILLING_DB = path_setting('BILLING_DB', 'database/billing.db')
CONTEXT_DB = path_setting('CONTEXT_DB', 'database/context.db')
INVOICE_DIR = path_setting('INVOICE_DIR', 'database/invoices')

MODEL_PROVIDER = LLM_IDENTIFIER
AZURE_OPENAI_ENDPOINT = _text('llm', 'api_endpoint').rstrip('/')
AZURE_OPENAI_DEPLOYMENT = _text('llm', 'model_deployment')
AZURE_OPENAI_MODEL = _text('llm', 'model_name')
AZURE_OPENAI_API_VERSION = _text('llm', 'api_version')
# A blank YAML key uses the existing local environment secret.
AZURE_OPENAI_API_KEY = _text('llm', 'api_key') or os.environ.get('AZURE_OPENAI_API_KEY', '').strip()
AZURE_REASONING_EFFORT = _text('llm', 'reasoning_effort').lower()

API_KEY = _secret('access', 'api_key_env', 'BILLING_API_KEY')
ADMIN_API_KEY = _secret('access', 'admin_key_env', 'BILLING_ADMIN_API_KEY')
CASHIER_API_KEY = _secret('access', 'cashier_key_env', 'BILLING_CASHIER_API_KEY')

# Keep policy money/days as strings for existing Decimal and MCP environment use.
RETURN_WINDOW_DAYS = str(_integer('billing', 'return_window_days', 30))
_limit = _value('billing', 'cashier_refund_limit', '100.00')
if isinstance(_limit, bool) or not isinstance(_limit, (str, int, float)):
    raise ValueError('billing.cashier_refund_limit must be a nonnegative money value.')
try:
    _amount = Decimal(str(_limit))
    if not _amount.is_finite() or not 0 <= _amount <= Decimal('99999999.99') or _amount != _amount.quantize(Decimal('.01')):
        raise ValueError('Invalid refund limit.')
except (InvalidOperation, ValueError):
    raise ValueError('billing.cashier_refund_limit must be nonnegative money with at most two decimal places.') from None
CASHIER_REFUND_LIMIT = format(_amount, '.2f')
CASHIER_REFUND_CURRENCY = _text('billing', 'cashier_refund_currency', 'USD').upper()
if len(CASHIER_REFUND_CURRENCY) != 3 or not CASHIER_REFUND_CURRENCY.isascii() or not CASHIER_REFUND_CURRENCY.isalpha():
    raise ValueError('billing.cashier_refund_currency must be a three-letter currency code.')

SQL_MAX_ROWS = _integer('execution', 'sql_max_rows', 200, 1, 1000)
CHAT_TIMEOUT = _integer('execution', 'chat_timeout_seconds', 180, 1)
MAX_TOOL_ITERATIONS = _integer('execution', 'max_tool_iterations', 12, 2, 20)

API_HOST = _text('app', 'api_host', '127.0.0.1')
API_PORT = _integer('app', 'api_port', 8000, 1, 65535)
API_URL = _text('app', 'api_url', 'http://127.0.0.1:8000').rstrip('/')
APP_NAME = _text('app', 'name', 'BillingAgentApp')
APP_VERSION = _text('app', 'version', '1.0.0')
APP_ENVIRONMENT = _text('app', 'environment', 'dev')
TOOLS_FABRIC_ENDPOINT = _text('tools_fabric', 'endpoint')
TOOLS_FABRIC_TIMEOUT = _integer('tools_fabric', 'timeout', 30, 1)
TOOLS_FABRIC_MODE = _text('tools_fabric', 'mode', 'streamble')

OBSERVABILITY_ENABLED = _boolean('observability', 'enabled', True)
OBSERVABILITY_DB = path_setting('OBSERVABILITY_DB', str(BILLING_DB.parent / 'observability.db'))
if OBSERVABILITY_DB.resolve() in {BILLING_DB.resolve(), CONTEXT_DB.resolve()}:
    raise ValueError('observability.db must be separate from the billing and context databases.')
OBSERVABILITY_QUEUE_SIZE = _integer('observability', 'queue_size', 1000, 1, 10000)
OBSERVABILITY_RETENTION_DAYS = _integer('observability', 'retention_days', 90, 1, 3650)

# SDK-only values: the SDK's accepted exporter format still needs local mapping.
TELEMETRY_ENABLED = _boolean('telemetry', 'enable', False)
TELEMETRY_EXPORTER = _value('telemetry', 'exporter', None)
COLLECT_SYSTEM_METRICS = _boolean('telemetry', 'collect_system_metrics', False)
if TELEMETRY_ENABLED and TELEMETRY_EXPORTER is None:
    raise ValueError('Configure telemetry.exporter according to your SDK before enabling SDK telemetry.')

AZURE_FIELDS = ('AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY',
                'AZURE_OPENAI_DEPLOYMENT', 'AZURE_OPENAI_MODEL', 'AZURE_OPENAI_API_VERSION')


def azure_configuration_error():
    """Validate local configuration without revealing secrets or calling Azure."""
    from urllib.parse import urlsplit
    if MODEL_PROVIDER != 'azure_openai':
        return 'BillingAgent llm.identifier must reference azure_openai.'
    if AZURE_REASONING_EFFORT not in ('', 'none', 'low', 'medium', 'high', 'xhigh'):
        return 'llm.reasoning_effort must be blank, none, low, medium, high or xhigh.'
    missing = [name for name in AZURE_FIELDS if not globals()[name]]
    if missing:
        return 'Set the YAML settings or referenced environment secrets for: ' + ', '.join(missing)
    try:
        endpoint = urlsplit(AZURE_OPENAI_ENDPOINT)
    except ValueError:
        return 'llm.azure_openai.api_endpoint must be a valid HTTPS resource URL.'
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ('', '/')):
        return 'llm.azure_openai.api_endpoint must be an HTTPS resource URL without a path, query or credentials.'
    return None
