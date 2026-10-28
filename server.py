"""Poker Trainer: play hands against the pokerbot bots in a browser, GTO-Wizard
style - pick who sits at the table, then check/call/raise/fold your way through
each hand and see an end-of-hand summary.

    python server.py              # http://localhost:8100
    python server.py --port 8080

This is a separate project from pokerbot/, but it doesn't duplicate the bot code:
it adds pokerbot/ to sys.path and imports its modules directly (cards, protocol,
ranges, strategy, opponents, bot, gto). A sibling ../pokerbot checkout is used
when it exists; otherwise vendor/pokerbot (the Git submodule) is. See README.md.

Tables are not kept in memory. The session id is a token holding the seat list,
the deal seed, and every action so far. Each request replays that from scratch.
That keeps a hand going across processes, which is what Vercel functions need.
`app` is the WSGI entrypoint Vercel loads; `python server.py` is the local server.
"""
import argparse
import base64
import json
import random
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent


def locate_pokerbot():
    candidates = [HERE.parent / 'pokerbot', HERE / 'vendor' / 'pokerbot']
    for path in candidates:
        if (path / 'protocol.py').is_file():
            return path
    tried = ', '.join(str(p) for p in candidates)
    raise SystemExit(f"can't find the pokerbot project (tried {tried}; see README.md)")


sys.path.insert(0, str(locate_pokerbot()))

from bot import TightProfiler                              # noqa: E402
from opponents import ROSTER                                # noqa: E402

from interactive import Table                                # noqa: E402

HUMAN_NAME = 'YOU'
MIN_SEATS, MAX_SEATS = 2, 8
MAX_HANDS = 250          # each request replays the whole session; stay well under a function timeout
MAX_ACTIONS = 40         # human decisions in a single hand
MAX_TOKEN_CHARS = 200_000

# Every bot a seat can be given: id -> (factory(name, seed) -> instance, description).
BOT_FACTORIES = {'YOSIEF': (lambda name, seed: TightProfiler(name=name),
                             'Exploitative: profiles every opponent and adapts to them')}
for kind, (cls, offset, about) in ROSTER.items():
    BOT_FACTORIES[kind] = (lambda name, seed, cls=cls, offset=offset: cls(name, seed + offset), about)


def bot_list():
    return [{'id': k, 'description': about} for k, (_, about) in sorted(BOT_FACTORIES.items())]


class Session:
    """A Table plus the seat metadata the frontend needs to label seats."""

    def __init__(self, table, seat_info):
        self.table = table
        self.seat_info = seat_info     # name -> {'kind': 'human'|'bot', 'bot': id or None}


def build_session(body):
    seats_in = body.get('seats')
    if not isinstance(seats_in, list):
        raise ValueError('seats must be a list')
    occupied = [s for s in seats_in if s and s.get('kind') != 'empty']
    if not MIN_SEATS <= len(occupied) <= MAX_SEATS:
        raise ValueError(f'seat {len(occupied)} players, but a table needs {MIN_SEATS} to {MAX_SEATS}')
    humans = [s for s in occupied if s.get('kind') == 'human']
    if len(humans) != 1:
        raise ValueError('exactly one seat must be "You"')

    names, bots, seat_info = [], {}, {}
    used = {}
    for s in occupied:
        if s.get('kind') == 'human':
            name = HUMAN_NAME
            seat_info[name] = {'kind': 'human', 'bot': None}
        else:
            kind = s.get('bot')
            if kind not in BOT_FACTORIES:
                raise ValueError(f'unknown bot: {kind!r}')
            used[kind] = used.get(kind, 0) + 1
            name = kind if used[kind] == 1 else f'{kind}-{used[kind]}'
            seat_info[name] = {'kind': 'bot', 'bot': kind}
        names.append(name)

    seed = body.get('seed')
    seed = int(seed) if seed is not None else None
    for name in names:
        if name == HUMAN_NAME:
            continue
        kind = seat_info[name]['bot']
        factory, _ = BOT_FACTORIES[kind]
        bots[name] = factory(name, 0 if seed is None else seed)

    big_blind = int(body.get('big_blind', 100))
    small_blind = int(body.get('small_blind', 50))
    stack = int(body.get('stack', 10000))
    if big_blind <= 0 or small_blind <= 0 or small_blind > big_blind or stack < big_blind:
        raise ValueError('check your blinds and starting stack')
    carry = bool(body.get('carry', True))

    table = Table(names, bots, HUMAN_NAME, big_blind=big_blind, small_blind=small_blind,
                  stack=stack, seed=seed, carry=carry)
    return Session(table, seat_info)


def session_view(sess):
    t = sess.table
    return {
        'seats': [{'name': n, **sess.seat_info[n], 'stack': t.stacks[n]} for n in t.seats],
        'button': t.button, 'big_blind': t.bb, 'small_blind': t.sb, 'hand_id': t.hand_id,
        'human': HUMAN_NAME,
    }


def encode_token(data):
    raw = json.dumps(data, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip('=')


def decode_token(token):
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise ValueError('unknown or expired session')
    pad = '=' * (-len(token) % 4)
    try:
        blob = base64.urlsafe_b64decode(token + pad)
        dec = zlib.decompressobj()
        raw = dec.decompress(blob, 500_000)
        if dec.unconsumed_tail or not dec.eof:
            raise ValueError('unknown or expired session')
        data = json.loads(raw)
    except ValueError:
        raise
    except Exception:
        raise ValueError('unknown or expired session')
    if not isinstance(data, dict):
        raise ValueError('unknown or expired session')
    hands = data.get('hands')
    if not isinstance(data.get('spec'), dict) or not isinstance(hands, list) or not hands:
        raise ValueError('unknown or expired session')
    if any(not isinstance(hand, list) for hand in hands):
        raise ValueError('unknown or expired session')
    return data


def new_token(body):
    seed = body.get('seed')
    seed = int(seed) if seed is not None else random.SystemRandom().randrange(1, 2**31 - 1)
    spec = {
        'seats': body.get('seats'),
        'big_blind': body.get('big_blind', 100),
        'small_blind': body.get('small_blind', 50),
        'stack': body.get('stack', 10000),
        'carry': body.get('carry', True),
        'seed': seed,
    }
    build_session(spec)   # reject a bad table before dealing
    return {'spec': spec, 'hands': [[]]}


def as_action(recorded):
    if not isinstance(recorded, (list, tuple)) or not recorded:
        raise ValueError('bad action')
    kind = recorded[0]
    if kind == 'raise':
        if len(recorded) < 2:
            raise ValueError("missing field: 'to'")
        return ('raise', int(recorded[1]))
    if kind not in ('fold', 'check', 'call'):
        raise ValueError(f'unknown action: {kind!r}')
    return (kind,)


def append_action(token, body):
    kind = body.get('kind')
    if kind == 'raise':
        recorded = ['raise', int(body['to'])]
    elif kind in ('fold', 'check', 'call'):
        recorded = [kind]
    else:
        raise ValueError(f'unknown action: {kind!r}')
    hands = [list(hand) for hand in token['hands']]
    if len(hands[-1]) >= MAX_ACTIONS:
        raise ValueError('too many actions this hand')
    hands[-1].append(recorded)
    return {'spec': token['spec'], 'hands': hands}


def play(token):
    """Rebuild the table and replay every hand. Bots are deterministic given the seed."""
    if len(token['hands']) > MAX_HANDS:
        raise ValueError('this table has gone on long enough; start a new one')
    sess = build_session(token['spec'])
    result = None
    for actions in token['hands']:
        if len(actions) > MAX_ACTIONS:
            raise ValueError('too many actions this hand')
        result = sess.table.new_hand()
        for recorded in actions:
            if result['status'] != 'acting':
                raise ValueError('no action is pending')
            result = sess.table.act(as_action(recorded))
    if result is None:
        raise ValueError('unknown or expired session')
    return sess, result


def response(token, sess, result):
    return {'session': encode_token(token), **session_view(sess), **result}


def parse_body(raw):
    try:
        return json.loads(raw or b'{}')
    except json.JSONDecodeError:
        raise ValueError('malformed JSON body')


def json_response(code, obj):
    return code, 'application/json', json.dumps(obj, separators=(',', ':')).encode()


def dispatch(method, path, query, body_bytes):
    """Return (status, content_type, body) for one request."""
    try:
        if method == 'GET' and path in ('/', '/index.html'):
            return 200, 'text/html; charset=utf-8', (HERE / 'static' / 'index.html').read_bytes()
        if method == 'GET' and path == '/api/bots':
            return json_response(200, {'bots': bot_list(), 'min_seats': MIN_SEATS, 'max_seats': MAX_SEATS})
        if method == 'GET' and path == '/api/state':
            q = {k: v[0] for k, v in parse_qs(query).items()}
            token = decode_token(q.get('session', ''))
            sess, result = play(token)
            return json_response(200, response(token, sess, result))
        if method == 'POST' and path == '/api/table':
            token = new_token(parse_body(body_bytes))
            sess, result = play(token)
            return json_response(200, response(token, sess, result))
        if method == 'POST' and path == '/api/act':
            body = parse_body(body_bytes)
            token = append_action(decode_token(body.get('session', '')), body)
            sess, result = play(token)
            return json_response(200, response(token, sess, result))
        if method == 'POST' and path == '/api/next_hand':
            body = parse_body(body_bytes)
            token = decode_token(body.get('session', ''))
            if len(token['hands']) >= MAX_HANDS:
                raise ValueError('this table has gone on long enough; start a new one')
            token = {'spec': token['spec'], 'hands': [*token['hands'], []]}
            sess, result = play(token)
            return json_response(200, response(token, sess, result))
        return 404, 'text/plain', b'not found'
    except ValueError as e:
        return json_response(400, {'error': str(e)})
    except KeyError as e:
        return json_response(400, {'error': f'missing field: {e}'})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        self._send(*dispatch('GET', url.path, url.query, b''))

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b''
        self._send(*dispatch('POST', url.path, url.query, raw))

    def log_message(self, fmt, *args):
        print(f'{self.address_string()} {fmt % args}')


def app(environ, start_response):
    """WSGI entrypoint used by Vercel."""
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO') or '/'
    query = environ.get('QUERY_STRING', '')
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    body = environ['wsgi.input'].read(length) if length else b''
    code, ctype, payload = dispatch(method, path, query, body)
    reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found'}.get(code, 'Error')
    start_response(f'{code} {reason}', [
        ('Content-Type', ctype),
        ('Content-Length', str(len(payload))),
    ])
    return [payload]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8100)
    args = ap.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Poker Trainer on http://localhost:{args.port}  (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
