"""Poker Trainer: play hands against the pokerbot bots in a browser, GTO-Wizard
style - pick who sits at the table, then check/call/raise/fold your way through
each hand and see an end-of-hand summary.

    python server.py              # http://localhost:8100
    python server.py --port 8080

This is a separate project from pokerbot/, but it doesn't duplicate the bot code:
it adds pokerbot/ to sys.path and imports its modules directly (cards, protocol,
ranges, strategy, opponents, bot, gto), so Yosief, GTO and the practice bots stay
a single source of truth. See README.md for the sibling-folder layout this needs.
"""
import argparse
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
POKERBOT = HERE.parent / 'pokerbot'
if not (POKERBOT / 'protocol.py').exists():
    raise SystemExit(f"can't find the pokerbot project at {POKERBOT} - this app expects to sit "
                      f"next to it (see README.md)")
sys.path.insert(0, str(POKERBOT))

from bot import TightProfiler                              # noqa: E402
from opponents import ROSTER                                # noqa: E402

from interactive import Table                                # noqa: E402

HUMAN_NAME = 'YOU'
MIN_SEATS, MAX_SEATS = 2, 8

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
        self.lock = threading.Lock()
        self.last_result = None        # so a page reload can recover without replaying an action


SESSIONS = {}


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


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        data = path.read_bytes()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            return json.loads(raw or b'{}')
        except json.JSONDecodeError:
            raise ValueError('malformed JSON body')

    def _session(self, sid):
        sess = SESSIONS.get(sid)
        if sess is None:
            raise ValueError('unknown or expired session')
        return sess

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ('/', '/index.html'):
            self._file(HERE / 'static' / 'index.html', 'text/html; charset=utf-8')
        elif url.path == '/api/bots':
            self._json(200, {'bots': bot_list(), 'min_seats': MIN_SEATS, 'max_seats': MAX_SEATS})
        elif url.path == '/api/state':
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                sess = self._session(q.get('session', ''))
                self._json(200, {**session_view(sess), **(sess.last_result or {})})
            except ValueError as e:
                self._json(400, {'error': str(e)})
        else:
            self._send_404()

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path == '/api/table':
                sess = build_session(body)
                sid = uuid.uuid4().hex
                SESSIONS[sid] = sess
                with sess.lock:
                    result = sess.table.new_hand()
                    sess.last_result = result
                self._json(200, {'session': sid, **session_view(sess), **result})
            elif url.path == '/api/next_hand':
                sess = self._session(body.get('session', ''))
                with sess.lock:
                    result = sess.table.new_hand()
                    sess.last_result = result
                self._json(200, {**session_view(sess), **result})
            elif url.path == '/api/act':
                sess = self._session(body.get('session', ''))
                kind = body.get('kind')
                action = (kind, body['to']) if kind == 'raise' else (kind,)
                with sess.lock:
                    result = sess.table.act(action)
                    sess.last_result = result
                self._json(200, {**session_view(sess), **result})
            else:
                self._send_404()
        except ValueError as e:
            self._json(400, {'error': str(e)})
        except KeyError as e:
            self._json(400, {'error': f'missing field: {e}'})

    def _send_404(self):
        body = b'not found'
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f'{self.address_string()} {fmt % args}')


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
