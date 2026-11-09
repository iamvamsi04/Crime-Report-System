# Coordinate asynchronous model requests, MCP dispatch, locks and cancellation.
import asyncio
# Encode tool arguments and internal response envelopes as JSON, then decode verified results.
import json
# Read process environment values passed to the child MCP server.
import os
# Use the current Python interpreter when starting MCP so both processes share dependencies.
import sys
# Write timing/error records through the application's existing logging system.
import logging
# Hash current order facts into fingerprints used to detect changed approval terms.
import hashlib
# Omit unsupported Azure request fields during tool-free reflection.
from openai import NOT_GIVEN
# Measure elapsed durations with a monotonic high-resolution clock.
from time import perf_counter
# Copy nested state so one turn cannot mutate saved proposals or cached tool schemas accidentally.
from copy import deepcopy
# Close SQLite connections even when validation returns early or raises an exception.
from contextlib import closing
# Compare monetary amounts exactly with Decimal and handle invalid decimal input.
from decimal import Decimal, InvalidOperation
# Use AutoGen's assistant loop for semantic reasoning and repeated tool calls when needed.
from autogen_agentchat.agents import AssistantAgent
# Recognize the final human-readable AutoGen message rather than a tool event.
from autogen_agentchat.messages import TextMessage
# Connect cancellation of this request to pending model/MCP operations.
from autogen_core import CancellationToken
# Start each model turn with exactly the deliberately selected message history.
from autogen_core.model_context import UnboundedChatCompletionContext
# Use typed AutoGen messages, local model-result objects, token counters and tool-execution results.
from autogen_core.models import UserMessage, AssistantMessage, CreateResult, RequestUsage, FunctionExecutionResult
# Wrap structured tool results and errors in the content format AutoGen expects.
from autogen_core.tools import ToolResult, TextResultContent
# Connect to MCP and describe how its local child process should start.
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams
# Read validated application paths, model configuration and execution limits.
from config import settings
from agent.sdk_runtime import create_sdk_agent
# Validate chart specifications against the actual returned columns and rows before attaching them.
from mcp_server.charts import chart_from_result
# Restrict exposed tools by verified role and hide server-owned scope fields from the model.
from mcp_server.access import ADMIN_TOOLS, CASHIER_TOOLS, CUSTOMER_TOOLS, SCOPE_FIELDS
# Separate visible English, internal state envelopes and optional tabular presentation.
from context.reply_text import plain_reply, split_reply_tables, reply_envelope
# Track customer shopping state and compare canonical approval arguments with current order facts.
from context.shopping_state import (ShoppingTracker, Proposal, order_snapshot,
    canonical_proposal_arguments, canonical_refund_arguments, proposal_details)
# Open the billing database through its shared connection configuration.
from database.connection import connect
# Build the role-specific system instructions from trusted account/state/schema facts.
from agent.prompts.runtime_policy import runtime_policy
# Verify that a proposed coupon identifier came from the customer rather than the assistant.
from agent.coupon_policy import customer_supplied_coupon
# Recognize executed result shapes and evidence; these helpers do not route natural-language questions.
from agent.response_contract import ResponseContract, READ_TOOLS, result_contract, evidence_satisfied, inventory_rows, category_rows
# Build typed presentation tables from successful tool results.
from agent.table_presentation import tool_tables
# Validate saved analytics recipes before reusing them as context for a later question.
from context.context_manager import sanitize_analytics_context
# Validate table attachments and ensure returned rows have a consistent shape.
from context.tables import table_artifact, sanitize_tables
# Bound old chat history while retaining whole recent conversational turns.
from agent.model_history import bounded_history
# Record request-local observability events without including raw prompts or credentials.
from observability import capture as telemetry


# Send performance records to Uvicorn's billing logger.
log = logging.getLogger("uvicorn.error.billing_performance")

# Only these tools are allowed together in a model-selected batch. Unknown and
# mutating tools fail closed before any member of that batch is dispatched.
# Only these known read operations may share one model-selected batch; mutations stay sequential.
BATCH_READ_TOOLS = frozenset({'execute_sql_query', 'compare_products',
    'find_cheaper_alternatives', 'billing_analysis', 'billing_anomalies'})


# Adapt AutoGen cancellation to the MCP adapter's task/coroutine behavior.
class RunCancellationToken(CancellationToken):
    # Initialize a token and remember the task that owns this complete billing turn.
    def __init__(self):
        # Initialize the parent token's cancellation bookkeeping.
        super().__init__()
        # A token may be constructed outside an active asyncio event loop.
        try:
            # Capture the current chat task so it is not registered as its own cancellable child.
            self.owner = asyncio.current_task()
        # Treat construction without a running event loop as having no owner task.
        except RuntimeError:
            # Store the absence explicitly rather than failing token construction.
            self.owner = None

    # Register actual futures/tasks without scheduling an already-owned raw coroutine twice.
    def link_future(self, future):
        # Avoid linking the owning task back to itself.
        if future is self.owner:
            # Leave the owner task unchanged.
            return future
        # The pinned MCP adapter also registers raw call_tool coroutines. They
        # have no cancel method; cancel their owning task without scheduling or
        # awaiting the coroutine a second time.
        # The MCP adapter can pass a coroutine that does not itself expose cancel().
        if not callable(getattr(future,'cancel',None)):
            # Find the task already executing that coroutine.
            task = asyncio.current_task()
            # Only attach a separate existing task; never create a second execution of the coroutine.
            if task is not None and task is not self.owner:
                # Let the parent cancellation token cancel that actual task.
                super().link_future(task)
            # Return the original coroutine to its caller without awaiting or scheduling it here.
            return future
        # Delegate ordinary cancellable futures to AutoGen's implementation.
        return super().link_future(future)


# Wrap Azure calls with request timing, usage capture and safe local responses for blocked dispatch.
class ObservedSDKClient:
    """Keep billing safeguards around the model client supplied by the SDK."""

    def __init__(self, client, workbench):
        self._client = client
        self.batch_workbench = workbench

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def create(self, *args, **kwargs):
        # Start timing before any local validation or provider work.
        started = perf_counter()
        # Track whether a provider call is in flight so local failures are not counted as failed model calls.
        provider_called = False
        # Ensure provider failures are recorded and then propagated to the chat boundary.
        try:
            # Read the current turn's workbench when one has been attached by BillingAgent.
            workbench = getattr(self, 'batch_workbench', None)
            # An uncertain/completed-but-unreported mutation must not be sent back to the model for an
            # automatic retry.
            if workbench is not None and (workbench.uncertain_change or workbench.completed_response_error or workbench.dispatch_problem):
                # There is nothing safe for a model to retry or infer here.
                # Record that this response was resolved locally without a model request.
                log.info('billing_model_skipped reason=billing_dispatch_error')
                # Choose the accurate warning for a completed change, an uncertain change or a dispatch
                # failure.
                answer = ('The saved billing change completed, but response handling failed. Do not repeat it.'
                    if workbench.completed_response_error else
                    'The billing change could not be verified. Check its saved status before retrying.'
                    if workbench.uncertain_change else workbench.dispatch_problem)
                # Return an AutoGen-compatible local answer with zero new provider token usage.
                return CreateResult(content=answer,
                    finish_reason='stop', usage=RequestUsage(prompt_tokens=0,completion_tokens=0),cached=False)
            # A blocked refund request needs a validated saved review before another execution attempt.
            if workbench is not None and workbench.refund_review:
                # The rejected operation needs a saved review, not another SDK
                # round trip that could repeat the same unapproved request.
                # Return a local review instruction so reflection cannot loop through the same rejected write.
                return CreateResult(content='Review the current refund request details.',
                    finish_reason='stop', usage=RequestUsage(prompt_tokens=0,completion_tokens=0),cached=False)
            # Copy per-call options before adapting them for the current request.
            extra = dict(kwargs.get('extra_create_args', {}))
            # Azure permits tool-calling options only when this request actually includes tools.
            if kwargs.get('tools'):
                # The recording workbench validates the returned batch before dispatch.
                if workbench is not None:
                    # Allow the model to propose independent reads together; unsafe batches are rejected by
                    # the workbench.
                    extra['parallel_tool_calls'] = True
            # Handle final reflection requests that carry no tool definitions.
            else:
                # AutoGen omits tools during final reflection. Azure rejects
                # parallel_tool_calls even when false in that case. NOT_GIVEN
                # also overrides the constructor default for this call only.
                # Omit parallel_tool_calls entirely; even false can be rejected when tools is absent.
                extra['parallel_tool_calls'] = NOT_GIVEN
                # Omit tool_choice for the same no-tools request.
                extra['tool_choice'] = NOT_GIVEN
                # Remove any duplicate top-level tool_choice so it cannot leak into reflection.
                kwargs.pop('tool_choice', None)
            # Only attach the adapted options when there is something to send.
            if extra:
                # Replace this call's options without changing the model client's constructor defaults.
                kwargs['extra_create_args'] = extra
            # Mark the upcoming provider operation for failure accounting.
            provider_called = True
            # Make the actual Azure model request and wait for its response.
            result = await self._client.create(*args, **kwargs)
            # The provider completed; subsequent presentation errors are not network/model failures.
            provider_called = False
            # Capture measured duration and provider-reported input/output tokens for this one model call.
            telemetry.model_call(perf_counter() - started,
                input_tokens=getattr(getattr(result, 'usage', None), 'prompt_tokens', None),
                output_tokens=getattr(getattr(result, 'usage', None), 'completion_tokens', None))
            # A provider content-filter stop is a guardrail outcome, not a successful business action.
            if result.finish_reason == 'content_filter':
                # Record the provider's filter result without saving its prompt or response text.
                telemetry.guardrail('provider_content_filter', 'blocked', 'provider_filtered')
            # Batch validation is meaningful only for a workbench-backed billing turn.
            if workbench is not None:
                # Inspect the structured returned tool calls before AutoGen dispatches any member.
                workbench.prepare_batch(result.content)
            # Read provider-reported token counts from the completed result.
            usage = result.usage
            # Add usage to this turn's counters when a workbench is present.
            if workbench is not None:
                # Count this successful provider call once.
                workbench.model_calls += 1
                # Accumulate all input tokens across this turn's model/tool iterations.
                workbench.input_tokens += usage.prompt_tokens
                # Accumulate all generated output tokens across the same iterations.
                workbench.output_tokens += usage.completion_tokens
            # Log model latency and usage for troubleshooting without printing conversation content.
            log.info("billing_model seconds=%.3f input_tokens=%s output_tokens=%s",
                     perf_counter() - started, usage.prompt_tokens, usage.completion_tokens)
            # Return the original provider result so the normal AutoGen loop continues.
            return result
        # Include cancellation in failure handling while allowing it to propagate.
        except BaseException:
            # Only record a failed provider call if the failure happened during that provider operation.
            if provider_called:
                # Save elapsed failed-call timing; unavailable token counts remain unknown.
                telemetry.model_call(perf_counter() - started, failed=True)
            # Write a concise failure timing record.
            log.info("billing_model seconds=%.3f status=failed", perf_counter() - started)
            # Propagate the original exception instead of fabricating a successful reply.
            raise


# Describe the local MCP subprocess and the trusted settings inherited by its tools.
def parameters():
    # Run mcp_server.server with this environment's Python, project cwd, billing paths and refund rules; MCP
    # reads may wait up to 60 seconds.
    return StdioServerParams(command=sys.executable, args=["-m", "mcp_server.server"],
                            cwd=str(settings.ROOT), env={**os.environ,
                            "BILLING_DB": str(settings.BILLING_DB),
                            "INVOICE_DIR": str(settings.INVOICE_DIR),
                            "RETURN_WINDOW_DAYS": settings.RETURN_WINDOW_DAYS,
                            "CASHIER_REFUND_LIMIT": settings.CASHIER_REFUND_LIMIT,
                            "CASHIER_REFUND_CURRENCY": settings.CASHIER_REFUND_CURRENCY}, read_timeout_seconds=60)


# Extract a structured JSON dictionary from MCP text-content blocks.
def decode_result(result):
    # MCP can return multiple content blocks; inspect them in order.
    for block in result.result:
        # A block need not contain text, so retrieve content defensively.
        content = getattr(block, "content", None)
        # Only JSON-decode text content, not binary/image objects.
        if isinstance(content, str):
            # A malformed block should not prevent trying a later valid block.
            try:
                # Parse the tool's serialized payload.
                value = json.loads(content)
                # The application expects an object containing fields such as ok, rows and error.
                if isinstance(value, dict):
                    # Return the first usable structured result.
                    return value
            # Skip invalid JSON instead of exposing its parser exception to the model.
            except ValueError:
                # Continue to the next content block.
                pass
    # If no valid object exists, return an explicit failure rather than invented data.
    return {"ok": False, "error": "Tool returned an unreadable response."}


# Produce a short factual receipt from a successful tool result, independent of model wording.
def change_receipt(name, arguments, data):
    """A brief local action receipt, independent of later model wording."""
    # A rejected operation must never receive a success receipt.
    if not data.get('ok'):
        # Report no completed action for unsuccessful data.
        return None
    # Prefer the tool's actual order target, falling back to its submitted structured ID.
    target = data.get('order_id') or arguments.get('order_id')
    # Read the structured action; an omitted refund action uses the tool's request default.
    action = arguments.get('action', 'request' if name == 'process_refund' else None)
    # Draft creation has a distinct receipt from final order placement.
    if name == 'request_order':
        # Confirm only that the draft exists; this does not imply an invoice or inventory deduction.
        return f'Draft {target} was created.'
    # An order placement receipt names its issued invoice.
    if name == 'place_order':
        # Use the actual invoice number returned by the tool.
        return f'Order {target} was placed. Invoice {data.get("invoice_number")} was issued.'
    # Coupon validation alone is insufficient; the result must say the code was actually applied.
    if name == 'apply_promotion' and data.get('applied') is True:
        # Name the supplied coupon and affected order in the completed-change receipt.
        return f'Coupon {arguments.get("promo_code")} was applied to order {target}.'
    # Profile changes use a separate receipt from saved-address operations.
    if name == 'manage_customer' and action == 'update':
        # Acknowledge that the customer profile update completed.
        return 'Your customer details were updated.'
    # Select an address receipt using exact action identifiers, not chat wording.
    if name == 'manage_customer' and action in ('add_address','update_address','delete_address','set_default_address'):
        # Map each completed address operation to a specific readable acknowledgment.
        return {'add_address':'Your delivery address was saved.',
                'update_address':'Your delivery address was updated.',
                'delete_address':'Your saved delivery address was removed.',
                'set_default_address':'Your default delivery address was updated.'}[action]
    # An invoice correction receipt is emitted only for the correction action.
    if name == 'download_invoice' and action == 'correct_details':
        # Identify the invoice whose stored contact details changed.
        return f'Contact details on invoice {arguments.get("invoice_number")} were corrected.'
    # Draft management supports several distinct mutations.
    if name == 'manage_draft':
        # Describe each supported structured draft action, including shipping/delivery and address selection.
        labels = {'replace_items':'had its draft items updated', 'cancel':'was cancelled',
            'apply_store_credit':'had its store credit reservation updated',
            'report_billing_issue':'had a billing review note recorded',
            'ship':'was marked shipped', 'deliver':'was marked delivered',
            'select_address':'had its delivery address selected; review the current quote before placement'}
        # Only known action labels produce a receipt.
        if action in labels:
            # Combine the actual order ID and that action's completed description.
            return f'Order {target} {labels[action]}.'
    # A refund request, approval and escalation have different business meanings.
    if name == 'process_refund':
        # Keep pending requests distinct from approved local-ledger refunds and review flags.
        labels = {'request':'has a pending refund request',
            'approve':'has an approved refund in the local ledger',
            'escalate':'has a recorded refund review flag'}
        # Select the applicable completed refund action.
        if action in labels:
            # Return a factual status receipt for the affected order.
            return f'Order {target} {labels[action]}.'
        # Bulk requests can have mixed per-order outcomes.
        if action == 'bulk_request':
            # Tell the caller to use each returned status instead of claiming every refund succeeded.
            return 'The bulk refund request was processed. Review the returned status for each order.'
    # Reads or unrecognized receipt shapes have no mutation receipt.
    return None


# Map exact tool/action identifiers to the kind of customer proposal that must be reviewed.
def customer_change(name, values):
    """Map structured tool actions to saved terms; never inspect chat wording."""
    # Inspect the structured action field, never the customer's sentence.
    action = values.get('action')
    # Placing an existing draft is a protected customer change.
    if name == 'place_order':
        # Use the same proposal action identifier as shopping-state validation.
        return 'place_order'
    # Only the listed draft actions need customer approval terms at this layer.
    if name == 'manage_draft':
        # Normalize tool actions such as cancel into the saved proposal vocabulary.
        return {'replace_items':'replace_items', 'cancel':'cancel_order',
            'apply_store_credit':'apply_store_credit',
            'report_billing_issue':'report_billing_issue'}.get(action)
    # Treat a profile update as a reviewed customer change.
    if name == 'manage_customer' and action == 'update':
        # Return its proposal identifier.
        return 'update_customer'
    # Saved-address mutations also use reviewed structured proposals.
    if name == 'manage_customer' and action in ('add_address','update_address','delete_address','set_default_address'):
        # Their action identifiers already match the proposal schema.
        return action
    # Refund requests and escalation/bulk actions each have their own proposal kind.
    if name == 'process_refund':
        # Map the refund tool's default request action to process_refund and the others to their saved kinds.
        return {'request':'process_refund', 'escalate':'escalate_refund',
            'bulk_request':'bulk_refund'}.get(action or 'request')
    # Invoice-detail correction changes stored customer-facing records.
    if name == 'download_invoice' and action == 'correct_details':
        # Bind it to a correct_invoice proposal.
        return 'correct_invoice'
    # Other tool calls do not require this customer-proposal gate.
    return None


# Fill omitted optional refund arguments from the exact saved review for the same order.
def approved_refund_arguments(arguments, proposal):
    """Omitted optional fields retain the exact saved terms, never new values."""
    # Copy submitted arguments before filling defaults.
    values = dict(arguments)
    # Only a matching process_refund proposal can supply the missing values.
    if (isinstance(proposal, dict) and proposal.get('action') == 'process_refund'
            and proposal.get('order_id') == values.get('order_id')):
        # Normalize saved refund terms into the same typed comparison representation as current arguments.
        saved = canonical_refund_arguments(proposal.get('arguments'))
        # These optional values materially affect the refund amount, items or destination.
        for key in ('amount', 'items', 'disposition', 'target_order_id'):
            # Fill only omitted values; do not replace explicitly changed terms.
            if values.get(key) is None and key in saved:
                # Copy nested saved items so downstream processing cannot alter the saved approval.
                values[key] = deepcopy(saved[key])
    # Return the exact effective arguments that validation and dispatch must both use.
    return values


# Validate structured approval terms; semantic consent remains the model's responsibility.
def customer_terms_error(name, arguments, proposal):
    """Check saved IDs and typed values only; the model interprets human approval.

    Draft creation has no proposal gate. Financial changes stay bound to the
    reviewed order snapshot without any confirmation-word or sentence matching.
    """
    # Determine whether this tool action needs a saved customer proposal.
    action = customer_change(name, arguments)
    # Read-only operations and draft creation have no proposal requirement here.
    if not action:
        # Return no validation error for such calls.
        return None
    # Use a failure message that explicitly states no change was executed.
    detail = 'No change was executed. Review the current terms before confirming this change.'
    # A missing/non-object proposal cannot authorize a protected change.
    if not isinstance(proposal, dict):
        # Ask for current reviewed terms instead of dispatching the mutation.
        return detail
    # Accept the older cancel_draft proposal identifier for the equivalent cancel_order operation.
    actions = {action, 'cancel_draft'} if action == 'cancel_order' else {action}
    # The saved and proposed action must describe the same operation.
    if proposal.get('action') not in actions:
        # Reject approval reuse for a different action.
        return detail
    # Account/address/bulk changes have no single order target; other operations bind an exact order ID.
    order_id = None if action in ('update_customer','bulk_refund','add_address','update_address','delete_address','set_default_address') else arguments.get('order_id')
    # A review for another order cannot authorize this target.
    if proposal.get('order_id') != order_id:
        # Return the unchanged-terms error.
        return detail
    # Placement has no editable financial arguments. Its complete order and
    # delivery terms are bound by the saved fingerprint, not model display data.
    # Placement binds its whole snapshot fingerprint; other relevant changes also compare their explicit
    # arguments.
    bound = action != 'place_order' and (action != 'cancel_order' or proposal.get('arguments') is not None or bool(arguments.get('reason')))
    # Malformed typed financial terms must fail closed as a validation error.
    try:
        # Refund normalization has additional optional-field rules; other actions use the general
        # canonicalizer.
        canonical = canonical_refund_arguments if action == 'process_refund' else canonical_proposal_arguments
        # Calculate the effective normalized arguments, including only previously reviewed refund defaults.
        current = canonical(approved_refund_arguments(arguments, proposal)
                            if action == 'process_refund' else arguments)
        # A submitted refund request must contain a strictly positive quoted amount.
        if action == 'process_refund' and Decimal(current.get('amount','0')) <= 0:
            # Reject missing/zero/negative refund amounts before the tool runs.
            return 'No change was executed. A refund request needs its exact quoted positive amount.'
        # Sorted canonical JSON compares the actual financial terms independent of dictionary key order.
        if bound and json.dumps(canonical(proposal.get('arguments')),sort_keys=True) != json.dumps(current,sort_keys=True):
            # Reject a mutation whose terms differ from the saved proposal.
            return detail
    # Invalid types or canonicalization failures cannot count as matching approval.
    except (ValueError, TypeError):
        # Return the same safe no-change error.
        return detail
    # No mismatch was found; the business tool still validates ownership, stock and eligibility.
    return None


# Identify structured mutations for checkpointing, error recovery and observability.
def mutating_call(name, values):
    # Include draft creation, coupon application, staff approval and shipping/address changes; pure reads are
    # excluded.
    return (customer_change(name,values) is not None or name == 'request_order'
        or name == 'apply_promotion' and values.get('action','apply') == 'apply'
        or name == 'process_refund' and values.get('action') == 'approve'
        or name == 'manage_draft' and values.get('action') in ('ship','deliver','select_address'))


# Classify mutation metadata for observability without letting malformed metadata break tool rejection.
def observed_mutation(name, values):
    """Malformed metadata must not change the existing tool-rejection path."""
    # Keep optional telemetry classification separate from the real dispatch error path.
    try:
        # Use the same structured mutation definition as dispatch.
        return mutating_call(name, values)
    # If malformed data cannot be classified, observability must not replace the real tool validation.
    except Exception:
        # Return a conservative metadata fallback; this does not authorize or execute anything.
        return False


# Replace unreliable model success claims with actual completed receipts and the latest dispatch rejection.
def blocked_action_reply(content, workbench, customer=False):
    """Never let a model turn a rejected dispatch into a success claim."""
    # Use the last authoritative tool-dispatch block reason.
    detail = workbench.action_blocks[-1]
    # Earlier successful actions still need to remain visible.
    if workbench.completed_changes:
        # Clarify that the later rejected action made no additional change.
        detail = detail.replace('No change was executed.', 'No additional change was executed.')
    # Combine completed receipts and the blocked-operation message in execution order.
    visible = '\n'.join([*workbench.completed_changes, detail])
    # Read customer state metadata only from a valid transport envelope.
    envelope = reply_envelope(content) if customer else None
    # Retrieve the proposed shopping-memory update separately from visible English.
    update = envelope.get('shopping_update') if isinstance(envelope, dict) else None
    # A blocked customer refund can prepare a review when no mutation has completed in this turn.
    if customer and workbench.refund_review and not workbench.completed_changes:
        # A blocked structured request can prepare a review, never grant its own
        # approval. The tracker quotes and validates it before saving any terms.
        # First check whether the model already supplied a usable typed review.
        try:
            # Validate its pending_confirmation structure through the Proposal schema.
            candidate = Proposal.model_validate(update.get('pending_confirmation') if isinstance(update, dict) else None)
            # An empty review for the same refund omits the financial terms the customer needs to approve.
            if (candidate.action == 'process_refund' and candidate.order_id == workbench.refund_review.get('order_id')
                    and not candidate.arguments):
                # Force preparation of the review from the actual structured refund request instead.
                raise ValueError('Use the structured refund request terms instead of an empty review.')
        # Recover missing or invalid reviews without granting approval.
        except (ValueError, TypeError):
            # Copy any valid state update before replacing its pending proposal.
            update = deepcopy(update) if isinstance(update, dict) else {}
            # Normalize the captured request before storing reviewable arguments.
            try:
                # Use structured request fields rather than reconstructing them from reply prose.
                values = canonical_proposal_arguments(workbench.refund_review)
            # Malformed request terms cannot be converted into a valid confirmation.
            except (ValueError, TypeError) as exc:
                # Explain the validation failure without claiming a request was submitted.
                return 'No refund request was submitted. ' + str(exc)
            # Save the exact refund action, actual order and normalized arguments for a later semantic
            # approval.
            update['pending_confirmation'] = {'action':'process_refund',
                'order_id':workbench.refund_review.get('order_id'),
                'summary':'Review the refund request before submitting it.', 'arguments':values}
            # Return one visible reply plus internal shopping-state metadata in the transport envelope.
            return json.dumps({'reply':visible, 'shopping_update':update})
    # A blocked placement similarly needs a current draft/order review rather than repeated blind writes.
    if customer and workbench.checkout_review and not workbench.completed_changes:
        # A model-selected placement without a saved approval is a request to
        # review, never permission to write. Bind the next review to its actual
        # tool target even if the final answer omitted the memory envelope.
        # Keep a valid existing proposal if the model already produced one.
        try:
            # Validate the current pending proposal structure.
            Proposal.model_validate(update.get('pending_confirmation') if isinstance(update, dict) else None)
        # If no valid review exists, prepare one using the tool's actual placement target.
        except (ValueError, TypeError):
            # Copy the update so previous state objects remain untouched.
            update = deepcopy(update) if isinstance(update, dict) else {}
            # Save a placement proposal that ShoppingTracker will bind to current order and delivery facts.
            update['pending_confirmation'] = {'action':'place_order',
                'order_id':workbench.checkout_review, 'summary':'Review the current order before placement.'}
            # Return the review envelope for state processing.
            return json.dumps({'reply':visible, 'shopping_update':update})
    # Other valid shopping updates may also contain a pending confirmation.
    if isinstance(update, dict):
        # Avoid mutating the original parsed envelope.
        update = deepcopy(update)
        # Read the candidate before clearing it in the copied state.
        proposal = update.get('pending_confirmation')
        # Default to no pending approval unless the following validation succeeds.
        update['pending_confirmation'] = None
        # Treat malformed review metadata as untrusted.
        try:
            # Validate and serialize the proposal using only defined non-null fields.
            proposed = Proposal.model_validate(proposal).model_dump(exclude_none=True)
            # Translate exact proposal action IDs into readable action descriptions.
            labels = {'place_order':'place the order', 'replace_items':'replace the draft items',
                'cancel_draft':'cancel the draft', 'cancel_order':'cancel the order',
                'process_refund':'submit a pending refund request',
                'update_customer':'update your customer details',
                'apply_store_credit':'reserve store credit for the draft',
                'correct_invoice':'correct invoice contact details',
                'report_billing_issue':'record a billing review note',
                'escalate_refund':'record a refund review flag',
                'bulk_refund':'submit the specified bulk refund requests',
                'apply_promotion':'apply the specified coupon',
                'add_address':'save your delivery address', 'update_address':'update your saved delivery address',
                'delete_address':'remove your saved delivery address', 'set_default_address':'change your default delivery address'}
            # Read the reviewed order target if this action has one.
            target = proposed.get('order_id')
            # Build a confirmation summary for the precise structured operation.
            summary = 'Please confirm that you want to ' + labels[proposed['action']]
            # Order-bound changes need their target stated explicitly.
            if target:
                # Include that actual order ID.
                summary += ' for ' + target
            # Finish the base confirmation sentence.
            summary += '.'
            # Coupon application must name the exact supplied code being reviewed.
            if proposed['action'] == 'apply_promotion':
                # Append that reviewed coupon identifier.
                summary += ' Coupon: ' + str(proposed.get('promo_code','')) + '.'
            # A placement proposal must refer to a currently available owned order.
            if proposed['action'] == 'place_order' and target:
                # Read its current authoritative snapshot for this customer.
                current = order_snapshot(target, workbench.customer_id)
                # An absent/inaccessible order cannot be proposed for placement.
                if not current:
                    # Reject the unusable review.
                    raise ValueError('Order is unavailable.')
                # ShoppingTracker renders the full saved quote and address;
                # keeping this summary short also supports large valid baskets.
            # Store the concise summary alongside the complete typed terms.
            proposed['summary'] = summary
            # Validate the generated review too; never truncate financial terms.
            # Validate the generated review again rather than truncating or weakening financial fields.
            proposed = Proposal.model_validate(proposed).model_dump(exclude_none=True)
            # Keep only the validated proposal in the shopping update.
            update['pending_confirmation'] = proposed
            # Show its confirmation summary after the no-change explanation.
            visible += '\n\n' + summary
        # An invalid proposal is dropped; the original dispatch rejection remains visible.
        except (ValueError, TypeError, KeyError):
            # Do not invent an alternative approval when validation fails.
            pass
        # Serialize the visible reply and separate state update together once.
        return json.dumps({'reply':visible, 'shopping_update':update})
    # Staff/plain replies without customer state return the visible text directly.
    return visible


# Build an order snapshot for staff, resolving its real owner from the database.
def staff_refund_snapshot(order_id):
    # Reject missing/non-string order IDs before querying.
    if not isinstance(order_id, str) or not order_id:
        # Signal that no valid snapshot is available.
        return None
    # Open a read-only connection and close it even on failure.
    with closing(connect(readonly=True)) as conn:
        # Use a bound parameter for the exact order ID and retrieve its owner.
        row = conn.execute('SELECT customer_id FROM orders WHERE order_id=?', (order_id,)).fetchone()
    # Build the standard ownership-aware order snapshot only when the order exists.
    snapshot = order_snapshot(order_id, row[0]) if row else None
    # Augment a valid snapshot with trusted staff-facing ownership facts.
    if snapshot:
        # Record the database owner's customer ID.
        snapshot['customer_id'] = row[0]
        # Bind ownership into the fingerprint so a review cannot survive reassignment to another account.
        snapshot['fingerprint'] = hashlib.sha256((snapshot['fingerprint']+'\0'+row[0]).encode()).hexdigest()
    # Return the current snapshot or None.
    return snapshot


# Resolve staff refund omissions from an existing immutable pending customer request.
def staff_refund_arguments(arguments, snapshot):
    """Resolve omitted approval parameters to the immutable pending request."""
    # Read the request embedded in the current order snapshot.
    request = snapshot.get('refund_request') if snapshot else None
    # Only pending recorded requests supply staff approval defaults.
    if not request or request['status'] != 'pending':
        # Without such a request, return a copy for the normal business validation to reject or inspect.
        return dict(arguments)
    # Work on a separate effective argument object.
    values = dict(arguments)
    # Bind the account to the database-resolved owner.
    values['customer_id'] = snapshot['customer_id']
    # Rebuild only recorded item codes and quantities from the pending request's JSON.
    stored_items = ([{'item_code':row['item_code'],'quantity':row['quantity']}
                     for row in json.loads(request['items_json'])] if request.get('items_json') else [])
    # Default amount/reason/destination/items to the recorded request; never assume physical return receipt.
    defaults = {'amount':format(Decimal(str(request['amount'])),'.2f'), 'reason':request['reason'],
        'disposition':request.get('disposition') or 'original_method',
        'target_order_id':request.get('target_order_id'),
        'items':stored_items or None,
        'return_received':False}
    # Fill omitted fields individually rather than replacing explicitly supplied approval terms.
    for key,value in defaults.items():
        # An empty staff reason also inherits the original customer's recorded reason.
        if values.get(key) is None or key == 'reason' and not values.get(key):
            # Apply the saved default for this field.
            values[key] = value
    # Normalize a well-shaped item list into the minimal financial comparison structure.
    if isinstance(values.get('items'),list) and all(isinstance(row,dict) and
            'item_code' in row and 'quantity' in row for row in values['items']):
        # Strip display-only item metadata while keeping actual IDs and quantities.
        values['items'] = [{'item_code':row['item_code'],'quantity':row['quantity']} for row in values['items']]
    # Return the effective approval arguments.
    return values


# Check live refund eligibility before showing staff a proposal or dispatching approval.
def staff_approval_error(arguments, mode):
    """Check current recorded eligibility locally before dispatching approval."""
    # Reuse the same business validator as the refund tool to avoid conflicting eligibility rules.
    from mcp_server.tools.service_workflows import refund_approval_check
    # This preflight reads data only and closes its connection on every path.
    with closing(connect(readonly=True)) as conn:
        # Read order/request eligibility within one consistent SQLite snapshot.
        conn.execute('BEGIN')
        # Find the exact order for the exact resolved customer using bound parameters.
        order = conn.execute('SELECT * FROM orders WHERE order_id=? AND customer_id=?',
                             (arguments.get('order_id'), arguments.get('customer_id'))).fetchone()
        # A missing/other-customer order is ineligible.
        if not order:
            # Explain that no approval occurred.
            return 'No refund was approved. Order not found for this customer.'
        # Check the role, request, return window, amount/shipping limit and recorded receipt or explicit
        # waiver.
        eligibility = refund_approval_check(conn, order, mode,
            return_received=arguments.get('return_received') is True,
            waiver_reason=arguments.get('waiver_reason') or '')
    # Any eligibility failure blocks staff approval.
    if not eligibility['ready_to_approve']:
        # Combine the business validator's concrete reasons in the no-approval response.
        return 'No refund was approved. ' + ' '.join(eligibility['reasons'])
    # Return no error when the request is currently eligible; the write tool still rechecks atomically.
    return None


# Validate a model-selected staff review against current structured database facts.
def staff_confirmation(content, mode='admin', customer_id=None, errors=None):
    """Bind a structured model proposal to DB facts; never parse visible prose."""
    # Parse only a well-formed internal response envelope.
    envelope = reply_envelope(content)
    # Extract the one visible reply separately.
    reply = plain_reply(content)
    # Read the model's optional structured staff proposal.
    proposed = envelope.get('pending_staff_action') if isinstance(envelope,dict) else None
    # Require exactly the supported proposal fields and an object of arguments.
    if (not isinstance(proposed,dict) or set(proposed) != {'action','order_id','arguments'}
            or not isinstance(proposed['arguments'],dict)):
        # If no valid proposal exists, retain the plain reply without saving approval terms.
        return reply, None
    # Read the candidate action and exact order ID.
    action, order_id = proposed['action'], proposed['order_id']
    # Admins may also ship/deliver; cashiers are limited to refund approval/escalation.
    allowed = {'approve','escalate','ship','deliver'} if mode == 'admin' else {'approve','escalate'}
    # Verify structured action membership and target type before reading records.
    if not isinstance(action,str) or action not in allowed or not isinstance(order_id,str):
        # Discard unsupported proposals without upgrading role access.
        return reply, None
    # Resolve the real order owner and current fingerprint.
    snapshot = staff_refund_snapshot(order_id)
    # Respect any staff-selected customer scope as well as actual record existence.
    if not snapshot or customer_id and snapshot['customer_id'] != customer_id:
        # Do not save a cross-customer/unavailable proposal.
        return reply, None
    # Combine only proposed business arguments and the validated structured action.
    arguments = {**proposed['arguments'],'action':action}
    # Read the existing refund request for this order.
    request = snapshot.get('refund_request')
    # Approval requires a current eligibility check before it can even be proposed.
    if action == 'approve':
        # Pass database-bound order/customer facts to the same role-aware approval validator.
        problem = staff_approval_error({**arguments, 'order_id':order_id,
                                       'customer_id':snapshot['customer_id']}, mode)
        # An invalid refund must be explained before asking staff to confirm it.
        if problem:
            # When supplied, collect the authoritative block reason for final response handling.
            if errors is not None:
                # Record that reason.
                errors.append(problem)
            # Return the rejection with no pending staff approval.
            return problem, None
    # Approval/escalation cannot manufacture a refund request that the customer never submitted.
    if action in ('approve','escalate') and not request:
        # Retain the original reply but save no action.
        return reply, None
    # For an approval, compare the recorded status and explicit refund terms checked below.
    if action == 'approve':
        # Only pending requests can be approved.
        if request['status'] != 'pending':
            # Drop proposals for already processed/rejected requests.
            return reply, None
        # Fill omitted optional terms from the actual request.
        arguments = staff_refund_arguments(arguments,snapshot)
        # Reason, refund destination and target draft must match the recorded request exactly.
        if (str(arguments.get('reason')) != request['reason'] or
                arguments.get('disposition') != (request.get('disposition') or 'original_method') or
                arguments.get('target_order_id') != request.get('target_order_id')):
            # Reject changed refund terms rather than silently authorizing them.
            return reply, None
        # Compare financial amounts with Decimal, avoiding binary floating-point differences.
        try:
            # The reviewed amount must equal the customer's recorded request amount.
            if Decimal(str(arguments.get('amount'))) != Decimal(str(request['amount'])):
                # A different amount cannot be approved using this proposal.
                return reply, None
        # Invalid numeric input is not equivalent approval.
        except (ValueError, TypeError, InvalidOperation):
            # Drop an unusable monetary proposal.
            return reply, None
    # Validate remaining business arguments and build a readable review.
    try:
        # Canonicalize the structured arguments for stable later comparison.
        bound = canonical_proposal_arguments(arguments)
        # Render all reviewable business terms from those canonical values.
        details = proposal_details(bound)
    # Invalid argument structures cannot become saved staff approvals.
    except (ValueError, TypeError):
        # Return no pending action for invalid terms.
        return reply, None
    # Show the exact order and the terms staff are being asked to confirm.
    reply += f'\n\nOrder: {order_id}\n' + details
    # Return the visible review and a separate typed action context bound to the current fingerprint.
    return reply, {'kind':'staff_refund' if action == 'approve' else 'staff_action',
        'action':action,'order_id':order_id,'order_fingerprint':snapshot['fingerprint'],
        'arguments':bound}


# Extend MCP with role scope, approved-term checks, factual receipts and UI attachments.
class RecordingWorkbench(McpWorkbench):
    # Initialize reusable transport state; BillingAgent resets all request-local fields before each turn.
    def __init__(self, server_params=None):
        # Start with the provided server description or the default local MCP subprocess parameters.
        super().__init__(server_params or parameters())
        # Collect successful invoice-download references for the API response.
        self.downloads = []
        # Collect validated chart attachments separately from model prose.
        self.charts = []
        # Optionally publish charts while the request is still running.
        self.chart_callback = None
        # Optionally checkpoint the request before its first mutation.
        self.mutation_callback = None
        # Track whether that pre-write checkpoint has already been recorded this turn.
        self._change_recorded = False
        # Track chart provenance for deduplication of the same verified source.
        self._chart_origins = {}
        # Remember whether any tool failed during this turn.
        self.failed = False
        # Track whether the MCP transport itself needs a restart.
        self.transport_failed = False
        # Cache only tool definitions, never cached business data.
        self._tool_schemas = None
        # Default to customer scope until trusted API context sets the actual role.
        self.mode = 'customer'
        # Hold the trusted associated customer ID, not a chat-claimed identity.
        self.customer_id = None
        # Attach shopping memory only for verified customer turns.
        self.shopping_tracker = None
        # Retain literal customer-supplied coupon identifiers for provenance validation.
        self.coupon_codes = []
        # Keep the current user message for identifier provenance, not intent matching.
        self.user_message = ''
        # Keep prior user messages for the same provenance check.
        self.user_history = []
        # Hold the saved structured review for the current semantic approval turn.
        self.action_proposal = None
        # Remember a blocked placement target that needs a fresh review.
        self.checkout_review = None
        # Remember a blocked refund request whose exact terms need a review.
        self.refund_review = None
        # Accumulate authoritative rejections for final response correction.
        self.action_blocks = []
        # Accumulate confirmed mutation receipts in execution order.
        self.completed_changes = []
        # Distinguish uncertain writes from definite business validation rejections.
        self.uncertain_change = False
        # Distinguish a completed write whose response processing later failed.
        self.completed_response_error = False
        # Store an internal dispatch failure warning without automatically replaying a write.
        self.dispatch_problem = None
        # Accumulate time spent in the timed checkpoint/MCP portion of the tool pipeline.
        self.tool_seconds = 0.0
        # Count timed tool attempts, including a possible checkpoint failure before MCP dispatch.
        self.tool_calls = 0
        # Count successful provider requests for this turn.
        self.model_calls = 0
        # Accumulate provider-reported input tokens.
        self.input_tokens = 0
        # Accumulate provider-reported output tokens.
        self.output_tokens = 0
        # Flag an unsafe multi-call batch before any member executes.
        self._batch_blocked = False
        # Serialize dispatch so receipts, navigation and checkpoints stay in execution order.
        self._dispatch_lock = asyncio.Lock()
        # Start with a neutral observed-response contract.
        self.contract = ResponseContract()
        # Store successful structured tool arguments/results as evidence.
        self.receipts = []
        # Collect validated display tables outside model history.
        self.tables = []
        # Collect table preparation errors without inventing a successful attachment.
        self.table_errors = []
        # Collect full report datasets outside the model context.
        self.reports = []
        # Switch to read-only recovery after an uncertain mutation.
        self.repair_only = False

    # Expose an evidence-status property without additional database or model calls.
    @property
    # Determine whether successful receipts satisfy the observed result contract.
    def has_evidence(self):
        # Use actual tool evidence rather than claims in the assistant's prose.
        return evidence_satisfied(self.contract, self.receipts)

    # Build replayable analytics context from verified reads instead of saving stale chart values.
    def analytics_context(self):
        """Save read recipes and scope, never model claims or stale observations."""
        # Only admin analytics with actual successful receipts can produce this context.
        if self.mode != 'admin' or not self.receipts:
            # Return no analytics recipe for other roles or empty turns.
            return None
        # Inspect the most recent successful tool receipt.
        name, arguments, data = self.receipts[-1]
        # Billing-analysis reports have a defined reusable filter/report structure.
        if name == 'billing_analysis':
            # A complete store overview needs only its verified filters and selected chart mode.
            if data.get('report') == 'store_overview':
                # Save its explicit kind, actual filters and presentation mode.
                value = {'kind': 'store_overview', 'filters': data['filters'],
                         'chart_mode': data.get('chart_mode', 'overview')}
            # Other reports preserve only the supported structured report parameters.
            else:
                # Allow identifiers, date scope, report kind, ranking and trend/chart options in saved
                # recipes.
                allowed = {'customer_id', 'order_id', 'date_from', 'date_to', 'report',
                           'top_n', 'trend_period', 'trend_metric', 'chart_type', 'chart_mode'}
                # Exclude unrelated/model-invented fields from the replayable parameters.
                saved = {key: value for key, value in arguments.items() if key in allowed}
                # Prefer actual result filters when the tool resolved defaults.
                saved.update(data.get('filters', {}))
                # Distinguish invoice trends from other billing-analysis recipes.
                value = {'kind': 'invoice_trend' if data.get('report') == 'invoice_trend'
                         else 'billing_analysis', 'arguments': saved}
        # SQL charts preserve executed SQL and bound parameters, not assistant-generated numerical summaries.
        elif name == 'execute_sql_query':
            # Collect the contiguous group of SQL reads that produced the latest analysis.
            sources = []
            # Inspect receipts backward so only the most recent SQL group is reused.
            for tool, args, result in reversed(self.receipts):
                # A different intervening tool ends that contiguous SQL context.
                if tool != 'execute_sql_query':
                    # Stop collecting older unrelated sources.
                    break
                # Read the exact executed query from its structured arguments.
                query = args.get('query', '')
                # Schema introspection or invalid query objects are not analytical data recipes.
                if not isinstance(query, str) or query.lstrip().lower().startswith('pragma'):
                    # Skip those entries.
                    continue
                # Save the read query, binding values and actual output column names.
                source = {'query': query, 'parameters': args.get('parameters') or [],
                          'columns': result.get('columns', [])}
                # Avoid storing the same recipe more than once.
                if source not in sources:
                    # Append a distinct verified source.
                    sources.append(source)
            # Restore execution order in the saved SQL source list.
            value = {'kind': 'sql', 'sources': list(reversed(sources))}
        # Other tool types have no reusable admin analytics context here.
        else:
            # Return no context rather than guessing a recipe.
            return None
        # Sanitize size, structure and scope before returning the context for persistence.
        return sanitize_analytics_context(value)

    # Convert successful executed results into trusted receipts, tables and full report attachments.
    def record_result(self, name, arguments, data):
        # Rejected results are not evidence and cannot produce successful report presentation.
        if not data.get('ok'):
            # Leave the current attachments/receipts unchanged for a failure.
            return
        # Presentation follows the executed tool and its verified rows. User
        # wording never selects a report or rewrites the model's tool arguments.
        # Derive presentation coverage from the actual returned result and executed arguments.
        self.contract = result_contract(name, arguments, data)
        # A store overview carries complete datasets and explicit coverage metadata.
        if name == 'billing_analysis' and data.get('report') == 'store_overview':
            # Require the result's scope and completeness to match the requested report contract.
            if self.contract.kind != 'store_overview':
                # Fail instead of presenting partial/mismatched data as a full-store report.
                raise ValueError('The returned report scope or coverage does not match the executed tool arguments.')
            # Store complete rows outside the LLM context. Display previews are
            # bounded separately and the full datasets remain downloadable.
            # Validate full report attachments before sending them to the UI or saving them.
            from context.reports import sanitize_reports
            # Package the actual datasets with their coverage, resolved filters and notes.
            report = {'title': 'Store analytics', 'datasets': data['datasets'],
                      'coverage': {**data['coverage'], 'filters': data['filters']}, 'notes': data['notes']}
            # Strictly validate both previous reports and this candidate report.
            prepared = sanitize_reports([*self.reports, report], strict=True)
            # Retain only the validated report objects.
            self.reports = prepared
            # Overview/no-chart modes provide tabular previews rather than forcing a chart dashboard.
            if data.get('chart_mode', 'overview') in ('overview', 'none'):
                # Preview up to twelve datasets; the full validated report remains separately available.
                for source in data['datasets'][:12]:
                    # Attach each source using its real title, columns, rows and scope description.
                    self.add_table(source['title'], source['columns'], source['rows'],
                                   coverage=source['scope'])
        # A SQL response without a requested chart may be best presented as a table.
        elif name == 'execute_sql_query' and not arguments.get('chart'):
            # Recognize a typed category-count result independently of whether it covers all inventory.
            rows = category_rows(data)
            # Only a well-formed category result gets category-specific presentation.
            if rows is not None:
                # Mark completeness only when the executed query proves a full inventory overview.
                complete = self.contract.kind == 'inventory_overview'
                # Complete inventory gets a consistently ordered, fully validated category list.
                if complete:
                    # Use verified complete rows rather than an arbitrary SQL preview.
                    rows = inventory_rows(arguments, data)
                # Choose customer-facing category/count column headings.
                columns = ['Category', 'Products']
                # A third returned field represents the number of distinct in-stock products.
                if len(data['columns']) == 3:
                    # Add its readable heading without exposing internal item codes.
                    columns.append('In stock products')
                # Attach real category counts and explicitly state truncation, coverage and product-type
                # meaning.
                table = self.add_table('Products by category', columns, rows,
                    truncated=data.get('truncated', False),
                    coverage=('All products, including out-of-stock products.' if complete else
                              'Returned category groups for this request; filters, limits and truncation still apply.')
                              + ' Counts are product types, not stock units.')
                # Persist category navigation only when the table was actually accepted for display.
                if (table is not None and self.shopping_tracker is not None
                        and table in sanitize_tables(self.tables)):
                    # Tables are not copied into model history. Preserve a
                    # small, trusted navigation list for category follow-ups.
                    # Update the trusted customer shopping-state object.
                    state = self.shopping_tracker.state
                    # Clear item-level navigation because the displayed choices are categories, not individual
                    # items.
                    state['displayed_product_ids'] = []
                    # Save the executed category query, parameters, displayed labels, count and completeness
                    # for follow-ups.
                    state['product_query'] = {'kind':'category_overview',
                        'query':arguments.get('query'),
                        'parameters':deepcopy(arguments.get('parameters') or []),
                        'displayed_categories':[row['Category'] for row in table['rows'][:40]],
                        'row_count':len(table['rows']),
                        'truncated':table['truncated'] or len(table['rows'])>40,
                        'complete':complete and not table['truncated']}
            # Other multi-row/multi-column SQL results become generic tables, excluding schema PRAGMA
            # responses.
            elif (not arguments.get('query','').lstrip().lower().startswith('pragma') and
                  (len(data.get('columns', [])) > 1 or len(data.get('rows', [])) != 1)):
                # Read an optional total-count column supplied by a paginated product query.
                counts = [row[data['columns'].index('matching_products')] for row in data['rows']] if 'matching_products' in data['columns'] else []
                # Trust the total only when every row agrees on a nonnegative integer at least as large as the
                # page.
                total = counts[0] if counts and all(type(n) is int and n == counts[0] and n >= len(data['rows']) for n in counts) else None
                # Attach returned rows with their real total/truncation and a scope-aware coverage note.
                self.add_table('Current results', data['columns'], data['rows'],
                    total_rows=total,truncated=data.get('truncated',False),
                    coverage=('More matching records exist; this table is a preview.' if data.get('truncated')
                              else 'Returned records for this request. Any filters or page limits still apply.'))
        # When a tool did not supply charts, its structured data may have useful specialized tables.
        elif not data.get('chart') and not any(source.get('chart') for source in data.get('chart_results', [])):
            # Presentation errors should be captured without fabricating a table.
            try:
                # Use the tool-specific table builder on successful verified data.
                for table in tool_tables(name,data,arguments):
                    # Deduplicate tables and respect the existing twelve-table attachment limit.
                    if table not in self.tables and len(self.tables)<12:
                        # Retain the accepted table for the current API response.
                        self.tables.append(table)
            # Capture malformed presentation data as an attachment error.
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
                # Save a concrete table failure reason for UI/result metadata.
                self.table_errors.append(str(exc))
        # Keep independent copies of the actual successful call and result as execution evidence.
        self.receipts.append((name, deepcopy(arguments), deepcopy(data)))

    # Validate and deduplicate one table while preserving coverage and pagination facts.
    def add_table(self, title, columns, rows, coverage=None, total_rows=None, truncated=False):
        # The table validator can reject inconsistent columns, rows or metadata.
        try:
            # Build the normalized table artifact from the exact values supplied by the tool.
            table = table_artifact(title, columns, rows, coverage=coverage,total_rows=total_rows,truncated=truncated)
        # A bad table does not become a successful attachment.
        except (ValueError, TypeError) as exc:
            # Record the table validation error.
            self.table_errors.append(str(exc))
            # Return without adding malformed rows.
            return
        # Avoid duplicates and keep the existing attachment-count bound.
        if table not in self.tables and len(self.tables) < 12:
            # Add this validated table.
            self.tables.append(table)
        # Return a table only if it is actually among this turn's attachments.
        return table if table in self.tables else None

    # Reject unsafe structured multi-tool batches before AutoGen runs any member.
    def prepare_batch(self, content):
        # A normal text result contains no tool-call batch.
        calls = content if isinstance(content, list) else []
        # Determine whether one structured call has a known read-only action.
        def read_only(call):
            # Known read tools are safe to consider in a multi-call batch.
            if getattr(call, 'name', None) in BATCH_READ_TOOLS:
                # Mark this call as read-only.
                return True
            # Coupon validation is read-only, but coupon application is a mutation.
            if getattr(call, 'name', None) == 'apply_promotion':
                # Malformed coupon arguments must not qualify the call as safe.
                try:
                    # Read the structured JSON tool arguments.
                    arguments = json.loads(call.arguments)
                    # Allow this special case only for an object with action exactly validate.
                    return isinstance(arguments, dict) and arguments.get('action') == 'validate'
                # Invalid JSON or missing call metadata is unsafe for batch dispatch.
                except (ValueError, TypeError, AttributeError):
                    # Reject the call as a read-only batch member.
                    return False
            # Unknown/mutating tools are not permitted in a multi-call batch.
            return False
        # If any member is unsafe, block the entire multi-call batch before the first write.
        self._batch_blocked = len(calls) > 1 and any(not read_only(call) for call in calls)
        # Only multi-call proposals need a batch timing/debug record.
        if len(calls) > 1:
            # Log batch size and validation result without printing arguments.
            log.info('billing_batch calls=%s allowed=%s', len(calls), not self._batch_blocked)

    # Expose only permitted role/action tool schemas to the model.
    async def list_tools(self):
        # Our server registers a fixed tool set. Cache schemas, never billing data.
        # Fetch the fixed server definitions once per live MCP connection.
        if self._tool_schemas is None:
            # Cache schema metadata, not query results or customer facts.
            self._tool_schemas = await super().list_tools()
        # Choose the allowlist from the server-established admin/cashier/customer role.
        allowed = ADMIN_TOOLS if self.mode == 'admin' else CASHIER_TOOLS if self.mode == 'cashier' else CUSTOMER_TOOLS
        # Copy allowed definitions so per-role edits cannot change the shared schema cache.
        schemas = [deepcopy(s) for s in self._tool_schemas if s['name'] in allowed]
        # After an uncertain change, recovery is restricted to reads.
        if self.repair_only:
            # Remove every non-read tool from the repair-only schema list.
            schemas = [schema for schema in schemas if schema['name'] in READ_TOOLS]
        # Apply role restrictions and hide trusted fields on each copied schema.
        for schema in schemas:
            # Get this tool's JSON parameter schema.
            parameters = schema.get('parameters', {})
            # Staff use recorded refund requests rather than creating new customer requests.
            if self.mode in ('admin','cashier') and schema['name'] == 'process_refund':
                # The order determines its owner; staff never need to guess it.
                # Remove model-supplied ownership; the database resolves the order's actual customer.
                parameters.get('properties',{}).pop('customer_id',None)
                # Keep required-field metadata consistent with the removed field.
                if 'required' in parameters:
                    # Staff callers no longer need to invent customer_id.
                    parameters['required'] = [key for key in parameters['required'] if key != 'customer_id']
                # Read the structured action schema.
                action = parameters.get('properties',{}).get('action',{})
                # Expose only staff-supported refund actions.
                action['enum'] = ['approve','status','escalate']
                # Remove the customer-request default so staff must select an explicit action.
                action.pop('default',None)
                # Require one staff action without duplicating an existing required entry.
                parameters['required'] = list(dict.fromkeys([*parameters.get('required',[]),'action']))
            # Customer management in chat supports verified profile/address edits, not account registration.
            if schema['name'] == 'manage_customer':
                # Explain the permitted actions and their distinct profile/address fields in the exposed
                # schema.
                schema['description'] = ('Update the signed-in customer or saved delivery addresses after confirmation. '
                    'Use action=update with name for profile; address actions omit profile fields. add_address/update_address accept address fields, '
                    'delete_address/set_default_address accept address_id. Account registration uses the account form.')
                # Read the customer-management action definition.
                action = parameters.get('properties', {}).get('action', {})
                # Remove any implicit action so the model must identify the intended edit.
                action.pop('default', None)
                # Limit action choices to the actual profile/address operations implemented here.
                action['enum'] = ['update','add_address','update_address','delete_address','set_default_address']
                # Require that structured action explicitly.
                parameters['required'] = list(dict.fromkeys([*parameters.get('required', []), 'action']))
            # Store-data chart generation is exposed only to admins.
            if self.mode != 'admin' and schema['name'] == 'execute_sql_query':
                # Remove the admin-only visualization guidance from other roles' tool description.
                schema['description'] = schema['description'].split('For requested admin visuals', 1)[0].rstrip()
                # Hide the chart parameter from customer/cashier SQL tools.
                parameters.get('properties', {}).pop('chart', None)
                # Update required fields if chart was included there.
                if 'required' in parameters:
                    # Ensure the hidden chart argument is not still advertised as required.
                    parameters['required'] = [f for f in parameters['required'] if f != 'chart']
            # Trusted scope, confirmation and reviewed delivery are injected by application code.
            for field in (*SCOPE_FIELDS, 'confirmed', 'expected_delivery'):
                # Remove each such field from model-writable properties.
                parameters.get('properties', {}).pop(field, None)
            # Also remove hidden fields from the required list.
            if 'required' in parameters:
                # Keep the exposed schema internally consistent without granting model control of trusted
                # scope.
                parameters['required'] = [f for f in parameters['required'] if f not in (*SCOPE_FIELDS,'confirmed','expected_delivery')]
        # Return the role-filtered copied schemas.
        return schemas

    # Stop the MCP connection and invalidate its cached definitions.
    async def stop(self):
        # A later server connection must retrieve its own tool schemas.
        self._tool_schemas = None
        # Delegate subprocess/session cleanup to the MCP adapter.
        await super().stop()

    # Public tool entry point: validate batch shape, serialize dispatch and contain event-loop failures.
    async def call_tool(self, name, arguments=None, cancellation_token=None, **kwargs):
        # Arguments must be an object or absent, never a scalar/list masquerading as tool metadata.
        if arguments is not None and not isinstance(arguments, dict):
            # Record the structured argument guardrail rejection.
            telemetry.guardrail('tool_arguments', 'blocked', 'object_required')
            # Record that this call was blocked before execution.
            telemetry.tool_blocked(name, mutating=observed_mutation(name, {}))
            # Return an AutoGen tool error that explicitly states no tool was run.
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok':False,'error':'Tool arguments must be an object. No tool was executed.'}))])
        # All members of an unsafe multi-call batch are rejected consistently.
        if self._batch_blocked:
            # Record why the proposed batch cannot execute.
            telemetry.guardrail('tool_batch', 'blocked', 'unsafe_parallel_changes')
            # Capture the blocked call's name and mutation classification without its sensitive arguments.
            telemetry.tool_blocked(name, mutating=observed_mutation(name, arguments or {}))
            # Explain that writes/invoice generation must be sequential and no member executed.
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok': False, 'error': 'No tools in this batch were executed. Request writes and '
                'invoice generation one at a time. Only independent read-only tools may be batched.'}))])
        # AutoGen dispatches a returned batch concurrently. Preserve tool/result
        # ordering for shopping navigation and checkpoints; savings come from
        # fewer model round trips, not concurrent database access.
        # Serialize actual dispatches so checkpoints and navigation follow deterministic execution order.
        async with self._dispatch_lock:
            # Remember completed changes before this dispatch to distinguish later response failures.
            completed_before = len(self.completed_changes)
            # Remember actual call count to identify a local rejection that never reached MCP.
            calls_before = self.tool_calls
            # Contain unexpected dispatch exceptions so AutoGen receives a completed error event.
            try:
                # Run the internal scope/approval/business dispatch pipeline.
                result = await self._call_tool(name, arguments, cancellation_token, **kwargs)
                # An error with no new MCP call was a local block, not an executed tool failure.
                if self.tool_calls == calls_before and result.is_error:
                    # Record that local rejection separately in observability.
                    telemetry.tool_blocked(name, mutating=observed_mutation(name, arguments or {}))
                # Return the structured result to AutoGen's tool loop.
                return result
            # Unexpected dispatch failures require safe recovery, never an automatic mutation replay.
            except Exception:
                # AutoGen's workbench event loop can otherwise wait for an event
                # that a failed dispatch never publishes. Cancellation must still
                # propagate, and an interrupted write must never be replayed.
                # Record the failure stack without printing the tool arguments.
                log.exception('billing_tool_dispatch_failed tool=%s',name)
                # Mark an internal dispatch guardrail error.
                telemetry.guardrail('tool_dispatch', 'error', 'internal_dispatch_error')
                # Flag both the turn and its transport as failed.
                self.failed = self.transport_failed = True
                # Save the safe final warning for this failed dispatch.
                self.dispatch_problem = 'An internal tool error prevented a reliable result. No automatic retry was performed.'
                # Use an empty argument object when none was supplied.
                values = arguments or {}
                # Determine whether the attempted structured action could modify stored state.
                mutating = mutating_call(name,values)
                # Only mutations require write-completion uncertainty handling.
                if mutating:
                    # A receipt added during this dispatch proves the mutation completed before presentation
                    # failed.
                    if len(self.completed_changes) > completed_before:
                        # Mark a completed write with failed response handling.
                        self.completed_response_error = True
                    # Without a completed receipt, do not assume the attempted write was rolled back.
                    else:
                        # Mark the outcome uncertain so callers inspect saved status before trying again.
                        self.uncertain_change = True
                # Mutation recovery must be read-only even if the transport failure was local.
                if mutating:
                    # Prevent additional writes in this repair path.
                    self.repair_only = True
                # Return an explicit error, including factual receipts only when completion is known.
                return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                    'ok':False,'error':('The saved change completed, but response handling failed. Do not repeat the change.'
                        if self.completed_response_error else 'The tool encountered an internal error. '
                        'For a billing change, completion is unverified; inspect the saved order or request status '
                        'before retrying. Do not repeat a write automatically.'),
                    **({'change_completed':True,'receipts':self.completed_changes[completed_before:]}
                       if self.completed_response_error else {})}))])

    # Internal dispatch binds trusted scope and reviewed terms before calling the real MCP tool.
    async def _call_tool(self, name, arguments=None, cancellation_token=None, **kwargs):
        # An uncertain-write recovery turn cannot execute another mutation.
        if self.repair_only and name not in READ_TOOLS:
            # Record the read-only recovery rejection.
            telemetry.guardrail('read_only_recovery', 'blocked', 'change_not_allowed')
            # Return a no-change tool error without dispatching to MCP.
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok': False, 'error': 'This is a read-only retrieval turn. No billing change was executed.'}))])
        # Copy model-selected business arguments before applying trusted validations/defaults.
        authorization = dict(arguments or {})
        # Discard model-supplied expected delivery; only the saved reviewed address can provide it.
        authorization.pop('expected_delivery', None)
        # Staff order/refund mutations resolve the current record and real ownership first.
        if self.mode in ('admin','cashier') and name in ('process_refund','manage_draft'):
            # Read the authoritative order/refund snapshot using the exact structured order ID.
            snapshot = staff_refund_snapshot(authorization.get('order_id'))
            # Any explicitly selected customer must own that order.
            if snapshot and self.customer_id and snapshot['customer_id'] != self.customer_id:
                # Record the ownership mismatch.
                telemetry.guardrail('order_ownership', 'blocked', 'selected_owner_mismatch')
                # Explain that the order is outside the selected customer scope.
                detail = 'No change was executed. This order does not belong to the selected customer.'
                # Keep the reason for final reply correction.
                self.action_blocks.append(detail)
                # Return the rejection before a tool can run.
                return ToolResult(name=name,is_error=True,result=[TextResultContent(content=json.dumps({'ok':False,'error':detail}))])
            # These exact staff action IDs are protected operations tied to saved review terms.
            protected = (name == 'process_refund' and authorization.get('action') in ('approve','escalate')
                         or name == 'manage_draft' and authorization.get('action') in ('ship','deliver'))
            # Use saved staff context only for a protected structured action.
            proposed = self.action_proposal if protected else None
            # Reject a changed target, action or order/refund fingerprint before reusing the prior review.
            if proposed and (not snapshot or proposed.get('order_id') != authorization.get('order_id')
                    or proposed.get('action') != authorization.get('action')
                    or snapshot['fingerprint'] != proposed.get('order_fingerprint')):
                # Record the stale-review guardrail failure.
                telemetry.guardrail('approval_terms', 'blocked', 'stale_staff_terms')
                # Require a fresh review after current order/refund facts changed.
                detail = 'No change was executed. The reviewed order or refund terms changed. Review the current terms and obtain fresh approval.'
                # Preserve the authoritative rejection for the visible answer.
                self.action_blocks.append(detail)
                # Stop dispatch with no write.
                return ToolResult(name=name,is_error=True,result=[TextResultContent(content=json.dumps({'ok':False,'error':detail}))])
            # A current snapshot supplies trusted ownership.
            if snapshot:
                # Override any model-selected customer ID with the actual database owner.
                authorization['customer_id'] = snapshot['customer_id']
            # Previously reviewed arguments may fill omissions, never overwrite explicit revised terms.
            if proposed:
                # Inspect each saved business field.
                for key,value in proposed.get('arguments',{}).items():
                    # Fill only omitted fields or an empty reason from those saved terms.
                    if authorization.get(key) is None or key == 'reason' and not authorization.get(key):
                        # Copy nested values so the saved proposal cannot be altered by dispatch.
                        authorization[key] = deepcopy(value)
            # Refund approval must pass current business rules even after human confirmation.
            if authorization.get('action') == 'approve':
                # Check live role, request, return-window, amount and receipt/waiver eligibility.
                problem = staff_approval_error(authorization, self.mode)
                # Any current eligibility error blocks execution.
                if problem:
                    # Record the failed eligibility guardrail.
                    telemetry.guardrail('refund_eligibility', 'blocked', 'request_not_eligible')
                    # Keep the business rejection visible at the end of the turn.
                    self.action_blocks.append(problem)
                    # Return without approving the refund.
                    return ToolResult(name=name,is_error=True,result=[TextResultContent(content=json.dumps({'ok':False,'error':problem}))])
                # Record that the current pending request is eligible for this role.
                telemetry.guardrail('refund_eligibility', 'pass', 'current_request_eligible')
                # Resolve missing approval values from the immutable request before comparing/dispatching.
                authorization = staff_refund_arguments(authorization, snapshot)
            # If there is saved staff review context, its arguments must still match.
            if proposed:
                # Invalid canonical forms cannot establish an approval match.
                try:
                    # Compare canonical sorted JSON so ordering differences do not weaken the actual financial
                    # comparison.
                    matched = (json.dumps(canonical_proposal_arguments(authorization),sort_keys=True)
                               == json.dumps(canonical_proposal_arguments(proposed['arguments']),sort_keys=True))
                # Missing/malformed arguments count as a mismatch.
                except (ValueError, TypeError, KeyError):
                    # Fail closed on invalid comparison data.
                    matched = False
                # Changed explicit terms cannot inherit the saved human approval.
                if not matched:
                    # Record the argument mismatch.
                    telemetry.guardrail('approval_terms', 'blocked', 'staff_arguments_changed')
                    # Ask staff to review the revised terms rather than silently applying them.
                    detail = 'No change was executed. The tool arguments differ from the reviewed terms. Review the revised terms before approval.'
                    # Save the no-change reason.
                    self.action_blocks.append(detail)
                    # Return the mismatch before any MCP write.
                    return ToolResult(name=name,is_error=True,result=[TextResultContent(content=json.dumps({'ok':False,'error':detail}))])
                # Record a match only after canonical argument equality succeeds.
                telemetry.guardrail('approval_terms', 'pass', 'saved_staff_terms_match')
        # Customer mutations require a still-current saved proposal.
        if self.mode == 'customer' and customer_change(name, authorization):
            # Read the proposal selected from trusted shopping memory.
            proposal = self.action_proposal
            # Order-bound proposals also require a live snapshot check.
            if proposal and proposal.get('order_id'):
                # Read this customer's current order facts.
                current = order_snapshot(proposal['order_id'], self.customer_id)
                # A missing order or changed fingerprint invalidates the prior review.
                if not current or current['fingerprint'] != proposal.get('order_fingerprint'):
                    # Clear stale approval rather than letting the model execute with outdated terms.
                    self.action_proposal = None
                # Invoice correction targets an invoice but approval is bound to its owning order.
                elif name == 'download_invoice' and authorization.get('action') == 'correct_details':
                    # Require that the reviewed invoice number matches the proposed correction.
                    if current['invoice_number'] == authorization.get('invoice_number'):
                        # Add the current owned order ID for exact canonical validation.
                        authorization['order_id'] = current['order_id']
                    # A different invoice cannot reuse the review.
                    else:
                        # Clear the unusable proposal.
                        self.action_proposal = None
        # Check the effective structured customer arguments against the still-current review.
        blocked = customer_terms_error(name, authorization, self.action_proposal) if self.mode == 'customer' else None
        # A mismatch/missing proposal requires a review, not a write.
        if blocked:
            # Record the approval-term guardrail rejection.
            telemetry.guardrail('approval_terms', 'blocked', 'customer_terms_unavailable_or_changed')
            # Capture the actual placement target for a later validated checkout review.
            if name == 'place_order':
                # Save only the exact structured order ID.
                self.checkout_review = authorization.get('order_id')
            # Capture a refund review only for the already verified signed-in customer.
            elif (name == 'process_refund' and customer_change(name, authorization) == 'process_refund'
                    and authorization.get('customer_id') == self.customer_id and self.customer_id):
                # Preserve the structured refund request terms for local review preparation.
                self.refund_review = deepcopy(authorization)
            # Keep the authoritative no-change reason for the final response.
            self.action_blocks.append(blocked)
            # Return a confirmation-required tool result without executing the mutation.
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok': False, 'error': blocked, 'confirmation_required': True}))])
        # A protected customer change has now matched its saved terms.
        if self.mode == 'customer' and customer_change(name, authorization):
            # Record the successful term comparison.
            telemetry.guardrail('approval_terms', 'pass', 'saved_customer_terms_match')
            # Refund execution must use the same effective defaults that were compared with the review.
            if name == 'process_refund' and customer_change(name, authorization) == 'process_refund':
                # Dispatch the exact reviewed refund amount/items/destination, including saved omissions.
                authorization = approved_refund_arguments(authorization, self.action_proposal)
        # Customers may use only coupon codes they actually supplied.
        if self.mode == 'customer' and name == 'apply_promotion':
            # Read the proposed literal coupon identifier.
            code = authorization.get('promo_code')
            # Check provenance against current/prior user messages and previously recorded supplied IDs.
            if not customer_supplied_coupon(code, self.user_message, self.user_history, self.coupon_codes):
                # Record a fabricated/assistant-origin coupon rejection.
                telemetry.guardrail('coupon_provenance', 'blocked', 'customer_code_required')
                # Explain the code requirement without exposing any other coupons.
                detail = 'The customer must supply the exact coupon code before it can be checked or applied. No discount changed.'
                # Save the policy rejection for the final reply.
                self.action_blocks.append(detail)
                # Do not validate/apply an undisclosed code through MCP.
                return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                    'ok': False, 'error':detail}))])
            # Record that this identifier has customer provenance.
            telemetry.guardrail('coupon_provenance', 'pass', 'customer_supplied_code')
            # Keep at most thirty unique normalized supplied identifiers in recent order.
            self.coupon_codes = list(dict.fromkeys([*self.coupon_codes, code.strip().upper()]))[-30:]
            # Persist provenance in customer shopping state when that tracker exists.
            if self.shopping_tracker is not None:
                # Store a separate list copy for future turns.
                self.shopping_tracker.state['provided_coupon_codes'] = list(self.coupon_codes)
        # Use the validated/resolved effective arguments for the actual dispatch.
        arguments = authorization
        # Checkout must bind the saved reviewed delivery selection.
        if self.mode == 'customer' and name == 'place_order':
            # Read the address snapshot from the pending order proposal.
            delivery = (self.action_proposal or {}).get('delivery')
            # A missing saved address/review cannot authorize placement.
            if not isinstance(delivery, dict) or not delivery.get('delivery_address'):
                # Record the missing-delivery guardrail result.
                telemetry.guardrail('delivery_selection', 'blocked', 'saved_review_missing')
                # Explain the address selection and complete-order review requirement.
                detail = 'No change was executed. Select a delivery address and review the complete current order before confirming placement.'
                # Save the no-change reason.
                self.action_blocks.append(detail)
                # Return without placing the order.
                return ToolResult(name=name,is_error=True,result=[TextResultContent(content=json.dumps({'ok':False,'error':detail}))])
            # Record that saved delivery terms are present.
            telemetry.guardrail('delivery_selection', 'pass', 'saved_delivery_review')
            # Pass a copy of that exact delivery snapshot for the business tool to compare atomically.
            arguments = {**arguments, 'expected_delivery':deepcopy(delivery)}
        # Only tools whose real server schema defines confirmed receive that trusted field.
        if any(schema['name'] == name and 'confirmed' in schema.get('parameters',{}).get('properties',{})
               for schema in (self._tool_schemas or [])):
            # Set the flag from this structured validated mutation path; the model never supplies it directly.
            arguments = {**(arguments or {}), 'confirmed':bool(mutating_call(name, arguments or {}))}
        # The model cannot choose its execution role or customer scope.
        # Inject the verified role, account scope and supplied-code provenance over any model fields.
        arguments = {**(arguments or {}), 'execution_mode': self.mode,
                     'execution_customer_id': self.customer_id,
                     'execution_coupon_codes': list(self.coupon_codes)}
        # Start timing actual checkpoint/transport work.
        started = perf_counter()
        # Until a call returns, there is no known tool result.
        result = None
        # Track whether a request was handed to MCP at all.
        dispatched = False
        # Always account for elapsed dispatch time, including failures.
        try:
            # Before the first mutation, record the request's write checkpoint once when supported.
            if mutating_call(name, authorization) and self.mutation_callback and not self._change_recorded:
                # A checkpoint failure must block the write rather than proceed untracked.
                try:
                    # Persist the pre-write marker through the API-supplied callback.
                    self.mutation_callback()
                # Record checkpoint failure without hiding the underlying exception.
                except Exception:
                    # Emit a write-checkpoint guardrail error.
                    telemetry.guardrail('write_checkpoint', 'error', 'checkpoint_failed')
                    # Stop before dispatching the mutation.
                    raise
                # Record successful checkpointing before the write.
                telemetry.guardrail('write_checkpoint', 'pass', 'marked_before_write')
                # Do not repeat the checkpoint for later tools in this turn.
                self._change_recorded = True
            # The next awaited operation may reach the MCP server.
            dispatched = True
            # Call the actual MCP tool using trusted arguments and the linked cancellation token.
            result = await super().call_tool(name, arguments, cancellation_token, **kwargs)
        # Measure the attempt whether it completed, failed or was cancelled.
        finally:
            # Compute this actual tool attempt's elapsed duration.
            elapsed = perf_counter() - started
            # Accumulate transport/tool time for the turn summary.
            self.tool_seconds += elapsed
            # Count this timed pipeline attempt once, even if its pre-write checkpoint raised before dispatch.
            self.tool_calls += 1
            # No returned result after dispatch means the attempt failed before completion was reported.
            if dispatched and result is None:
                # Capture an error timing event without assuming a write was rolled back.
                telemetry.tool_call(name, elapsed, outcome='error', mutating=observed_mutation(name, authorization))
            # Log tool name and elapsed time without sensitive parameter values.
            log.info("billing_tool tool=%s seconds=%.3f", name, elapsed)
        # Keep any prior failure remembered across later successful calls.
        self.failed = self.failed or result.is_error
        # A later completed MCP call proves the channel recovered. Keep the
        # turn's error history, but do not cold-start healthy transports again.
        # The most recent result indicates whether the live transport is healthy again.
        self.transport_failed = bool(result.is_error)
        # Decode the structured business result separately from MCP transport success/failure.
        data = decode_result(result)
        # Record actual elapsed tool timing and business/transport outcome for this attempt.
        telemetry.tool_call(name, elapsed,
            outcome='error' if result.is_error else 'success' if data.get('ok') else 'rejected',
            mutating=observed_mutation(name, authorization))
        # Distinguish transport errors, successful validation and explicit business-rule rejections.
        telemetry.guardrail('tool_validation', 'error' if result.is_error else 'pass' if data.get('ok') else 'blocked',
                            'transport_error' if result.is_error else 'backend_accepted' if data.get('ok') else 'backend_rejected')
        # A failed transport during a mutation has an unknown completion outcome.
        if result.is_error and mutating_call(name, authorization):
            # Restrict recovery to reads and prevent blind write replay.
            self.uncertain_change = self.repair_only = True
        # A normal returned business rejection is a definite no-change result.
        if not data.get('ok') and not result.is_error and mutating_call(name, authorization):
            # Preserve its concrete reason for the final English answer.
            self.action_blocks.append('No change was executed. ' + str(data.get('error','The change was rejected.')))
        # Invoice attachment failures must also override any model claim of a completed download.
        if name == 'download_invoice' and not data.get('ok'):
            # Record the exact invoice preparation failure.
            self.action_blocks.append('The invoice could not be prepared. ' + str(data.get('error','No invoice was attached.')))
        # Derive a factual mutation receipt from the successful tool output.
        receipt = change_receipt(name, arguments, data)
        # Only operations with real completed receipts enter the completed-change list.
        if receipt:
            # Keep that receipt for reliable final reporting.
            self.completed_changes.append(receipt)
        # A completed protected customer mutation consumes its saved proposal.
        if data.get('ok') and self.mode == 'customer' and customer_change(name, authorization):
            # One human approval authorizes one matching change, never a second
            # model-selected action later in the same turn.
            # Clear consumed approval so it cannot authorize another write or cause repeated confirmations.
            self.action_proposal = None
        # Selecting an address may change the previously reviewed checkout fingerprint.
        if data.get('ok') and self.mode == 'customer' and name == 'manage_draft' and authorization.get('action') == 'select_address':
            # Read the updated owned draft snapshot after address selection.
            current = order_snapshot(data.get('order_id'), self.customer_id)
            # Retain a placement proposal only when it still targets this unchanged current snapshot.
            if (not self.action_proposal or not current
                    or self.action_proposal.get('order_id') != current['order_id']
                    or self.action_proposal.get('order_fingerprint') != current['fingerprint']):
                # Otherwise require a fresh review of the new delivery/order terms.
                self.action_proposal = None
        # A completed staff refund approval also consumes its proposal.
        if data.get('ok') and self.mode in ('admin','cashier') and name == 'process_refund' and arguments.get('action') == 'approve':
            # Clear the staff approval context.
            self.action_proposal = None
        # Prepare trusted table/report evidence while containing malformed tool-result shapes.
        try:
            # Record actual results and construct attachments from them.
            self.record_result(name, arguments, data)
        # A result that cannot be presented honestly is returned as an error.
        except ValueError as exc:
            # Report presentation failure; do not invent evidence or silently claim a complete report.
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok': False, 'error': str(exc)}))])
        # Only admin SQL/analysis tools produce store-data chart attachments.
        if name in ('execute_sql_query', 'billing_analysis') and self.mode == 'admin':
            # Read the current structured result before chart validation.
            data = decode_result(result)
            # If no charts remain, old object identity metadata is stale.
            if not self.charts:
                # Clear old deduplication provenance.
                self._chart_origins.clear()
            # Compute a stable origin from the tool/arguments, excluding trusted execution-only scope fields.
            origin = json.dumps({'tool':name, 'args':{k:v for k,v in arguments.items()
                if k not in ('chart','chart_type')}}, sort_keys=True)
            # Remember the pre-call chart count for accurate attachment receipts.
            attached_before = len(self.charts)
            # SQL yields one candidate source; analysis may return many chart-result sources.
            sources = [data] if name == 'execute_sql_query' else data.get('chart_results', [])
            # Only a successful business result may produce charts.
            if data.get('ok'):
                # Validate each candidate independently so one bad chart does not erase valid siblings.
                for source in sources:
                    # A source with no chart specification is just data.
                    if not source.get('chart'):
                        # Skip it rather than guessing a chart type.
                        continue
                    # A malformed chart request must be reported without claiming a rendered visual.
                    try:
                        # Validate chart metadata and rows against supported chart rules.
                        chart = chart_from_result(source)
                    # Remove an invalid specification and retain its concrete error.
                    except (ValueError, KeyError, TypeError) as exc:
                        # Do not leave a rejected chart advertised as an attachment.
                        source.pop('chart', None)
                        # Attach the validation failure to that source.
                        source['chart_error'] = str(exc)
                        # Continue validating other requested charts.
                        continue
                    # Deduplicate only the same read scope; equal totals for two
                    # different date/customer filters are still distinct reports.
                    # Compare chart meaning/data independent of its decorative title.
                    identity = {k:v for k,v in chart.items() if k != 'title'}
                    # Deduplicate only the same chart from the same verified query/report origin.
                    if not any(self._chart_origins.get(id(saved)) == origin and
                               identity == {k:v for k,v in saved.items() if k != 'title'} for saved in self.charts):
                        # Add the accepted visual specification to this turn's attachments.
                        self.charts.append(chart)
                        # Bind the stored chart object to its source identity.
                        self._chart_origins[id(chart)] = origin
            # Collect source-specific chart failures for the final delivery metadata.
            errors = [str(source['chart_error']) for source in sources if source.get('chart_error')]
            # A successful SQL read does not establish that it supplied a chart.
            # Report actual newly attached count, total count and errors so the model cannot invent delivery.
            data['chart_delivery'] = {'attached_this_call': len(self.charts) - attached_before,
                'total_attached_this_turn': len(self.charts), 'errors': errors}
            # Log only attachment counts and failure summaries.
            log.info('billing_chart tool=%s attached=%s total_attached=%s errors=%s',
                     name, len(self.charts) - attached_before, len(self.charts), errors)
            # Replace the first textual tool block with the verified chart-delivery payload.
            for block in result.result:
                # Non-text blocks are not JSON transport objects.
                if isinstance(getattr(block, 'content', None), str):
                    # Serialize updated delivery metadata for the model's reflection.
                    block.content = json.dumps(data)
                    # One structured payload update is sufficient.
                    break
            # Publish incremental charts only when this call actually added something.
            if self.chart_callback and len(self.charts) > attached_before:
                # Pass copied chart data to the UI callback so it cannot mutate workbench state.
                self.chart_callback(deepcopy(self.charts))
            # Update the matching successful receipt with final chart-delivery facts.
            if self.receipts and self.receipts[-1][0] == name:
                # Keep copied arguments and chart-enriched result as the authoritative evidence.
                self.receipts[-1] = (name, deepcopy(arguments), deepcopy(data))
        # Invoice-generation tools may supply a downloadable file reference.
        if name == "download_invoice":
            # Decode the validated invoice result.
            data = decode_result(result)
            # A download reference requires both business success and ready status.
            if data.get("ok") and data.get("ready"):
                # Store only invoice/customer identifiers; the protected download API resolves the actual
                # file.
                self.downloads.append({"invoice_number": data["invoice_number"], "customer_id": data["customer_id"]})
        # Customer turns update navigation/draft facts from actual completed tool observations.
        if self.shopping_tracker is not None and self.mode == 'customer':
            # Let ShoppingTracker verify and record those observations for the bound customer.
            self.shopping_tracker.observe(name, arguments, decode_result(result), self.customer_id)
        # Large store analytics retain full data outside the model context.
        if name == 'billing_analysis':
            # Read the original analysis payload after its UI attachments have been prepared.
            data = decode_result(result)
            # Only the complete store-overview payload needs dataset-row compaction here.
            if data.get('ok') and data.get('report') == 'store_overview':
                # The model needs scope/coverage and receipts, not thousands of
                # repeated observations already attached to the answer.
                # Keep dataset names/columns/coverage for reflection but remove full rows already attached
                # separately.
                data['datasets'] = [{key:value for key,value in source.items() if key != 'rows'}
                                    for source in data['datasets']]
                # Remove duplicate chart-row payloads from the text returned to the model.
                data.pop('chart_results', None)
                # Replace the tool's serialized text with this compact metadata representation.
                for block in result.result:
                    # Update only a textual block.
                    if isinstance(getattr(block, 'content', None), str):
                        # Serialize the compact data without changing full UI/report attachments.
                        block.content = json.dumps(data)
                        # Stop after the primary structured block.
                        break
        # Read the current compact result before adding table-delivery facts.
        data = decode_result(result)
        # Only successful result envelopes carry successful table-delivery metadata.
        if data.get('ok'):
            # Tell the model the actual table count and captured presentation errors.
            data['table_delivery'] = {'total_attached_this_turn':len(self.tables),
                                     'errors':list(self.table_errors)}
            # Find the textual structured result block.
            for block in result.result:
                # Leave non-text MCP content intact.
                if isinstance(getattr(block,'content',None),str):
                    # Write the table-enriched envelope.
                    block.content = json.dumps(data)
                    # Avoid rewriting unrelated content blocks.
                    break
        # Preserve every JSON value while avoiding pretty-print whitespace in
        # subsequent model requests. Do this after observation/attachment capture.
        # Measure JSON text size before and after whitespace-only compaction.
        before = after = 0
        # Inspect each text block that could contribute model input tokens.
        for block in result.result:
            # Read its optional textual content.
            content = getattr(block, 'content', None)
            # Compact only strings, never binary/image objects.
            if isinstance(content, str):
                # Count original characters for the payload timing record.
                before += len(content)
                # Malformed non-JSON text must stay unchanged.
                try:
                    # Serialize identical JSON values without unnecessary whitespace and keep Unicode
                    # characters readable.
                    compact = json.dumps(json.loads(content), ensure_ascii=False, separators=(',', ':'))
                    # Apply compaction only when it actually makes the payload smaller.
                    if len(compact) < len(content):
                        # Replace the text with the smaller equivalent JSON representation.
                        block.content = compact
                # Unparseable text is already the correct opaque content to preserve.
                except (ValueError, TypeError):
                    # Leave it unchanged.
                    pass
                # Count characters actually returned to the model.
                after += len(block.content)
        # Log payload-size savings without logging the contents.
        log.info('billing_payload tool=%s chars_before=%s chars_after=%s', name, before, after)
        # Return the final structured MCP result to AutoGen.
        return result


# Ensure AutoGen's tool-event protocol still completes when adapter/result handling raises unexpectedly.
class SafeBillingAssistant(AssistantAgent):
    """Close AutoGen's tool-event path even when schema/result handling fails."""
    # This hook is static because AutoGen invokes it without an assistant instance.
    @staticmethod
    # Wrap AutoGen's private tool executor while preserving its call signature and return shape.
    async def _execute_tool_call(tool_call, workbench, handoff_tools, agent_name,
                                 cancellation_token, stream):
        # Link the actual execution task to cancellation of the owning billing turn.
        cancellation_token.link_future(asyncio.current_task())
        # Snapshot each recording workbench's call/receipt counts for accurate failure-completion assessment.
        previous = [(wb, wb.tool_calls, len(wb.completed_changes))
                    for wb in workbench if isinstance(wb, RecordingWorkbench)]
        # Normal execution uses the existing AutoGen implementation unchanged.
        try:
            # Await that implementation and return its tool-call/result pair.
            return await AssistantAgent._execute_tool_call(tool_call,workbench,handoff_tools,
                agent_name,cancellation_token,stream)
        # Contain adapter/event errors so the assistant loop does not wait forever for a missing completion
        # event.
        except Exception:
            # Log the tool-event exception stack using only the tool name.
            log.exception('billing_tool_event_failed tool=%s',tool_call.name)
            # Decode arguments only to classify structured mutation risk during recovery.
            try:
                # Parse the call's JSON argument object.
                arguments = json.loads(tool_call.arguments)
            # Malformed JSON is not valid mutation metadata.
            except (ValueError, TypeError):
                # Use an empty metadata object for failure accounting.
                arguments = {}
            # Even valid JSON must be an object, not a scalar/list.
            if not isinstance(arguments,dict):
                # Discard the malformed shape from recovery classification.
                arguments = {}
            # Update only the recording workbenches involved in this execution.
            for wb, calls, completed in previous:
                # Mark the turn and transport as failed.
                wb.failed = wb.transport_failed = True
                # Save a warning that forbids automatic replay of an uncertain billing operation.
                wb.dispatch_problem = 'An internal tool error prevented a reliable result. No automatic retry was performed.'
                # Only mutating actions need write-completion checks.
                if mutating_call(tool_call.name,arguments):
                    # Restrict follow-up recovery to read tools.
                    wb.repair_only = True
                    # New completed receipts prove that a write finished before its event handling failed.
                    if len(wb.completed_changes) > completed:
                        # Mark completed-but-unreported response handling.
                        wb.completed_response_error = True
                    # An actual dispatch without a receipt may still have changed stored state.
                    elif wb.tool_calls > calls:
                        # Preserve that uncertainty rather than claiming a rollback.
                        wb.uncertain_change = True
            # Return a correctly paired failed tool-execution event so AutoGen can finish the request.
            return tool_call, FunctionExecutionResult(call_id=tool_call.id,name=tool_call.name,
                is_error=True,content=json.dumps({'ok':False,'error':
                    'Tool dispatch or result handling failed. Do not retry a billing change automatically; inspect its saved status.'}))


# Coordinate the local MCP transport and isolated per-request assistant/state objects.
class BillingAgent:
    # Advertise support for saved verified analysis recipes to the API layer.
    supports_analytics_context = True
    # Advertise support for persisted structured staff action context.
    supports_action_context = True
    # Advertise callbacks for incremental charts and pre-write request checkpointing.
    supports_request_progress = True
    # Keep workbench construction replaceable for controlled tests/adapters.
    workbench_factory = RecordingWorkbench

    # Initialize the engine without opening network or MCP resources immediately.
    def __init__(self):
        # Create the MCP workbench lazily or during explicit startup warming.
        self._workbench = None
        # Track whether application lifespan owns the reusable transports.
        self._managed = False
        # Serialize turns sharing this engine's mutable transport/workbench fields.
        self._lock = asyncio.Lock()

    # Warm MCP/tool schemas during API startup without calling Azure.
    async def start(self):
        """Warm the MCP process once during API startup; Azure stays lazy."""
        # Protect startup from a simultaneous chat/close operation.
        async with self._lock:
            # Start the local MCP connection if not already running.
            await self._get_workbench()
            # Keep healthy transports alive between requests under lifespan management.
            self._managed = True

    # Return the existing healthy workbench or initialize one safely.
    async def _get_workbench(self):
        # Only cold starts create a new subprocess/session.
        if self._workbench is None:
            # Use the configured workbench factory.
            self._workbench = self.workbench_factory()
            # Partially started transports must be cleaned up if startup/schema discovery fails.
            try:
                # Start the MCP adapter and child server.
                await self._workbench.start()
                # Warm its tool definitions so the first chat need not rediscover schemas.
                await self._workbench.list_tools()
            # Include cancellation/startup failures in cleanup.
            except BaseException:
                # Discard both partially initialized transport resources.
                await self._reset()
                # Propagate the original startup failure.
                raise
        # Return the ready shared workbench.
        return self._workbench

    # Detach and close the current MCP and model resources without retaining broken handles.
    async def _reset(self):
        workbench, self._workbench = self._workbench, None
        if workbench is not None:
            await workbench.stop()

    # Release lifespan-owned resources safely at application shutdown.
    async def close(self):
        # Wait for the active turn/startup to leave the engine lock.
        async with self._lock:
            # Remove lifespan ownership so later standalone calls clean up their own resources.
            self._managed = False
            # Close the transport owned by BillingAgent.
            await self._reset()

    # Expose a configuration check that the API can call before accepting chat work.
    def validate_configuration(self):
        # Get a sanitized configuration error message, if any.
        error = settings.azure_configuration_error()
        # Reject invalid configuration immediately.
        if error:
            # Raise the specific actionable message without printing secret settings.
            raise ValueError(error)

    # Run one semantic conversation turn with trusted identity, saved state and optional progress/checkpoint
    # callbacks.
    async def chat(self, message, history, customer_id, mode='customer', shopping_state=None, state_callback=None, chart_callback=None, analytics_context=None, action_context=None, mutation_callback=None):
        # Measure total time including waiting for this engine's lock.
        started = perf_counter()
        # Prevent overlapping turns from sharing mutable role, approvals or attachments.
        async with self._lock:
            # Separate lock wait from actual processing duration.
            acquired = perf_counter()
            # Keep a safe default for logging if workbench initialization fails.
            workbench = None
            # Create cancellation coordination for all child operations in this turn.
            run_token = RunCancellationToken()
            # All failure/cancellation paths must release/reset resources safely.
            try:
                # Reuse or initialize the MCP connection.
                workbench = await self._get_workbench()
                # Reset tool-duration accounting for this request only.
                workbench.tool_seconds = 0.0
                # Reset this turn's timed tool-pipeline attempt count.
                workbench.tool_calls = 0
                # Reset successful model-call count.
                workbench.model_calls = 0
                # Reset accumulated input tokens.
                workbench.input_tokens = 0
                # Reset accumulated output tokens.
                workbench.output_tokens = 0
                # Clear prior turn failure status.
                workbench.failed = False
                # Start this turn with no known transport failure.
                workbench.transport_failed = False
                # Remove invoice attachments from previous turns.
                workbench.downloads.clear()
                # Remove previous chart attachments.
                workbench.charts.clear()
                # Remove previous table attachments.
                workbench.tables.clear()
                # Clear prior presentation errors.
                workbench.table_errors.clear()
                # Remove previous full report objects.
                workbench.reports.clear()
                # Clear prior execution receipts so old data cannot count as fresh evidence.
                workbench.receipts.clear()
                # Clear previous action rejection reasons.
                workbench.action_blocks.clear()
                # Clear any prior turn's blocked checkout target.
                workbench.checkout_review = None
                # Clear any prior turn's blocked refund review candidate.
                workbench.refund_review = None
                # Clear previous completed-change receipts.
                workbench.completed_changes.clear()
                # Clear old uncertain-mutation status.
                workbench.uncertain_change = False
                # Clear old completed-write presentation failure status.
                workbench.completed_response_error = False
                # Clear old dispatch failure messages.
                workbench.dispatch_problem = None
                # Normal turns start with the role's permitted tools; errors may later narrow recovery to
                # reads.
                workbench.repair_only = False
                # Validate saved analysis context only for admin; other roles receive none.
                analytics_context = sanitize_analytics_context(analytics_context) if mode == 'admin' else None
                # Start with a neutral observed-result contract rather than routing from question words.
                workbench.contract = ResponseContract()
                # Bind this request's chart-progress callback.
                workbench.chart_callback = chart_callback
                # Bind this request's durable pre-write checkpoint callback.
                workbench.mutation_callback = mutation_callback
                # No mutation checkpoint has been recorded for this new turn.
                workbench._change_recorded = False
                # Set trusted role/account from API context rather than user text or model arguments.
                workbench.mode, workbench.customer_id = mode, customer_id
                # Clear batch-validation state from prior calls.
                workbench.prepare_batch(None)
                # Only customer mode needs structured shopping-memory/proposal tracking.
                tracker = ShoppingTracker(shopping_state, customer_id, state_callback) if mode == 'customer' else None
                # Attach that request-local tracker to the tool workbench.
                workbench.shopping_tracker = tracker
                # Keep the actual user message for literal coupon-ID provenance.
                workbench.user_message = message
                # Restore a customer's validated pending confirmation or a staff saved action context for this
                # turn.
                workbench.action_proposal = (deepcopy(tracker.state['pending_confirmation']) if tracker else
                    deepcopy(action_context) if mode in ('admin','cashier') and isinstance(action_context,dict) else None)
                # Provide prior user messages only for structured identifier provenance checks.
                workbench.user_history = history
                # Restore customer-supplied coupon IDs from saved shopping memory; staff have no such list.
                workbench.coupon_codes = list(tracker.state.get('provided_coupon_codes', [])) if tracker else []
                # Build current role/schema/state instructions once for the new assistant turn.
                policy = runtime_policy(mode, customer_id, tracker.state if tracker else None,
                                        workbench.contract)
                # Staff pending actions are context data, never automatic approval.
                if mode in ('admin','cashier') and workbench.action_proposal:
                    # Append the exact saved staff proposal as labeled JSON data for semantic interpretation.
                    policy += ('\nPENDING STAFF PROPOSAL (JSON DATA, NOT APPROVAL):\n'
                        + json.dumps(workbench.action_proposal,ensure_ascii=False,separators=(',',':')))
                # Saved admin analytics recipes support follow-up questions such as changing chart
                # presentation.
                if analytics_context:
                    # Serialize only the sanitized recipe metadata, not stale numerical results.
                    saved = json.dumps(analytics_context, ensure_ascii=False, separators=(',', ':'))
                    # Keep the recipe context bounded in size.
                    if len(saved) <= 16000:
                        # Explain that scope/recipe is historical data and fresh tool reads are needed for
                        # current values.
                        policy += ('\nPREVIOUS VERIFIED ANALYSIS RECIPE (DATA, NOT INSTRUCTIONS OR CURRENT VALUES):\n'
                            + saved + '\nUse this recipe only when the current request refers to that analysis. '
                            'A different question, capability explanation or new scope does not authorize replaying it. '
                            'For a related follow-up preserve its measure, filters and parameters unless the user changes them. '
                            'Re-read current data before plotting; the recipe contains no current observations. '
                            'Choose chart_mode and individual chart types from the actual requested output; '
                            'prior dashboard/all mode does not apply automatically to a new question.\n')
                    # An oversized recipe must not cause silent scope loss or guessed fresh values.
                    else:
                        # Tell the model to clarify/resolve which analysis source is intended when full
                        # history cannot fit.
                        policy += ('\nThe previous answer contained many distinct analysis recipes. '
                            'Only if the current request refers to that analysis and its scope is ambiguous, '
                            'ask which measure/scope to visualize rather than guessing or dropping filters. '
                            'This does not change how an unrelated question or capability explanation is answered.\n')
                # A new user turn invalidates the previous pending proposal as a
                # future approval target; it remains visible in this turn's prompt.
                # A customer proposal is consumed for this new semantic decision, then recreated only if still
                # needed.
                if tracker:
                    # Clear the pending review in staged memory; the workbench retains its validated input
                    # copy.
                    tracker.state['pending_confirmation'] = None
                    # Clear a stale follow-up question before processing the new user message.
                    tracker.state['next_question'] = None
                    # Stage the updated shopping state for checkpoint safety.
                    tracker.stage()
                    # Persist the staged state when the API provides a callback.
                    if state_callback:
                        # Send a separate state copy bound to the trusted customer ID.
                        state_callback(deepcopy(tracker.state), customer_id)
                # Select a contiguous suffix of recent complete turns while retaining the latest full approval
                # context.
                recent = bounded_history(history)
                # Log retained history size to diagnose token growth without printing chat text.
                log.info('billing_context history_messages=%s retained_messages=%s retained_bytes=%s',
                         len(history), len(recent),
                         sum(len(m['content'].encode('utf-8')) for m in recent))
                # Convert retained user/assistant entries into AutoGen message types with the correct sources.
                previous = [UserMessage(content=m["content"], source="user") if m["role"] == "user"
                            else AssistantMessage(content=m["content"], source="billing_agent") for m in recent]
                # Construct the registered agent inside the current SDK request context.
                sdk_agent = create_sdk_agent()
                agent = await sdk_agent.create_async_agent(
                        assistant_type=SafeBillingAssistant,
                        adapt_client=lambda client: ObservedSDKClient(client, workbench),
                        workbench=workbench,
                        model_context=UnboundedChatCompletionContext(initial_messages=previous),
                        system_message=policy,
                        reflect_on_tool_use=True,
                        max_tool_iterations=settings.MAX_TOOL_ITERATIONS)
                # Run the actual request; AutoGen may select multiple sequential tools when the task requires
                # them.
                result = await agent.run(task=message,cancellation_token=run_token)
                # Read the last returned event/message as the final candidate answer.
                final = result.messages[-1]
                # A tool event alone is not a valid final visible reply.
                if not isinstance(final, TextMessage):
                    # If dispatch was explicitly blocked, the local rejection can supply the final answer.
                    if workbench.action_blocks:
                        # Use an empty model answer so blocked_action_reply constructs the authoritative
                        # explanation.
                        content = ''
                    # Without a block reason, missing final text is an internal response failure.
                    else:
                        # Raise instead of returning a tool payload as the user answer.
                        raise RuntimeError("Agent did not produce a final text answer.")
                # A proper text message supplies the candidate model content.
                else:
                    # Keep that content for envelope validation and factual correction.
                    content = final.content
                # Default to no new staff approval context.
                staff_proposal = None
                # Default to no staff review text.
                review = None
                # Prepare a staff review only when no change completed and dispatch outcome is reliable.
                if (mode in ('admin','cashier') and not workbench.completed_changes
                        and not (workbench.uncertain_change or workbench.completed_response_error or workbench.dispatch_problem)):
                    # Validate any model proposal against current owner, refund request, eligibility and
                    # terms.
                    review, staff_proposal = staff_confirmation(content,mode,customer_id,workbench.action_blocks)
                # Authoritative tool blocks override unsupported model claims.
                if workbench.action_blocks:
                    # Build a visible no-change message and a safe structured review when appropriate.
                    content = blocked_action_reply(content, workbench, tracker is not None)
                    # A valid staff review can accompany the concrete block reason.
                    if staff_proposal is not None:
                        # Append the review once to that blocked-action response.
                        content += '\n\n' + review
                # A valid unblocked staff proposal becomes the visible review.
                elif staff_proposal is not None:
                    # Use the sanitized review content instead of the raw envelope.
                    content = review
                # Unknown mutation completion requires an explicit check-status warning.
                if workbench.uncertain_change:
                    # Preserve known earlier receipts but never assert this uncertain write succeeded or
                    # failed.
                    content = '\n'.join([*workbench.completed_changes,
                        'An internal tool error interrupted this request. I cannot verify whether the change completed. '
                        'Check the saved order or refund status before repeating it.'])
                # A known completed write with failed presentation needs a different warning.
                elif workbench.completed_response_error:
                    # Keep completion receipts and tell the user not to replay the change.
                    content = '\n'.join([*workbench.completed_changes,
                        'The saved change completed, but response handling failed. Do not repeat the change.'])
                # Other internal dispatch errors also override unreliable generated success text.
                elif workbench.dispatch_problem:
                    # Combine factual receipts with the stored dispatch failure explanation.
                    content = '\n'.join([*workbench.completed_changes,workbench.dispatch_problem])
                # Only a reliable turn can promote model explanation tables to UI attachments.
                if not workbench.failed:
                    # Separate readable text and optional structured/Markdown tables from the model envelope.
                    content, explanation_tables = split_reply_tables(content)
                    # Verified tool tables already carry the source rows. A
                    # model-written explanation/table is a separate output choice.
                    # Existing verified tool tables take precedence over duplicated model-written tables.
                    if not workbench.tables:
                        # Consider explanatory tables only when no verified source table was already attached.
                        for table in explanation_tables:
                            # Avoid attaching the same explanation table twice.
                            if table not in workbench.tables:
                                # Keep the accepted explanation table for the API response.
                                workbench.tables.append(table)
                # Finish customer memory/proposals through ShoppingTracker; staff return only visible English.
                reply = tracker.finish(content) if tracker else plain_reply(content)
                # Successful customer placement/refund requests need factual receipts rather than repeated
                # model confirmation.
                if tracker and not (workbench.action_blocks or workbench.uncertain_change
                        or workbench.completed_response_error or workbench.dispatch_problem):
                    # Select only actual completed placement or refund-request receipts from this turn's
                    # successful evidence.
                    submitted = [change_receipt(name, args, data) for name, args, data in workbench.receipts
                                 if data.get('ok') and (name == 'place_order' or name == 'process_refund'
                                     and args.get('action', 'request') == 'request')]
                    # A real submitted operation overrides a model that redundantly asks to confirm again.
                    if submitted:
                        # A successful placement/request has an authoritative
                        # receipt; model prose must not ask to repeat it.
                        # Show the actual completion receipts once.
                        reply = '\n'.join(submitted)
                        # Other tool failures must not erase a separately confirmed completed action.
                        if workbench.failed:
                            # Choose whether the factual successful operation was placement or a refund
                            # request.
                            receipt_status = ('The order placement above is confirmed.' if any(
                                name == 'place_order' and data.get('ok') for name, _, data in workbench.receipts)
                                else 'The refund request above is confirmed.')
                            # Explain any unrelated missing results while preserving the real operation's
                            # confirmed status.
                            reply += '\n\nA tool failed during this request; some other requested results may be unavailable. ' + receipt_status
                        # A distinct still-pending proposal may legitimately need its own review.
                        review = tracker.confirmation_review()
                        # Append it only when the tracker actually has a review to show.
                        if review:
                            # Show that separate current review after completed receipts.
                            reply += '\n\n' + review
                        # Clear a stale model follow-up question after a confirmed submission.
                        tracker.state['next_question'] = None
                # Return one visible response plus protected invoice references, validated
                # charts/tables/reports and role-appropriate saved context; no internal envelope is rendered
                # directly.
                return {"response": reply, "invoices": list(workbench.downloads),
                        "charts": list(workbench.charts),
                        "tables": sanitize_tables(workbench.tables), "reports": list(workbench.reports),
                        "associated_customer_id": workbench.customer_id,
                        **({'analytics_context': workbench.analytics_context()} if mode == 'admin' else {}),
                        **({'pending_staff_action':staff_proposal} if mode in ('admin','cashier') else {}),
                        **({'shopping_state': deepcopy(tracker.state)} if tracker else {})}
            # Any exception or cancellation invalidates the current transport operation rather than silently
            # retrying the turn.
            except BaseException:
                # Discard failed/cancelled transports without retrying the turn.
                # Cancel child futures/tasks linked to this request.
                run_token.cancel()
                # Discard possibly broken MCP and Azure resources.
                await self._reset()
                # Propagate the original exception for API-level handling.
                raise
            # Always clean up callback references and record final timing/usage.
            finally:
                # Cancel any remaining child operation even on a normal exit.
                run_token.cancel()
                # Log total time, lock wait, tool time/count, chart count and provider token usage without
                # content.
                log.info("billing_turn mode=%s total_seconds=%.3f lock_wait_seconds=%.3f tool_calls=%s tool_seconds=%.3f charts=%s model_calls=%s input_tokens=%s output_tokens=%s",
                         mode, perf_counter() - started, acquired - started,
                         workbench.tool_calls if workbench else 0,
                         workbench.tool_seconds if workbench else 0.0,
                         len(workbench.charts) if workbench else 0,
                         workbench.model_calls if workbench else 0,
                         workbench.input_tokens if workbench else 0,
                         workbench.output_tokens if workbench else 0)
                # Only clear per-workbench references when initialization actually succeeded.
                if workbench is not None:
                    # Detach customer state so the next turn cannot inherit another customer's tracker.
                    workbench.shopping_tracker = None
                    # Detach the request-specific chart callback.
                    workbench.chart_callback = None
                    # Detach the request-specific mutation checkpoint callback.
                    workbench.mutation_callback = None
                    # Reset the checkpoint marker for a future turn.
                    workbench._change_recorded = False
                # Standalone turns clean up immediately; lifespan-managed engines retain only healthy
                # transports.
                if not self._managed or (self._workbench is not None and self._workbench.transport_failed):
                    # Close resources when unmanaged or after a transport failure.
                    await self._reset()

    # Generate an invoice download through the protected tool without an additional model round trip.
    async def invoice(self, invoice_number, customer_id):
        # A lifespan-managed engine can reuse its MCP connection.
        if self._managed:
            # Serialize invoice work with chat calls that share the workbench.
            async with self._lock:
                # Reset resources on invoice dispatch failure.
                try:
                    # Get the live or newly started workbench.
                    workbench = await self._get_workbench()
                    # Bind customer execution scope to the authenticated download requester.
                    workbench.mode, workbench.customer_id = 'customer', customer_id
                    # Use a neutral report contract for this direct invoice operation.
                    workbench.contract = ResponseContract()
                    # Clear read-repair status from previous work.
                    workbench.repair_only = False
                    # Clear any old blocked batch marker.
                    workbench.prepare_batch(None)
                    # Call download_invoice with the exact invoice/customer and decode the structured result.
                    result = decode_result(await workbench.call_tool('download_invoice',
                        {'invoice_number': invoice_number, 'customer_id': customer_id}))
                    # A failed MCP channel should not remain cached.
                    if workbench.transport_failed:
                        # Discard the failed resources.
                        await self._reset()
                    # Return actual invoice readiness/error metadata to the API.
                    return result
                # Include cancellation in invoice cleanup.
                except BaseException:
                    # Reset the failed transport/client pair.
                    await self._reset()
                    # Reraise the actual error.
                    raise
        # Without lifespan management, open a short-lived MCP session and close it after this operation.
        async with McpWorkbench(parameters()) as workbench:
            # Pass the trusted customer execution role/scope explicitly and return the decoded invoice result.
            return decode_result(await workbench.call_tool("download_invoice",
                {"invoice_number": invoice_number, "customer_id": customer_id,
                 "execution_mode": "customer", "execution_customer_id": customer_id}))
