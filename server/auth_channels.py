"""Deliver 2FA nudges and intercept replies before conversation persistence."""
import asyncio
import re

import channels
import settings

source_for = lambda aid: ('gui', None)
_direct = {}  # Observed, allowlisted private conversations; no message text.
SUPPORTED = ('imessage', 'telegram', 'whatsapp')


def _is_direct(channel, chat_id, sender):
    if channel == 'imessage':
        return ';-;' in chat_id and chat_id.split(';-;', 1)[1].lower() == sender.lower()
    return channel in ('telegram', 'whatsapp') and chat_id == sender


def observe(channel, chat_id, sender):
    # Called only after the channel adapter's sender allowlist check.
    if _is_direct(channel, chat_id, sender):
        allowed = channels.channel_settings().get(channel, {}).get('allowed_senders') or []
        _direct[channel, chat_id] = {'channel': channel, 'chat_id': chat_id, 'sender': sender,
                                     'allowlist': sorted(map(str, allowed))}


def destination(aid):
    mode = (settings.load().get('browser') or {}).get('auth_channel', 'auto')
    if mode == 'app':
        return None
    source, chat_id = source_for(aid)
    choices = [mode] if mode in SUPPORTED else list(dict.fromkeys([source, *SUPPORTED]))
    conf = channels.channel_settings()
    for name in choices:
        channel = channels.manager.get(name)
        if name not in SUPPORTED or not channel or not channel.connected or not conf[name].get('enabled'):
            continue
        # Prefer the actual direct conversation for the task.
        allowed = [str(value) for value in conf[name].get('allowed_senders') or []]
        if name == source and (name, chat_id) in _direct:
            route = _direct[name, chat_id]
            if route['allowlist'] == sorted(allowed):
                return route
        # A single configured phone destination is unambiguous even when the
        # same owner's email address is also allowed for iMessage.
        phones = [value for value in allowed if re.fullmatch(r'\+?\d{7,16}', value)]
        if name == 'imessage' and len(phones) == 1:
            sender = phones[0]
            return {'channel': name, 'chat_id': f'any;-;{sender}', 'sender': sender}
        if len(allowed) != 1:
            continue
        sender = allowed[0]
        if name == 'imessage':
            return {'channel': name, 'chat_id': f'any;-;{sender}', 'sender': sender}
        if re.fullmatch(r'\d+', sender):
            return {'channel': name, 'chat_id': sender, 'sender': sender}
        # Telegram @usernames cannot address a private chat until observed.
        observed = [route for (channel_name, _), route in _direct.items()
                    if channel_name == name and route['allowlist'] == sorted(allowed)]
        if len(observed) == 1:
            return observed[0]
    return None


async def nudge(record):
    route = destination(record['public']['agent'])
    if not route:
        record['public']['delivery'] = 'app'
        return
    record['route'] = route  # Bind replies before sending, including a fast reply.
    public = record['public']
    import browser_auth
    for other in browser_auth._requests.values():
        other_route = other.get('route', {})
        if (other is not record and not other['future'].done() and other_route.get('channel') == route['channel']
                and other_route.get('sender') == route['sender']):
            public['require_id'] = other['public']['require_id'] = True
    public['delivery'] = route['channel']
    prompt = (f"Mission Control needs your 2FA code to sign in to {public['origin']}. "
              f"Reply with just the code. If several logins are waiting, reply "
              f"2fa {public['id'][:8]} CODE. This request expires in 5 minutes.")
    try:
        sent = await asyncio.wait_for(channels.manager.send(route['channel'], route['chat_id'], prompt), 15)
        public['delivery_status'] = 'sent' if sent else 'failed'
    except Exception:
        public['delivery_status'] = 'unconfirmed'


def _matches(route, channel, chat_id, sender):
    if route.get('channel') != channel or route.get('sender', '').lower() != sender.lower():
        return False
    if channel == 'imessage':
        return _is_direct(channel, chat_id, sender)
    return route.get('chat_id') == chat_id and _is_direct(channel, chat_id, sender)


async def consume(channel: str, chat_id: str, sender: str, text: str) -> bool:
    """Return True for auth replies, including expired/ambiguous codes.

    Never fall through into an LLM or database after recognizing a code.
    Callers have already authenticated the transport and allowlisted sender.
    """
    if channel not in SUPPORTED:
        return False
    raw = text.strip()
    tagged = re.fullmatch(r'(?:2fa|otp)\s+(?:([0-9a-fA-F]{8})\s+)?(.+)', raw, re.I)
    bare = re.fullmatch(r'[\d -]{4,24}', raw) and 4 <= len(re.sub(r'\D', '', raw)) <= 12
    # Some sites issue alphanumeric verification codes. Keep those out of
    # model history too; ordinary words/commands still use the normal channel.
    bare = bare or (re.fullmatch(r'[A-Za-z0-9-]{6,20}', raw) and any(char.isdigit() for char in raw))
    if not tagged and not bare:
        return False
    # Only private messages can fulfill a challenge, even from an allowed user.
    if not _is_direct(channel, chat_id, sender):
        return True
    import browser_auth
    rid = tagged[1].lower() if tagged and tagged[1] else None
    value = tagged[2].strip() if tagged else re.sub(r'[ -]', '', raw)
    matches = [record for record in browser_auth._requests.values()
               if record['public']['kind'] == 'code' and not record['future'].done()
               and _matches(record.get('route', {}), channel, chat_id, sender)
               and (rid is None or record['public']['id'].startswith(rid))]
    if len(matches) != 1 or (rid is None and matches[0]['public'].get('require_id')):
        reply = ('Several sign-ins are waiting. Reply with 2fa REQUEST CODE, using the request ID in its nudge.'
                 if matches else 'No matching 2FA request is waiting. Ask the agent to request a fresh code.')
    else:
        record = matches[0]
        try:
            await browser_auth.answer(record['public']['id'], {'value': value, 'cancel': value.lower() == 'cancel'})
            reply = 'Login request cancelled.' if value.lower() == 'cancel' else 'Code received privately. Continuing the website sign-in.'
        except (browser_auth.AuthError, ValueError):
            reply = 'That login request expired or its page changed. Ask the agent to request a fresh code.'
    try:
        await asyncio.wait_for(channels.manager.send(channel, chat_id, reply), 15)
    except Exception:
        pass
    return True
