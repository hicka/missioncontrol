"""Private login handoffs. Secrets move from local UI/vault directly to fields.

Only request metadata is observable. Answers stay in memory, are single-use,
and are discarded when the waiting tool finishes, expires, or is cancelled.
"""
import asyncio
import time
import uuid

import credentials
import secret_mask

WAIT_SECONDS = 300
emit = None
_requests = {}


class AuthError(ValueError):
    pass


async def _changed():
    if emit:
        await emit({'type': 'browser_auth_updated'})


def pending() -> list:
    return [dict(record['public']) for record in _requests.values() if not record['future'].done()]


def cancel_page(page=None, navigation=False):
    for record in list(_requests.values()):
        if page is None or record['page'] is page:
            if navigation and record['public']['kind'] == 'manual':
                continue
            if not record['future'].done():
                record['future'].set_exception(AuthError('The browser page closed or changed; request sign-in again if needed.'))


async def answer(rid: str, values: dict):
    record = _requests.get(rid)
    if not record or record['future'].done() or record['public']['expires'] <= time.time():
        raise AuthError('This login request has expired or was already answered.')
    public = record['public']
    if values.get('cancel'):
        record['future'].set_exception(AuthError('The owner cancelled this login request.'))
        await _changed()
        return
    if record['page'].is_closed() or (public['kind'] != 'manual' and credentials.origin(record['page'].url) != public['origin']):
        raise AuthError('The website changed. Cancel this request and ask the agent to read the current page.')
    if public['kind'] == 'login':
        if values.get('credential_id'):
            if values['credential_id'] not in {row['id'] for row in public.get('accounts', [])}:
                raise AuthError('Choose a saved login for this website.')
        elif (public['needs_username'] and not values.get('username')) or (public['needs_password'] and not values.get('password')):
            raise AuthError('Enter the required login details.')
        elif values.get('remember') and (not values.get('username') or not values.get('password')):
            raise AuthError('Saving a login requires both a username and password. Uncheck Save login for a one-time answer.')
    elif public['kind'] == 'manual':
        if not values.get('confirmed'):
            raise AuthError('Complete the website step, then choose Continue.')
    elif not values.get('value'):
        raise AuthError('Enter the requested value.')
    record['future'].set_result(dict(values))
    await _changed()


async def _ask(aid: str, page, kind: str, **details) -> dict:
    rid = uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    public = {'id': rid, 'agent': aid, 'origin': credentials.origin(page.url),
              'kind': kind, 'expires': time.time() + WAIT_SECONDS, **details}
    _requests[rid] = {'future': future, 'public': public, 'page': page}
    try:
        await _changed()
        if kind == 'code':
            import auth_channels
            await auth_channels.nudge(_requests[rid])
            await _changed()
        return await asyncio.wait_for(future, max(0.01, public['expires'] - time.time()))
    except asyncio.TimeoutError:
        raise AuthError('The secure login prompt expired. Ask again when the owner is ready.') from None
    finally:
        _requests.pop(rid, None)
        await _changed()


FIELD_JS = r"""el => ({
  connected: el.isConnected, tag: el.tagName, type: el.type || '',
  autocomplete: el.autocomplete || '',
  identity: [el.name, el.id, el.placeholder, el.getAttribute('aria-label')].filter(Boolean).join(' '),
  url: el.ownerDocument.location.href,
  action: el.form ? el.form.action : '',
  disabled: el.disabled || el.readOnly,
  visible: el.getBoundingClientRect().width > 1 && el.getBoundingClientRect().height > 1
})"""


async def _field(page, ref: int, site: str, role: str):
    if not isinstance(ref, int) or isinstance(ref, bool) or ref < 1:
        raise AuthError('Read the page and provide a current input field reference.')
    handle = await page.locator(f'[data-oref="{ref}"]').element_handle(timeout=5000)
    if handle is None:
        raise AuthError('The input field changed. Read the page again.')
    await _check(handle, site, role)
    return handle


async def _check(handle, site, role):
    import re
    field = await handle.evaluate(FIELD_JS)
    if (not field['connected'] or not field['visible'] or field['disabled'] or field['tag'] != 'INPUT'
            or field['type'] not in ('text', 'password', 'email', 'tel', 'number', 'search', 'url')):
        raise AuthError('Private input needs a visible, editable login field on the current page.')
    if credentials.origin(field['url']) != site or (field['action'] and credentials.origin(field['action']) != site):
        raise AuthError('The login form points to a different website. Saved credentials were not filled.')
    identity = field['identity'].lower()
    autocomplete = field['autocomplete'].lower()
    if ('new-password' in autocomplete or re.search(r'new.?pass|confirm.?pass|repeat.?pass', identity)
            or 'cc-' in autocomplete or re.search(r'card.?number|\bcvv\b|\bcvc\b', identity)):
        raise AuthError('Password changes and payment fields need direct owner interaction.')
    if role == 'password' and field['type'] != 'password' and 'current-password' not in autocomplete:
        raise AuthError('Use a password field reference for the saved password.')
    if role == 'username' and field['type'] == 'password':
        raise AuthError('Use a username or email field reference for the username.')


async def _fill(handle, site, role, value):
    # Keep the captured element, not a selector that could now target another
    # document. Recheck immediately after an owner prompt or vault access.
    await _check(handle, site, role)
    await handle.evaluate("el => el.setAttribute('data-mc-private', '1')")
    await handle.fill(value, timeout=5000)


async def login(aid: str, page, args: dict) -> str:
    site = credentials.origin(page.url)
    handles = {key: await _field(page, args[key + '_ref'], site, key)
               for key in ('username', 'password') if args.get(key + '_ref') is not None}
    if not handles:
        raise AuthError('Read the sign-in page, then pass username_ref and/or password_ref to login.')
    accounts = await asyncio.to_thread(credentials.list_entries, site)
    cid = args.get('credential_id')
    if not cid and len(accounts) == 1:
        cid = accounts[0]['id']
    values = None
    try:
        if cid and not args.get('ask_owner'):
            values = await asyncio.to_thread(credentials.get, cid, site)
        else:
            response = await _ask(aid, page, 'login', accounts=accounts,
                                  needs_username='username' in handles, needs_password='password' in handles,
                                  message='Sign in to continue the website task.')
            if response.get('credential_id'):
                values = await asyncio.to_thread(credentials.get, response['credential_id'], site)
            else:
                values = {'username': response.get('username', ''), 'password': response.get('password', '')}
                # Validate the captured form again before saving or filling.
                for role, handle in handles.items():
                    await _check(handle, site, role)
                if response.get('remember'):
                    label = response.get('label') or next((row['label'] for row in accounts if row['id'] == cid), '')
                    await asyncio.to_thread(credentials.save, site, values['username'], values['password'], label, cid)
            response.clear()
        for role, handle in handles.items():
            secret_mask.protect(values[role])
            await _fill(handle, site, role, values[role])
        return 'Protected sign-in fields filled. Read the page, submit its sign-in button, and verify the result. Request secure input if the site asks for 2FA.'
    finally:
        if values:
            values.clear()


async def request_input(aid: str, page, args: dict) -> str:
    site = credentials.origin(page.url)
    kind = args.get('kind', 'code')
    if kind not in ('code', 'secret', 'text', 'manual'):
        raise AuthError('Request a code, secret, text, or manual login step.')
    refs = args.get('refs') or ([args['ref']] if args.get('ref') is not None else [])
    if kind != 'manual' and (not refs or len(refs) > 12 or len(set(refs)) != len(refs)):
        raise AuthError('Provide one input ref, or unique refs in order for a split verification code.')
    handles = [await _field(page, ref, site, 'input') for ref in refs] if kind != 'manual' else []
    response = await _ask(aid, page, kind, message=str(args.get('prompt') or {
        'code': 'Enter the verification code requested by this website.',
        'secret': 'Enter the additional private login detail requested by this website.',
        'text': 'Enter the additional login detail requested by this website.',
        'manual': 'Complete the sign-in step in the browser, then continue.'}[kind])[:240])
    try:
        if kind == 'manual':
            return 'The owner completed the browser step. Read the current page and verify sign-in before continuing.'
        value = response['value']
        secret_mask.protect(value)
        if len(handles) > 1:
            value = ''.join(value.split())
            secret_mask.protect(value)
            if len(value) != len(handles):
                raise AuthError('The verification code length does not match the input fields. Request a new code.')
            for handle, char in zip(handles, value):
                await _fill(handle, site, 'input', char)
        else:
            await _fill(handles[0], site, 'input', value)
        return 'The private response was entered directly into the website. Read the current page, submit if needed, and verify the result.'
    finally:
        response.clear()
