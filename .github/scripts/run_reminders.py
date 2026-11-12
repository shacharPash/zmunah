"""Bounded GitHub Actions reminder pagination. Logs only aggregate counts/cursors.

Vercel Cron is the primary trigger; this backup resumes from the event's saved
progress. A page that still fails after its retries is skipped so later pages
are delivered, and the run then exits 1 so the failure stays visible.
"""
import json
import os
import re
import uuid
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_PAGES = 1000
MAX_SECONDS = 5400
ATTEMPTS = 3
BUSY_WAIT_SECONDS = 30
MAX_BUSY_WAITS = 14
# These outcomes cannot change by retrying the same request.
TERMINAL_REASONS = ('missed_window',)
SUMMARY_KEYS = ('status', 'reason', 'attempted', 'sent', 'failed', 'skipped', 'undeliverable', 'pages', 'cursor')
CONTINUATION = re.compile(r'(?:0|[0-9a-f]{32})')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('unexpected redirect')


def _read_json(stream):
    body = stream.read(65537)
    if len(body) > 65536:
        raise RuntimeError('response too large')
    result = json.loads(body)
    if not isinstance(result, dict):
        raise RuntimeError('unexpected response')
    return result


def fetch(opener, url, secret):
    """Return the endpoint's JSON summary, including the body of a 503 answer."""
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + secret})
    try:
        with opener.open(request, timeout=290) as response:
            return _read_json(response)
    except urllib.error.HTTPError as error:
        try:
            if error.code != 503:
                raise RuntimeError('unexpected status') from None
            return _read_json(error)
        finally:
            error.close()


def healthy(result):
    return (result is not None and not result.get('failed', 0)
            and result.get('status') in ('completed', 'skipped')
            and result.get('reason') not in TERMINAL_REASONS)


def main():
    base = os.environ['VERCEL_URL'].rstrip('/')
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('invalid deployment URL')
    endpoint = os.environ['REMINDER_ENDPOINT']
    if endpoint not in ('shabbat_reminder', 'omer_reminder', 'omer_morning_reminder'):
        raise ValueError('invalid endpoint')
    secret = os.environ['CRON_SECRET']
    if not secret:
        raise ValueError('missing secret')
    opener = urllib.request.build_opener(NoRedirect())
    cursor = os.environ.get('RESUME_CURSOR') or '0'
    restart = os.environ.get('RESTART_TRAVERSAL', '').lower() == 'true'
    if restart:
        if cursor != '0':
            raise ValueError('choose resume or restart, not both')
        # Generated once; all retries keep the same fresh traversal identity.
        cursor = 'r.' + uuid.uuid4().hex
    if not re.fullmatch(r'(?:0|[0-9a-f]{32}|r\.[0-9a-f]{32})', cursor):
        raise ValueError('invalid resume cursor')
    started = time.monotonic()
    failed_pages = 0
    for page in range(MAX_PAGES):
        url = base + '/api/' + endpoint + '?' + urllib.parse.urlencode({'cursor': cursor})
        attempt = busy_waits = 0
        while True:
            if time.monotonic() - started > MAX_SECONDS:
                raise RuntimeError('time cap reached')
            try:
                result = fetch(opener, url, secret)
            except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError):
                result = None
            if healthy(result):
                break
            if result is not None and result.get('reason') in TERMINAL_REASONS:
                print(json.dumps({k: result.get(k) for k in SUMMARY_KEYS}))
                return 1
            if result is not None and result.get('status') == 'busy' and busy_waits < MAX_BUSY_WAITS:
                # Another run of the same event holds the lock; it ends within one function lifetime.
                busy_waits += 1
                time.sleep(BUSY_WAIT_SECONDS)
                continue
            attempt += 1
            if attempt == ATTEMPTS:
                break
            time.sleep(5 * attempt)
        if not healthy(result):
            continuation = result.get('cursor') if result is not None else None
            if not isinstance(continuation, str) or not CONTINUATION.fullmatch(continuation) or continuation == cursor:
                print(json.dumps({'status': 'failed', 'page': page, 'resume_cursor': cursor}))
                return 1
            failed_pages += 1
            print(json.dumps({'status': 'page_failed', 'page': page, 'resume_cursor': cursor}))
        print(json.dumps({k: result.get(k) for k in SUMMARY_KEYS}))
        cursor = result['cursor']
        if not isinstance(cursor, str) or not CONTINUATION.fullmatch(cursor):
            raise RuntimeError('invalid continuation')
        if cursor == '0':
            return 1 if failed_pages else 0
    print(json.dumps({'status': 'batch_cap_reached', 'resume_cursor': cursor}))
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('{"status":"failed","reason":"scheduler_request_failed"}')
        sys.exit(1)
