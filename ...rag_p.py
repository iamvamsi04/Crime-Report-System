# Import asynchronous locks, tasks, queues, cancellation and timeout handling.
import asyncio
# Import constant-time credential or digest comparisons.
import hmac
# Import server-side diagnostic logging.
import logging
# Import JSON encoding/decoding for API events and saved structured metadata.
import json
# Measure request and queue durations using a monotonic clock.
from time import perf_counter
# Import context managers that close database connections or manage API lifespan.
from contextlib import asynccontextmanager, closing
# Validate and normalize the structured UUID correlation IDs supplied to the API.
from uuid import UUID
# Import bounded role/type annotations.
from typing import Literal
# Import HTTP routing, dependency-based authorization and validated errors.
from fastapi import FastAPI, Depends, Header, HTTPException
# Serve protected invoice files and stream chat/progress events over HTTP.
from fastapi.responses import FileResponse, StreamingResponse
# Return structured JSON for API validation/error responses.
from fastapi.responses import JSONResponse
# Recognize FastAPI input-validation failures and return the application's safe error shape.
from fastapi.exceptions import RequestValidationError
# Import typed, length-bounded request models.
from pydantic import BaseModel, ConfigDict, Field
# Import typed Azure/OpenAI failure handling.
from openai import APIConnectionError, APITimeoutError, APIStatusError
# Import the existing application settings.
from config import settings
from agent.sdk_runtime import sdk_request_trace
# Persist conversation history, customer shopping memory and durable request checkpoints.
from context import context_manager as context
# Extract visible English without rendering the internal reply/state envelope twice.
from context.reply_text import plain_reply
# Validate table attachments before returning or saving them.
from context.tables import sanitize_tables
# Validate full analytics report attachments independently of model prose.
from context.reports import sanitize_reports
# Initialize and migrate billing storage during application startup.
from database.connection import initialize as initialize_billing
# Read current billing/customer/address facts through the configured SQLite connection helper.
from database.connection import connect
# Resolve a validated invoice identifier to its generated PDF location.
from database.invoice import invoice_path
# Import customer authentication helpers kept outside model prompts.
from api import customer_auth
# Capture safe per-request latency, usage and guardrail observations.
from observability import capture as telemetry
# Attach the separately authorized operational dashboard API routes.
from observability.api import create_router as observability_router

# Create a module-specific logger for diagnostics without changing response data.
log = logging.getLogger(__name__)


# Translate provider and application exceptions into useful public error codes without exposing raw SDK
# responses.
def failure_detail(exc):
    """Classify failures without exposing SDK bodies, URLs or credentials."""
    # Identify application/provider timeouts separately from configuration and network failures.
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        # Return the normalized failure detail value calculated by the expression below.
        return 'request_timeout', 'The request exceeded a timeout while waiting for the agent or Azure OpenAI.'
    # Report network/proxy/TLS connection failures without exposing the provider URL or request internals.
    if isinstance(exc, APIConnectionError):
        # Return the normalized failure detail value calculated by the expression below.
        return 'azure_connection_failed', ('Unable to establish a connection to Azure OpenAI. '
            'Check the endpoint hostname, network, VPN, proxy, firewall and certificate configuration.')
    # Use the provider's HTTP status to give specific safe authentication, quota or capability guidance.
    if isinstance(exc, APIStatusError):
        # Map known Azure HTTP status codes to safe guidance about configuration, access or quota.
        messages = {
            400: 'Azure rejected the model request. Check deployment capabilities and API version.',
            401: 'Azure rejected authentication. Check the configured API key and resource.',
            403: 'Azure denied access. Check resource permissions and network access rules.',
            404: 'Azure could not find the requested resource. Check the endpoint, deployment and API version.',
            429: 'Azure rate or quota limits were reached. Check deployment capacity.',
        }
        # Return the normalized failure detail value calculated by the expression below.
        return 'azure_http_error', messages.get(exc.status_code, 'Azure returned a service error.')
    # Return the normalized failure detail value calculated by the expression below.
    return 'agent_failed', 'The billing agent encountered an internal error. Check the server traceback for this request.'


# Apply the optional application API key to every route through the FastAPI dependency.
async def authorize(x_api_key: str | None = Header(default=None)):
    # When an application key is configured, require a constant-time match with the supplied API-key header.
    if settings.API_KEY and not hmac.compare_digest((x_api_key or "").encode(), settings.API_KEY.encode()):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(401, "Invalid API key.")


# Require a separately configured, sufficiently long admin key before allowing privileged operations.
def require_admin(key):
    # Read the separately configured privileged API credential.
    configured = settings.ADMIN_API_KEY
    # Refuse admin access when its key is short or reused for a less-privileged role.
    if len(configured) < 16 or configured in (settings.API_KEY, settings.CASHIER_API_KEY):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(503, 'Set a distinct BILLING_ADMIN_API_KEY of at least 16 characters and restart the API.')
    # Deny access unless the supplied staff key matches the configured key using a constant-time comparison.
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(403, 'Admin access denied.')


# Allow a verified admin or the separate cashier credential to enter the cashier permission scope.
def require_cashier(key, admin_key=None):
    # Continue here only when admin key is present/true.
    if admin_key:
        # Verify the distinct admin key before continuing the privileged route.
        require_admin(admin_key)
        # End this path without returning protected data or running the remaining steps.
        return
    # Read the separately configured privileged API credential.
    configured = settings.CASHIER_API_KEY
    # Refuse cashier access when its key is short or reused for another application role.
    if len(configured) < 16 or configured in (settings.API_KEY, settings.ADMIN_API_KEY):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(503, 'Set a distinct BILLING_CASHIER_API_KEY of at least 16 characters and restart the API.')
    # Deny access unless the supplied staff key matches the configured key using a constant-time comparison.
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(403, 'Cashier access denied.')


# Require a live customer session belonging to the exact requested customer account.
def require_customer(customer_id, token):
    # Reject a missing, expired or different-account customer token even if the caller knows the account ID.
    if customer_auth.customer_for_token(token) != customer_id:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(401, 'Sign in to the matching customer account.')


# Authorize the saved conversation role and owner before any history, tool execution or invoice access.
def check_record(record, admin_key, cashier_key=None, customer_token=None):
    # Stop when no matching saved record exists; later steps require its persisted role or owner.
    if not record:
        # Record the outcome of this structured access, validation or request-consistency check.
        telemetry.guardrail('account_access', 'blocked', 'conversation_unavailable')
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(404, 'Conversation not found.')
    # Attach the saved role/validated IDs to this request's observation.
    telemetry.identify(mode=record['mode'])
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Continue here only when mode in record equals 'admin'.
        if record['mode'] == 'admin':
            # Verify the distinct admin key before continuing the privileged route.
            require_admin(admin_key)
        # Continue here only when mode in record equals 'cashier'.
        elif record['mode'] == 'cashier':
            # Verify cashier credentials or an explicitly supplied valid admin credential.
            require_cashier(cashier_key, admin_key)
        # Continue here only when customer id in record is present/true.
        elif record['customer_id']:
            # Verify that the supplied customer token belongs to the requested account.
            require_customer(record['customer_id'], customer_token)
    # Handle HTTPException through this operation's explicit recovery path.
    except HTTPException:
        # Record the outcome of this structured access, validation or request-consistency check.
        telemetry.guardrail('account_access', 'blocked', 'credentials_rejected')
        # Preserve the original exception after recording or cleaning up this operation.
        raise
    # Record the outcome of this structured access, validation or request-consistency check.
    telemetry.guardrail('account_access', 'pass',
                        'public_scope' if record['mode']=='customer' and not record['customer_id'] else 'verified_scope')


# Initialize storage, create the shared agent and lock, then start and clean up long-lived services with the
# API.
@asynccontextmanager
async def lifespan(app):
    # Create/migrate the billing schema before the API begins accepting requests.
    initialize_billing()
    # Initialize persisted chat and shopping-state storage before serving conversations.
    context.initialize()
    # Create one process-local lock so mutable agent/workbench state is never shared across simultaneous chat
    # turns.
    app.state.chat_lock = asyncio.Lock()
    # Create the production engine only when the application has not already been given one.
    if not hasattr(app.state, "engine"):
        # Import the agent.billing_agent helpers used below.
        from agent.billing_agent import BillingAgent
        # Create the orchestration object that owns the model client and MCP workbench lifecycle.
        app.state.engine = BillingAgent()
    # Detect an optional engine startup hook without requiring it from test/replacement engines.
    start = getattr(app.state.engine, "start", None)
    # Detect an optional engine cleanup hook for the API shutdown path.
    close = getattr(app.state.engine, "close", None)
    # Start the optional background observability writer during API startup.
    telemetry.start()
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Continue here only when start is present/true.
        if start:
            # Await the pending asynchronous operation before continuing this workflow.
            await start()
        # Yield this stream event or keep-alive to the connected client without waiting for the complete
        # response.
        yield
    finally:
        # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
        try:
            # Continue here only when close is present/true.
            if close:
                # Await the pending asynchronous operation before continuing this workflow.
                await close()
        finally:
            # Stop the optional writer within its bounded shutdown period.
            telemetry.stop()


# Obtain app from FastAPI using the explicit arguments shown here.
app = FastAPI(title="Billing Agent", version="7.0.0", lifespan=lifespan,
              dependencies=[Depends(authorize)])
# Wrap chat HTTP responses with optional end-to-end measurement, including streaming failures.
app.add_middleware(telemetry.ObservabilityMiddleware)
# Attach the separate dashboard routes with the same admin verifier used by the billing API.
app.include_router(observability_router(require_admin))


# Return useful validation guidance while removing input values that could contain passwords or address details.
@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Use address-specific validation guidance for address creation/selection routes.
    if request.url.path.startswith(('/customers/addresses', '/customers/orders/')):
        # Build the explicit JSON response with the supplied status and non-cacheable headers where configured
        # and return that result.
        return JSONResponse(status_code=422, content={'detail': 'Check the required delivery address fields and their lengths.'})
    # Use account-specific guidance for customer authentication/profile validation failures.
    if request.url.path.startswith('/customers/'):
        # Build the explicit JSON response with the supplied status and non-cacheable headers where configured
        # and return that result.
        return JSONResponse(status_code=422, content={'detail': 'Check the customer ID, account details and password length (10 to 128 characters).'})
    # Do not echo user-supplied values in error responses.
    errors = [{key: value for key, value in error.items() if key not in ('input', 'ctx')}
              for error in exc.errors()]
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse(status_code=422, content={'detail': errors})


# Define the optional account association and the three supported conversation roles.
class ConversationRequest(BaseModel):
    # Define the validated customer id field, including its required/default value and length/type constraints.
    customer_id: str | None = Field(default=None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    # Define mode with the annotated request-model type and the shown default.
    mode: Literal['customer', 'cashier', 'admin'] = 'customer'


# Require distinct conversation/request UUIDs and bounded nonempty text for an idempotent chat turn.
class ChatRequest(BaseModel):
    # Define conversation id with the annotated request-model type and the shown default.
    conversation_id: UUID
    # Define request id with the annotated request-model type and the shown default.
    request_id: UUID
    # Define the validated message field, including its required/default value and length/type constraints.
    message: str = Field(min_length=1, max_length=10000)


# Validate login fields while keeping the password out of model representations and serialized model output.
class CustomerLogin(BaseModel):
    # Define the validated customer id field, including its required/default value and length/type constraints.
    customer_id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')
    # Define the validated password field, including its required/default value and length/type constraints.
    password: str = Field(min_length=10, max_length=128, repr=False, exclude=True)


# Extend the login fields with the customer profile required to open a new account.
class CustomerRegistration(CustomerLogin):
    # Define the validated name field, including its required/default value and length/type constraints.
    name: str = Field(min_length=1, max_length=200)
    # Define the validated company field, including its required/default value and length/type constraints.
    company: str | None = Field(default=None, max_length=200)
    # Define the validated email field, including its required/default value and length/type constraints.
    email: str | None = Field(default=None, max_length=200)


# Expose registration with a non-cacheable session response and a conflict status for rejected account creation.
@app.post('/customers/register', status_code=201)
def register_customer(body: CustomerRegistration):
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Build the explicit JSON response with the supplied status and non-cacheable headers where configured
        # and return that result.
        return JSONResponse(customer_auth.register(body.customer_id, body.name, body.password, body.company, body.email),
                            status_code=201, headers={'Cache-Control': 'no-store'})
    # Handle ValueError through this operation's explicit recovery path.
    except ValueError as exc:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(409, str(exc)) from exc


# Exchange valid credentials for a session token without distinguishing unknown IDs from wrong passwords.
@app.post('/customers/login')
def login_customer(body: CustomerLogin):
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Keep the server-validated session details returned for this customer token.
        session = customer_auth.login(body.customer_id, body.password)
    # Handle ValueError through this operation's explicit recovery path.
    except ValueError:
        # Keep the server-validated session details returned for this customer token.
        session = None
    # Reject authentication/session lookup when no verified current session was returned.
    if not session:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(401, 'Unable to sign in. Check your credentials or contact store staff.')
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse(session, headers={'Cache-Control': 'no-store'})


# Revoke the token supplied in the customer header and acknowledge sign-out.
@app.post('/customers/logout')
def logout_customer(x_customer_token: str | None = Header(default=None)):
    # Apply this logout customer step to its explicit receiver and arguments.
    customer_auth.logout(x_customer_token)
    # Return the explicitly assembled logout customer response fields to the caller.
    return {'signed_out': True}


# Let the UI verify its current token and expiry without resending a password.
@app.get('/customers/session')
def customer_session(x_customer_token: str | None = Header(default=None)):
    # Keep the server-validated session details returned for this customer token.
    session = customer_auth.session_for_token(x_customer_token)
    # Reject authentication/session lookup when no verified current session was returned.
    if not session:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(401, 'Your customer session ended. Please sign in again.', headers={'Cache-Control': 'no-store'})
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse(session, headers={'Cache-Control': 'no-store'})


# Validate required delivery fields, optional descriptive fields and a strictly Boolean default-address flag.
class DeliveryAddress(BaseModel):
    # Reject unknown request fields rather than silently accepting unsupported input.
    model_config = ConfigDict(extra='forbid')
    # Define the validated full name field, including its required/default value and length/type constraints.
    full_name: str = Field(min_length=1, max_length=200)
    # Define the validated phone field, including its required/default value and length/type constraints.
    phone: str = Field(min_length=1, max_length=30)
    # Define the validated address line1 field, including its required/default value and length/type
    # constraints.
    address_line1: str = Field(min_length=1, max_length=200)
    # Define the validated address line2 field, including its required/default value and length/type
    # constraints.
    address_line2: str = Field(default='', max_length=200)
    # Define the validated city field, including its required/default value and length/type constraints.
    city: str = Field(min_length=1, max_length=100)
    # Define the validated state field, including its required/default value and length/type constraints.
    state: str = Field(min_length=1, max_length=100)
    # Define the validated postal code field, including its required/default value and length/type constraints.
    postal_code: str = Field(min_length=1, max_length=20)
    # Define the validated country field, including its required/default value and length/type constraints.
    country: str = Field(min_length=2, max_length=2)
    # Define the validated label field, including its required/default value and length/type constraints.
    label: str = Field(default='', max_length=50)
    # Define the validated landmark field, including its required/default value and length/type constraints.
    landmark: str = Field(default='', max_length=200)
    # Define the validated is default field, including its required/default value and length/type constraints.
    is_default: bool = Field(default=False, strict=True)


# Accept one structured saved-address ID and reject unknown request fields.
class DeliveryAddressSelection(BaseModel):
    # Reject unknown request fields rather than silently accepting unsupported input.
    model_config = ConfigDict(extra='forbid')
    # Define the validated address id field, including its required/default value and length/type constraints.
    address_id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')


# Derive address ownership from the verified token, never from a caller-supplied customer ID.
def address_customer(token):
    # Obtain customer id from customer_auth.customer_for_token using the explicit arguments shown here.
    customer_id = customer_auth.customer_for_token(token)
    # Require a verified customer identity before allowing address-book operations.
    if not customer_id:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(401, 'Sign in to manage your delivery addresses.', headers={'Cache-Control': 'no-store'})
    # Return the prepared customer id to the caller.
    return customer_id


# Convert address business-rule failures to non-cacheable conflicts while preserving successful status codes.
def address_result(operation, *args, status_code=200, **kwargs):
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Obtain result from operation using the explicit arguments shown here.
        result = operation(*args, **kwargs)
    # Handle ValueError through this operation's explicit recovery path.
    except ValueError as exc:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from exc
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse(result, status_code=status_code, headers={'Cache-Control': 'no-store'})


# List only the signed-in customer's saved addresses.
@app.get('/customers/addresses')
def customer_addresses(x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import list_addresses
    # Obtain customer id from address_customer using the explicit arguments shown here.
    customer_id = address_customer(x_customer_token)
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse({'addresses': list_addresses(customer_id)}, headers={'Cache-Control': 'no-store'})


# Save a new address under the token owner, using the validated address form.
@app.post('/customers/addresses', status_code=201)
def add_delivery_address(body: DeliveryAddress, x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import save_address
    # Return the normalized add delivery address value calculated by the expression below.
    return address_result(lambda: {'address': save_address(address_customer(x_customer_token), body.model_dump())}, status_code=201)


# Update an existing address through the storage layer's customer-ownership checks.
@app.put('/customers/addresses/{address_id}')
def update_delivery_address(address_id: str, body: DeliveryAddress, x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import save_address
    # Return the normalized update delivery address value calculated by the expression below.
    return address_result(lambda: {'address': save_address(address_customer(x_customer_token), body.model_dump(), address_id=address_id)})


# Delete a saved address only within the signed-in customer's address book.
@app.delete('/customers/addresses/{address_id}')
def remove_delivery_address(address_id: str, x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import delete_address
    # Return the normalized remove delivery address value calculated by the expression below.
    return address_result(delete_address, address_customer(x_customer_token), address_id)


# Change the default address through the transaction-aware storage operation.
@app.post('/customers/addresses/{address_id}/default')
def default_delivery_address(address_id: str, x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import set_default_address
    # Return the normalized default delivery address value calculated by the expression below.
    return address_result(lambda: {'address': set_default_address(address_customer(x_customer_token), address_id)})


# Associate a verified customer's saved address with the specified draft, subject to draft ownership and state
# checks.
@app.put('/customers/orders/{order_id}/delivery-address')
def select_delivery_address(order_id: str, body: DeliveryAddressSelection, x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import select_order_address
    # Return the normalized select delivery address value calculated by the expression below.
    return address_result(select_order_address, order_id, address_customer(x_customer_token), body.address_id)


# Read active drafts and their saved delivery snapshots so the UI can choose an address before placement.
@app.get('/customers/checkout')
def customer_checkout(x_customer_token: str | None = Header(default=None)):
    # Import existing billing storage, address ownership and invoice paths.
    from database.addresses import list_addresses
    # Obtain customer id from address_customer using the explicit arguments shown here.
    customer_id = address_customer(x_customer_token)
    # Ensure the opened connection is closed when this database block exits, including exceptions.
    with closing(connect(readonly=True)) as conn:
        # Start a read snapshot so related totals and rows describe the same database state.
        conn.execute('BEGIN')
        # Collect validated rows for a query result, table or chart.
        rows = conn.execute('''SELECT o.order_id,o.charged_price,
            COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') AS currency,
            delivery.address_id,delivery.address_snapshot
            FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id
            LEFT JOIN order_delivery delivery ON delivery.order_id=o.order_id
            WHERE o.customer_id=? AND o.order_status='draft'
              AND COALESCE(d.status,'active')<>'cancelled'
            ORDER BY o.order_date DESC,o.order_id''', (customer_id,)).fetchall()
        # Collect only the authenticated customer's current active drafts for address selection.
        orders = []
        # Process each row from this customer checkout collection in order.
        for row in rows:
            # Copy the draft ID, total, currency and selected address ID from this row.
            order = {key: row[key] for key in ('order_id', 'charged_price', 'currency', 'address_id')}
            # Handle the expected failure paths below while ensuring the surrounding workflow can clean up
            # safely.
            try:
                # Decode the immutable address snapshot if a selected address has been recorded.
                snapshot = json.loads(row['address_snapshot']) if row['address_snapshot'] else None
            # Handle (ValueError, TypeError) through this operation's explicit recovery path.
            except (ValueError, TypeError):
                # Decode the immutable address snapshot if a selected address has been recorded.
                snapshot = None
            # Derive delivery address from the current customer checkout values using the expression below.
            order['delivery_address'] = snapshot if isinstance(snapshot, dict) else None
            # Append the validated order to this operation's result collection.
            orders.append(order)
    # Build the explicit JSON response with the supplied status and non-cacheable headers where configured and
    # return that result.
    return JSONResponse({'addresses': list_addresses(customer_id), 'orders': orders}, headers={'Cache-Control': 'no-store'})


# Report local configuration readiness without making a model call or claiming that Azure connectivity was
# verified.
@app.get("/health")
async def health():
    # Validate local Azure settings without sending a request to Azure.
    error = settings.azure_configuration_error()
    # Return the explicitly assembled health response fields to the caller.
    return {"status": "degraded" if error else "ok", "api": "ready",
            "provider": "azure_openai", "model_configured": error is None,
            "chart_streaming": True,
            "connection_verified": False, "configuration_error": error,
            "model": settings.AZURE_OPENAI_MODEL,
            "deployment": settings.AZURE_OPENAI_DEPLOYMENT}


# Bind a new chat to its verified role and customer session; an invalid supplied token cannot become anonymous.
@app.post("/conversations")
async def create_conversation(body: ConversationRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    # Begin with the optional requested customer association, then replace/verify it against the session.
    customer_id = body.customer_id
    # Continue here only when body mode equals 'admin'.
    if body.mode == 'admin':
        # Verify the distinct admin key before continuing the privileged route.
        require_admin(x_admin_key)
    # Continue here only when body mode equals 'cashier'.
    elif body.mode == 'cashier':
        # Verify cashier credentials or an explicitly supplied valid admin credential.
        require_cashier(x_cashier_key, x_admin_key)
    # Treat any supplied token as an authentication claim that must be verified before opening a customer chat.
    elif x_customer_token is not None:
        # The verified session owns a new customer chat even when a client omits
        # its redundant account ID. A supplied invalid token is never anonymous.
        verified = customer_auth.customer_for_token(x_customer_token)
        # Reject invalid tokens and any supplied customer ID that differs from the verified session owner.
        if not verified or customer_id is not None and customer_id != verified:
            # Reject this HTTP request with the explicit status and safe public explanation shown here.
            raise HTTPException(401, 'Sign in to the matching customer account.')
        # Begin with the optional requested customer association, then replace/verify it against the session.
        customer_id = verified
    # Do not allow an unverified customer ID alone to open an account-associated conversation.
    elif customer_id:
        # Verify that the supplied customer token belongs to the requested account.
        require_customer(customer_id, None)
    # Return the explicitly assembled create conversation response fields to the caller.
    return {"conversation_id": context.conversation(customer_id=customer_id, mode=body.mode), 'mode': body.mode}


# Verify the admin credential for the UI before it opens privileged features.
@app.get('/admin/access')
async def admin_access(x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None)):
    # Verify the distinct admin key before continuing the privileged route.
    require_admin(x_admin_key)
    # Return the explicitly assembled admin access response fields to the caller.
    return {'authorized': True, 'mode': 'admin', 'read_only': False}


# Verify cashier access and return the configured refund threshold for display.
@app.get('/cashier/access')
async def cashier_access(x_cashier_key: str | None = Header(default=None), x_admin_key: str | None = Header(default=None)):
    # Verify cashier credentials or an explicitly supplied valid admin credential.
    require_cashier(x_cashier_key, x_admin_key)
    # Return the explicitly assembled cashier access response fields to the caller.
    return {'authorized': True, 'mode': 'cashier', 'refund_limit': settings.CASHIER_REFUND_LIMIT,
            'currency': settings.CASHIER_REFUND_CURRENCY}


# Load authorized history and role-appropriate saved state for resuming an existing chat.
@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    # Keep the current database or saved-conversation record for the checks that follow.
    record = context.get_conversation(str(conversation_id))
    # Recheck the saved conversation's role and ownership with the supplied credentials.
    check_record(record, x_admin_key, x_cashier_key, x_customer_token)
    # Load the saved history and its chart/table/report attachments for this authorized chat.
    messages = context.history(str(conversation_id), include_artifacts=True)
    # Load the saved history and its chart/table/report attachments for this authorized chat.
    messages = [{**m, "content": plain_reply(m["content"])} if m["role"] == "assistant" else m for m in messages]
    # Return the explicitly assembled get conversation response fields to the caller.
    return {**record, "messages": messages,
            **({'analytics_context': context.latest_analytics_context(str(conversation_id))} if record['mode'] == 'admin' else {}),
            **({'shopping_state': context.shopping_state(str(conversation_id))} if record['mode'] == 'customer' else {})}


# Run an ordinary JSON chat request through the shared authorization, retry and persistence path.
@app.post("/chat")
async def chat(body: ChatRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    # Return the normalized chat value calculated by the expression below.
    return await run_chat(body, x_admin_key, x_cashier_key=x_cashier_key, x_customer_token=x_customer_token)


# Authorize first, then stream early chart artifacts and the final saved result as newline-delimited JSON.
@app.post('/chat/stream')
async def stream_chat(body: ChatRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    # Check access before returning a streaming response or starting any work.
    check_record(context.get_conversation(str(body.conversation_id)), x_admin_key, x_cashier_key, x_customer_token)
    # Own the streaming queue and background chat task, including keep-alives and cancellation cleanup.
    async def events():
        # Capture a monotonic timestamp for elapsed-time or cache-expiry calculation.
        stream_started = perf_counter()
        # Create an asynchronous event queue linking the background chat task to the response stream.
        queue = asyncio.Queue()
        # Put either the completed chat response or a sanitized failure onto the stream queue.
        async def run():
            # Handle the expected failure paths below while ensuring the surrounding workflow can clean up
            # safely.
            try:
                # Await the API/agent result and retain it as result before continuing.
                result = await run_chat(body, x_admin_key, x_cashier_key=x_cashier_key, x_customer_token=x_customer_token,
                    chart_callback=lambda charts: queue.put_nowait({'event':'charts','charts':charts}))
                # Put the prepared event on the queue without blocking the producing task.
                queue.put_nowait({'event':'result','result':result})
            # Handle HTTPException through this operation's explicit recovery path.
            except HTTPException as exc:
                # Put the prepared event on the queue without blocking the producing task.
                queue.put_nowait({'event':'error','status':exc.status_code,'detail':exc.detail})
            # Handle Exception through this operation's explicit recovery path.
            except Exception:
                # Write the server-side traceback for diagnosis while keeping raw exception details out of the
                # public reply.
                log.exception('Streaming chat failed')
                # Put the prepared event on the queue without blocking the producing task.
                queue.put_nowait({'event':'error','status':503,'detail':'The billing request failed. Check the server logs.'})
        # Start the chat coroutine as a background task while the response generator consumes its events.
        task = asyncio.create_task(run())
        # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
        try:
            # Continue this loop while True.
            while True:
                # Handle the expected failure paths below while ensuring the surrounding workflow can clean up
                # safely.
                try:
                    # Bound this asynchronous wait with the configured timeout; retain the returned value as
                    # event.
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                # Handle asyncio.TimeoutError through this operation's explicit recovery path.
                except asyncio.TimeoutError:
                    # Yield this stream event or keep-alive to the connected client without waiting for the
                    # complete response.
                    yield '\n'
                    # Skip this invalid/empty iteration and continue with the remaining records or stream
                    # events.
                    continue
                # Apply this events step to its explicit receiver and arguments.
                logging.getLogger('uvicorn.error.billing_performance').info(
                    'billing_stream event=%s seconds=%.3f', event['event'], perf_counter()-stream_started)
                # Yield this stream event or keep-alive to the connected client without waiting for the complete
                # response.
                yield json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
                # End the event stream after a terminal saved result or error has been sent.
                if event['event'] in ('result','error'):
                    # Stop this loop once its completion, empty-queue or terminal-event condition has been met.
                    break
        finally:
            # Cancel the chat producer only if the streaming connection closes before the task finishes.
            if not task.done():
                # Cancel the unfinished background task when its response stream is closing.
                task.cancel()
            # Await task cleanup while collecting cancellation/errors instead of abandoning the task.
            await asyncio.gather(task, return_exceptions=True)
    # Return the newline-delimited event generator with proxy buffering disabled for early chart delivery and
    # return that result.
    return StreamingResponse(events(), media_type='application/x-ndjson',
        headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


# Apply the same reply, table and report sanitization to an authorized saved response.
def cached_chat_response(cached):
    """Apply the same display validation to both completed-request paths."""
    # Revalidate saved table artifacts on the completed-request retry path as well.
    if 'tables' in cached:
        # Obtain tables from sanitize_tables using the explicit arguments shown here.
        cached['tables'] = sanitize_tables(cached['tables'])
    # Revalidate saved report artifacts on the completed-request retry path as well.
    if 'reports' in cached:
        # Obtain reports from sanitize_reports using the explicit arguments shown here.
        cached['reports'] = sanitize_reports(cached['reports'])
    # Return the explicitly assembled cached chat response response fields to the caller.
    return {**cached, 'response': plain_reply(cached['response'])}


# Coordinate one idempotent chat turn: authorize, reuse completed work, serialize execution, checkpoint and
# persist the result.
@telemetry.observe_chat
async def run_chat(body, x_admin_key, chart_callback=None, x_cashier_key=None, x_customer_token=None):
    # Prepare cid, rid with the fields, rows or options required by the following run chat steps.
    cid, rid = str(body.conversation_id), str(body.request_id)
    # Reject whitespace-only chat submissions before admission or model/tool execution.
    if not body.message.strip():
        # Record the outcome of this structured access, validation or request-consistency check.
        telemetry.guardrail('request_validation', 'blocked', 'blank_message')
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(422, "Message must not be blank.")
    # Recheck the saved conversation's role and ownership with the supplied credentials.
    check_record(context.get_conversation(cid), x_admin_key, x_cashier_key, x_customer_token)
    # A finished response is immutable. Serve an authorized retry immediately
    # rather than queueing it behind an unrelated customer's model request.
    try:
        # Check whether this exact request ID and message already have an immutable saved response; retain the
        # returned value as cached.
        cached = context.completed_response(cid, rid, body.message)
    # Handle ValueError through this operation's explicit recovery path.
    except ValueError as exc:
        # Record the outcome of this structured access, validation or request-consistency check.
        telemetry.guardrail('request_consistency', 'blocked', 'request_id_conflict')
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(409, str(exc)) from exc
    # Reuse the authorized completed response instead of rerunning the model or billing tools.
    if cached:
        # Record that this retry reuses a completed response with no new model/tool charge.
        telemetry.cache_hit()
        # Return the normalized run chat value calculated by the expression below.
        return cached_chat_response(cached)
    # One worker and one active turn: no mutable agent state is shared across users.
    queued = perf_counter()
    # Start measuring time spent waiting for the shared agent lock.
    telemetry.queue_started()
    # Hold the relevant coordination lock for this state-sensitive block and release it on exit.
    async with app.state.chat_lock:
        # Stop the queue-wait measurement once exclusive agent access is acquired.
        telemetry.queue_finished()
        # Apply this run chat step to its explicit receiver and arguments.
        logging.getLogger("uvicorn.error.billing_performance").info(
            "billing_api queue_wait_seconds=%.3f", perf_counter() - queued)
        # Reload the saved conversation after waiting so authorization uses the current persisted role/owner.
        record = context.get_conversation(cid)
        # Recheck the saved conversation's role and ownership with the supplied credentials.
        check_record(record, x_admin_key, x_cashier_key, x_customer_token)
        # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
        try:
            # Detect whether this engine can checkpoint mutation dispatch for safe retry handling.
            tracks_changes = getattr(app.state.engine, 'supports_request_progress', False) is True
            # Admit this request or reuse its completed result, enforcing saved retry/mutation constraints;
            # retain the returned value as cached.
            cached = context.begin(cid, rid, body.message, may_have_changed=not tracks_changes)
        # Handle ValueError through this operation's explicit recovery path.
        except ValueError as exc:
            # Record the outcome of this structured access, validation or request-consistency check.
            telemetry.guardrail('request_consistency', 'blocked', 'request_not_retryable')
            # Reject this HTTP request with the explicit status and safe public explanation shown here.
            raise HTTPException(409, str(exc)) from exc
        # Reuse the authorized completed response instead of rerunning the model or billing tools.
        if cached:
            # Record that this retry reuses a completed response with no new model/tool charge.
            telemetry.cache_hit()
            # Return the normalized run chat value calculated by the expression below.
            return cached_chat_response(cached)
        # Record the outcome of this structured access, validation or request-consistency check.
        telemetry.guardrail('request_consistency', 'pass', 'request_admitted')
        # Prepare the request-bound shopping state before passing it to the agent.
        initial_shopping_state = None
        # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
        try:
            # Continue here only when mode in record equals 'customer'.
            if record['mode'] == 'customer':
                # Keep the original input only on this admitted request. A
                # read-only retry gets its proposal back; new turns do not.
                initial_shopping_state = context.customer_turn_state(cid, rid, record['customer_id'])
            # Detect an engine-specific configuration validator before starting a provider/tool turn.
            validate = getattr(app.state.engine, "validate_configuration", None)
            # Continue here only when validate is present/true.
            if validate:
                # Apply this run chat step to its explicit receiver and arguments.
                validate()
        # Handle ValueError through this operation's explicit recovery path.
        except ValueError as exc:
            # No tools have run. Retain request-bound input for a same-ID retry,
            # while its approval remains expired for any later customer turn.
            context.fail(cid, rid, before_execution=True)
            # Reject this HTTP request with the explicit status and safe public explanation shown here.
            raise HTTPException(503, str(exc)) from exc
        # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
        try:
            # Pass the saved staff role explicitly; customer mode remains the engine default.
            kwargs = {'mode': record['mode']} if record['mode'] in ('admin', 'cashier') else {}
            # Attach a mutation checkpoint only when the engine explicitly supports dispatch progress reporting.
            if tracks_changes:
                # Derive mutation callback from the current run chat values using the expression below.
                kwargs['mutation_callback'] = lambda: context.mark_request_changed(cid, rid)
            # Enable early chart delivery only for an admin stream that supplied a chart callback.
            if chart_callback is not None and record['mode'] == 'admin':
                # Derive chart callback from the current run chat values using the expression below.
                kwargs['chart_callback'] = chart_callback
            # Restore previous analytics evidence only for an admin engine that supports chart/report follow-up
            # context.
            if record['mode'] == 'admin' and getattr(app.state.engine, 'supports_analytics_context', False):
                # Obtain analytics context from context.latest_analytics_context using the explicit arguments
                # shown here.
                kwargs['analytics_context'] = context.latest_analytics_context(cid)
            # Restore saved staff-action review terms only for supported staff-mode engines.
            if record['mode'] in ('admin','cashier') and getattr(app.state.engine, 'supports_action_context', False):
                # Obtain action context from context.latest_action_context using the explicit arguments shown
                # here.
                kwargs['action_context'] = context.latest_action_context(cid,rid)
            # Continue here only when mode in record equals 'customer'.
            if record['mode'] == 'customer':
                # Merge the prepared validated fields into this operation's existing result or state mapping.
                kwargs.update(shopping_state=initial_shopping_state,
                    state_callback=lambda state, owner: context.checkpoint_shopping(cid, rid, state, owner))
            # Measure the complete agent await without altering its returned value or exception; retain the
            # returned value as result.
            with sdk_request_trace(conversation_id=cid, request_id=rid, mode=record['mode']):
                result = await telemetry.agent_call(asyncio.wait_for(
                    app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
                    timeout=settings.CHAT_TIMEOUT))
            # Obtain response from plain_reply using the explicit arguments shown here.
            result["response"] = plain_reply(result["response"])
            # Validate table artifacts before they are persisted or returned to the UI.
            if 'tables' in result:
                # Obtain tables from sanitize_tables using the explicit arguments shown here.
                result['tables'] = sanitize_tables(result['tables'])
            # Validate report datasets and coverage before they are persisted or returned.
            if 'reports' in result:
                # Obtain reports from sanitize_reports using the explicit arguments shown here.
                result['reports'] = sanitize_reports(result['reports'])
            # Remove any agent-proposed account association so it can be checked against the verified chat
            # owner.
            associated = result.pop('associated_customer_id', None)
            # Reject any attempt by an agent result to switch the customer identity bound to this verified chat.
            if associated and record['mode'] == 'customer' and associated != record['customer_id']:
                # Reject the invalid value with a clear business/configuration validation message.
                raise ValueError('Customer association requires verified sign-in in a new conversation.')
            # Add stable request/conversation IDs to the sanitized engine response before persistence.
            response = {"conversation_id": cid, "request_id": rid, **result}
            # Save the complete response and artifacts against this request ID before returning success.
            context.finish(cid, rid, response)
            # Return the prepared response to the caller.
            return response
        # Handle asyncio.CancelledError through this operation's explicit recovery path.
        except asyncio.CancelledError:
            # Persist the failed attempt state so a later retry cannot blindly repeat a possible billing change.
            context.fail(cid, rid)
            # Preserve the original exception after recording or cleaning up this operation.
            raise
        # Handle Exception through this operation's explicit recovery path.
        except Exception as exc:
            # Persist the failed attempt state so a later retry cannot blindly repeat a possible billing change.
            context.fail(cid, rid)
            # Write the server-side traceback for diagnosis while keeping raw exception details out of the
            # public reply.
            log.exception("Billing turn failed: %s", rid)
            # Translate the internal exception into a safe public error code and explanation.
            code, message = failure_detail(exc)
            # Reject this HTTP request with the explicit status and safe public explanation shown here.
            raise HTTPException(503, {"code": code, "message": message + ' '
                "If this request included a billing change, check its status before repeating it.",
                "conversation_id": cid, "request_id": rid}) from exc


# Verify the conversation/account scope, prepare the owned invoice and return its PDF through a file response.
@app.get("/invoices/{invoice_number}")
async def invoice(invoice_number: str, conversation_id: UUID, customer_id: str | None = None,
                  x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None),
                  x_customer_token: str | None = Header(default=None)):
    # Keep the current database or saved-conversation record for the checks that follow.
    record = context.get_conversation(str(conversation_id))
    # Recheck the saved conversation's role and ownership with the supplied credentials.
    check_record(record, x_admin_key, x_cashier_key, x_customer_token)
    # Use the customer association already stored on the authorized conversation.
    associated = record["customer_id"]
    # Reject an invoice request that tries to override the customer already associated with this chat.
    if associated and customer_id and associated != customer_id:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(403, "Invoice customer does not match this conversation.")
    # Use the conversation owner; only verified staff may supply a separate invoice customer ID.
    owner = associated or (customer_id if record['mode'] in ('admin', 'cashier') else None)
    # Require an explicit, authorized customer owner before preparing an invoice.
    if not owner:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(422, "A customer ID is required.")
    # Handle the expected failure paths below while ensuring the surrounding workflow can clean up safely.
    try:
        # Bound this asynchronous wait with the configured timeout; retain the returned value as result.
        result = await asyncio.wait_for(app.state.engine.invoice(invoice_number, owner), timeout=60)
    # Handle Exception through this operation's explicit recovery path.
    except Exception as exc:
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(503, "Invoice tool unavailable.") from exc
    # Return the invoice tool's not-found failure instead of assuming a PDF was generated.
    if not result.get("ok"):
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(404, result.get("error", "Invoice not found."))
    # Resolve the expected owned invoice PDF path through the invoice helper.
    path = invoice_path(invoice_number, owner)
    # Check that invoice generation actually produced a file before returning a download.
    if not path.is_file():
        # Reject this HTTP request with the explicit status and safe public explanation shown here.
        raise HTTPException(503, "Invoice file was not generated.")
    # Return the prepared owned invoice file as a PDF attachment and return that result.
    return FileResponse(path, media_type="application/pdf", filename="invoice.pdf")
