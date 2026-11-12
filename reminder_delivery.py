"""Shared bounded, authenticated reminder execution with owned delivery claims.

Vercel Cron calls (marked by the x-vercel-cron-schedule header) walk saved pages
until the event is done or the time budget ends. Other callers get one page per
request. Both resume from the event's saved progress when they start at cursor 0.
Progress stops at the first page with a failed recipient, so the next call that
starts at cursor 0 retries exactly that page.
"""
import json
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from bot_auth import authenticate_cron, send_json
from omer_utils import ISRAEL_TZ, omer_event_context
from redis_client import (acquire_claim, release_claim, complete_claim, claim_completed, update_user_prefs,
                          get_user_prefs, get_pending_deletion, reminder_user_batch, was_omer_counted,
                          get_reminder_progress, set_reminder_progress, touch_keepalive, EVENT_TTL)
from telegram_bot import TelegramDeliveryError, user_processing

# No new recipient starts after this many seconds; the function limit is 300.
DELIVERY_SECONDS = 240
# Israel local hour after which a Shabbat or holiday eve reminder is too close to candle lighting.
SHABBAT_LAST_HOUR = 12
MORNING_FIRST_HOUR = 6
MORNING_LAST_HOUR = 12
MAX_RETRY_AFTER = 30
# Outlives one invocation, so an interrupted run cannot overlap the next one.
RUN_LOCK_TTL = 360
RUN_LOCK_PREFIX = 'zmunah:reminder_run:'
CRON_HEADER = 'x-vercel-cron-schedule'


def _delivery_window(kind, context, eligible):
    """'open', 'closed' (nothing due yet or today), or 'missed' (the event's window has passed)."""
    now = context['now']
    if kind == 'shabbat':
        if not eligible(now):
            return 'closed'
        return 'open' if now.hour < SHABBAT_LAST_HOUR else 'missed'
    if context['day'] is None:
        return 'closed'
    if kind == 'evening':
        return 'open' if context['evening_eligible'] else 'missed'
    if now.hour < MORNING_FIRST_HOUR:
        return 'closed'
    return 'open' if now.hour < MORNING_LAST_HOUR else 'missed'


def _window_end(kind, now):
    """Israel time at which an open window closes: local midnight for the evening count, else noon."""
    local = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    if kind == 'evening':
        end = local.replace(hour=0) + timedelta(days=1)
    else:
        end = local.replace(hour=SHABBAT_LAST_HOUR if kind == 'shabbat' else MORNING_LAST_HOUR)
    return ISRAEL_TZ.localize(end)


def _send_with_one_rate_limit_retry(send, user_id, context, deadline):
    try:
        return send(user_id, context)
    except TelegramDeliveryError as error:
        if error.error_code != 429 or error.retry_after is None:
            raise
        wait = min(max(error.retry_after, 1), MAX_RETRY_AFTER)
        if time.monotonic() + wait > deadline:
            raise
        time.sleep(wait)
        return send(user_id, context)


def _deliver(user_id, kind, field, context, send, counts, deadline):
    key = f"zmunah:delivery:{user_id}:{kind}:{context['event_id']}"
    if claim_completed(key):
        counts['skipped'] += 1
        return
    token = None
    try:
        with user_processing(user_id):
            # Re-read after acquiring the lock, including for test mode.
            if get_pending_deletion(user_id) is not None or get_user_prefs(user_id).get(field) is not True:
                counts['skipped'] += 1
                return
            if kind == 'morning' and (was_omer_counted(user_id, context['event_id']) or was_omer_counted(user_id, context['day'])):
                counts['skipped'] += 1
                return
            token = acquire_claim(key)
            if token is None:
                if not claim_completed(key):
                    raise RuntimeError('delivery busy')
                counts['skipped'] += 1
                return
            counts['attempted'] += 1
            try:
                delivered = _send_with_one_rate_limit_retry(send, user_id, context, deadline)
            except TelegramDeliveryError as error:
                if not error.recipient_blocked:
                    raise
                update_user_prefs(user_id, {field: False})
                if not complete_claim(key, token, EVENT_TTL):
                    raise RuntimeError('delivery lease expired')
                counts['undeliverable'] += 1
                return
            if not delivered:
                raise RuntimeError('delivery failed')
            if not complete_claim(key, token, EVENT_TTL):
                raise RuntimeError('delivery lease expired')
            counts['sent'] += 1
    except Exception:
        counts['failed'] += 1
        if token:
            release_claim(key, token)


def _deliver_page(users, kind, field, context, send, counts, deadline):
    """Return False when the time budget ended before every recipient was handled."""
    for user_id in users:
        if time.monotonic() >= deadline:
            return False
        _deliver(user_id, kind, field, context, send, counts, deadline)
    return True


def _traverse(field, kind, context, send, counts, requested, loop, deadline):
    """Deliver saved pages; advance the event's progress only past fully delivered pages."""
    scope = f"{kind}:{context['event_id']}"
    raw, progress = get_reminder_progress(scope)
    cursor = requested
    if requested == '0':
        if progress['cursor'] == 'done':
            return {'status': 'completed', 'reason': 'already_completed', 'cursor': '0'}
        cursor = progress['cursor']
    on_chain = cursor == progress['cursor']
    while True:
        users, continuation = reminder_user_batch(field, cursor, scope=scope)
        failed_before = counts['failed']
        finished = _deliver_page(users, kind, field, context, send, counts, deadline)
        counts['pages'] += 1
        if not finished:
            return {'status': 'in_progress', 'cursor': requested}
        if on_chain and counts['failed'] > failed_before:
            # Later pages are still delivered, but progress waits here for a retry.
            on_chain = False
        elif on_chain:
            replaced, raw = set_reminder_progress(scope, raw, {'cursor': continuation if continuation != '0' else 'done'})
            if not replaced:
                raise RuntimeError('reminder progress changed')
        if continuation == '0':
            return {'status': 'completed', 'cursor': '0'}
        if not loop or time.monotonic() >= deadline:
            return {'status': 'in_progress' if loop else 'completed', 'cursor': continuation}
        cursor = continuation


def run_reminders(handler, secret, kind, send, eligible=None):
    if not authenticate_cron(handler, secret):
        return
    deadline = time.monotonic() + DELIVERY_SECONDS
    counts = {'attempted': 0, 'sent': 0, 'failed': 0, 'skipped': 0, 'undeliverable': 0, 'pages': 0}
    try:
        query = parse_qs(urlparse(handler.path).query)
        # A manually targeted request uses the same auth, consent and time guards.
        target = query.get('test_user_id', [None])[0]
        if target is not None and (not target.isdigit() or int(target) <= 0):
            send_json(handler, 400, {'error': 'invalid request'})
            return
        cursor = query.get('cursor', ['0'])[0]
        # Daily calls also keep the database active on days with nothing to send.
        touch_keepalive()
        loop = target is None and handler.headers.get(CRON_HEADER) is not None
        now = datetime.now(ISRAEL_TZ)
        context = omer_event_context(now) if kind != 'shabbat' else {'now': now, 'event_id': now.date().isoformat()}
        window = _delivery_window(kind, context, eligible)
        if window == 'open':
            # No recipient starts after the window closes, even when the call began inside it.
            deadline = min(deadline, time.monotonic() + (_window_end(kind, now) - now).total_seconds())
        field = 'shabbat_reminder_enabled' if kind == 'shabbat' else 'reminder_enabled'
        scope = f"{kind}:{context['event_id']}"
        if window != 'open' and (target or window == 'closed'):
            result = {'status': 'skipped', 'reason': 'outside_delivery_window', 'cursor': '0'}
        elif window == 'missed':
            _, progress = get_reminder_progress(scope)
            if progress['cursor'] == 'done':
                result = {'status': 'skipped', 'reason': 'outside_delivery_window', 'cursor': '0'}
            else:
                result = {'status': 'missed', 'reason': 'missed_window', 'cursor': '0'}
        elif target:
            _deliver_page([target], kind, field, context, send, counts, deadline)
            result = {'status': 'completed', 'cursor': '0'}
        else:
            lock = acquire_claim(RUN_LOCK_PREFIX + scope, RUN_LOCK_TTL)
            if lock is None:
                result = {'status': 'busy', 'cursor': cursor}
            else:
                try:
                    result = _traverse(field, kind, context, send, counts, cursor, loop, deadline)
                finally:
                    release_claim(RUN_LOCK_PREFIX + scope, lock)
        if counts['failed'] and result['status'] in ('completed', 'in_progress'):
            result['status'] = 'partial_failure'
        response = {**result, **counts}
        print(json.dumps({'event': 'reminder_summary', 'kind': kind, 'mode': 'cron' if loop else 'page', **response}))
        healthy = result['status'] in ('completed', 'in_progress', 'skipped')
        send_json(handler, 200 if healthy else 503, response)
    except Exception:
        print(json.dumps({'event': 'reminder_summary', 'kind': kind, 'status': 'failed', **counts}))
        send_json(handler, 503, {'error': 'unavailable', **counts})
