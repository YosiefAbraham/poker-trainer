"""A human-in-the-loop no-limit hold'em hand, adapted from the pokerbot project's
engine.py so a browser player can take one seat at the table alongside its bots.

The difference from engine.py's _Hand: betting_round() is a generator. For every
other seat it calls bot.act(state) exactly as the original dealer does; at the
human seat it instead `yield`s the GameState and waits to be sent an action tuple
(fold/check/call/raise) from the caller. Nothing here changes pokerbot/ itself -
this module only imports from it (see sys.path setup in server.py).
"""
import random
from collections import defaultdict

from cards import evaluate, full_deck
from protocol import STREETS, Event, GameState
from ranges import position_name
from strategy import classify


def award(contrib, alive, values):
    """Split the pot into side pots, same rule as pokerbot/engine.py."""
    payouts = defaultdict(int)
    prev = 0
    for level in sorted({c for c in contrib.values() if c > 0}):
        in_layer = [n for n, c in contrib.items() if c >= level]
        amount = (level - prev) * len(in_layer)
        eligible = [n for n in alive if contrib[n] >= level] or alive
        best = max(values[n] for n in eligible)
        winners = [n for n in eligible if values[n] == best]
        share, rem = divmod(amount, len(winners))
        for w in winners:
            payouts[w] += share
        payouts[winners[0]] += rem
        prev = level
    return payouts


def event_json(e):
    return {'player': e.player, 'street': e.street, 'kind': e.kind, 'amount': e.amount,
            'to': e.to, 'pot_before': e.pot_before, 'to_call_before': e.to_call_before}


def state_json(s):
    """A GameState, trimmed to what the browser needs to render the human's turn."""
    return {
        'hole': list(s.hole), 'board': list(s.board), 'street': s.street, 'pot': s.pot,
        'to_call': s.to_call, 'min_raise_to': s.min_raise_to, 'max_raise_to': s.max_raise_to,
        'stack': s.stack, 'committed': s.committed, 'big_blind': s.big_blind,
        'position': s.position, 'positions': dict(s.positions), 'opponents': list(s.opponents),
        'history': [event_json(e) for e in s.history], 'stacks': dict(s.stacks),
        'bets': dict(s.bets),
    }


class Table:
    """One session: fixed seating order, running stacks, and the hand in progress.

    seats: ordered list of names. bots: name -> bot instance for every seat except
    human_name. Stacks carry over hand to hand (with a rebuy) when carry=True, like
    engine.Table(carry=True).
    """

    def __init__(self, seats, bots, human_name, big_blind=100, small_blind=50,
                 stack=10000, seed=None, carry=True):
        self.seats = list(seats)
        self.bots = bots
        self.human_name = human_name
        self.bb, self.sb, self.stack = big_blind, small_blind, stack
        self.carry = carry
        self.stacks = {n: stack for n in self.seats}
        self.rebuys = defaultdict(int)
        self.rng = random.Random(seed)
        self.button = 0
        self.hand_id = 0
        self.hand = None
        self.gen = None
        self.waiting = False   # True once the generator is paused on a human decision

    def _call(self, name, method, *args):
        bot = self.bots.get(name)
        if bot is None:
            return None
        try:
            return getattr(bot, method)(*args)
        except Exception:
            return None

    def new_hand(self):
        """Deal the next hand and run it up to the human's first decision (or the end,
        if everyone else folds first). Returns the same shape as act()."""
        for n in self.seats:
            if self.stacks.get(n, 0) <= 0:
                self.rebuys[n] += 1
                self.stacks[n] = self.stack
        self.hand_id += 1
        self.hand = _Hand(self, self.hand_id, self.button)
        self.gen = self.hand.run()
        return self._advance(None)

    def act(self, action):
        if self.gen is None or not self.waiting:
            raise ValueError('no action is pending')
        return self._advance(action)

    def _advance(self, action):
        try:
            _, state = self.gen.send(action) if action is not None else next(self.gen)
            self.waiting = True
            return {'status': 'acting', 'state': state_json(state)}
        except StopIteration as e:
            self.waiting = False
            self.button = (self.button + 1) % len(self.seats)
            return {'status': 'hand_end', 'summary': e.value}


class _Hand:
    def __init__(self, table, hand_id, button):
        self.t = table
        self.names = list(table.seats)
        self.hand_id, self.button = hand_id, button
        self.deck = full_deck()
        table.rng.shuffle(self.deck)
        self.holes = {n: [self.deck.pop(), self.deck.pop()] for n in self.names}
        k = len(self.names)
        self.positions = {self.names[(button + off) % k]: position_name(off, k) for off in range(k)}
        self.start = {n: table.stacks[n] for n in self.names}
        self.stacks = dict(self.start)
        self.contrib = {n: 0 for n in self.names}
        self.commit = {n: 0 for n in self.names}
        self.folded = set()
        self.board = []
        self.history = []

    def pot(self):
        return sum(self.contrib.values())

    def alive(self):
        return [n for n in self.names if n not in self.folded]

    def can_act(self, n):
        return n not in self.folded and self.stacks[n] > 0

    def put(self, n, amount):
        amount = max(0, min(amount, self.stacks[n]))
        self.stacks[n] -= amount
        self.contrib[n] += amount
        self.commit[n] += amount
        return amount

    def emit(self, ev):
        self.history.append(ev)
        for n in self.names:
            if n != self.t.human_name:
                self.t._call(n, 'observe', ev)

    def run(self):
        names, k = self.names, len(self.names)
        for n in names:
            if n != self.t.human_name:
                self.t._call(n, 'new_hand', self.hand_id, dict(self.positions), list(self.holes[n]))
        sb_i, bb_i = ((self.button, (self.button + 1) % k) if k == 2
                      else ((self.button + 1) % k, (self.button + 2) % k))
        for idx, blind in ((sb_i, self.t.sb), (bb_i, self.t.bb)):
            n = names[idx]
            before = self.pot()
            amt = self.put(n, blind)
            self.emit(Event(n, 'preflop', 'blind', amt, self.commit[n], before, 0))

        for street in STREETS:
            if street != 'preflop':
                self.board += [self.deck.pop() for _ in range(3 if street == 'flop' else 1)]
                self.commit = {n: 0 for n in names}
            first = (bb_i + 1) % k if street == 'preflop' else (self.button + 1) % k
            yield from self.betting_round(street, first)
            if len(self.alive()) == 1:
                break

        alive = self.alive()
        if len(alive) == 1:
            payouts, shown = {alive[0]: self.pot()}, {}
        else:
            order = [names[(self.button + 1 + i) % k] for i in range(k)]
            alive_ordered = [n for n in order if n in alive]
            values = {n: evaluate(self.holes[n] + self.board) for n in alive}
            payouts = award(self.contrib, alive_ordered, values)
            shown = {n: list(self.holes[n]) for n in alive}
        nets = {n: payouts.get(n, 0) - self.contrib[n] for n in names}
        if self.t.carry:
            for n in names:
                self.t.stacks[n] += nets[n]
        for n in names:
            if n != self.t.human_name:
                self.t._call(n, 'end_hand', dict(nets), dict(shown), list(self.board))
        return self.summary(nets, shown)

    def betting_round(self, street, first):
        names, k, t = self.names, len(self.names), self.t
        current = max(self.commit.values())
        last_raise = t.bb
        needs = {n for n in names if self.can_act(n)}
        i = first
        while needs and len(self.alive()) > 1:
            n = names[i % k]
            i += 1
            if n not in needs:
                continue
            needs.discard(n)
            to_call = current - self.commit[n]
            others = any(self.can_act(o) for o in self.alive() if o != n)
            if to_call <= 0 and not others:
                continue
            max_to = self.commit[n] + self.stacks[n]
            state = GameState(
                me=n, hole=list(self.holes[n]), board=list(self.board), street=street,
                pot=self.pot(), to_call=min(to_call, self.stacks[n]),
                min_raise_to=min(current + last_raise, max_to), max_raise_to=max_to,
                stack=self.stacks[n], committed=self.commit[n], big_blind=t.bb,
                position=self.positions[n], positions=dict(self.positions),
                opponents=[o for o in self.alive() if o != n],
                history=list(self.history), hand_id=self.hand_id,
                stacks=dict(self.stacks), bets=dict(self.commit))
            if n == t.human_name:
                action = yield ('act', state)
            else:
                action = t._call(n, 'act', state)
            kind, to = self.normalize(action, state, current)

            before = self.pot()
            if kind == 'fold':
                self.folded.add(n)
                self.emit(Event(n, street, 'fold', 0, self.commit[n], before, to_call))
            elif kind == 'check':
                self.emit(Event(n, street, 'check', 0, self.commit[n], before, 0))
            elif kind == 'call':
                amt = self.put(n, to_call)
                self.emit(Event(n, street, 'call', amt, self.commit[n], before, to_call))
            else:
                amt = self.put(n, to - self.commit[n])
                level = self.commit[n]
                if level - current >= last_raise:
                    last_raise = level - current
                current = level
                needs = {o for o in names if o != n and self.can_act(o)}
                self.emit(Event(n, street, 'bet' if to_call <= 0 else 'raise',
                                amt, level, before, max(0, to_call)))

    def normalize(self, action, s, current):
        """Map whatever came back (bot tuple, or the human's action dict) onto a legal
        action - same rule as engine.py's _Hand.normalize: illegal input never crashes
        or acts out of turn, it's clamped or folded/checked instead."""
        try:
            kind = action[0]
        except Exception:
            kind = None
        if kind == 'raise':
            try:
                to = int(action[1])
            except Exception:
                to = None
            if to is not None and s.max_raise_to > current:
                to = min(max(to, s.min_raise_to), s.max_raise_to)
                if to > current:
                    return 'raise', to
            kind = 'call'
        if kind == 'call':
            return ('call', None) if s.to_call > 0 else ('check', None)
        if kind == 'check':
            return ('check', None) if s.to_call <= 0 else ('fold', None)
        if kind == 'fold':
            return 'fold', None
        return ('check', None) if s.to_call <= 0 else ('fold', None)

    def summary(self, nets, shown):
        human = self.t.human_name
        folded_on = next((e.street for e in self.history if e.player == human and e.kind == 'fold'),
                         None)
        classes = []
        for street, n in (('flop', 3), ('turn', 4), ('river', 5)):
            if len(self.board) < n or (folded_on and STREETS.index(folded_on) < STREETS.index(street)):
                break
            classes.append({'street': street, 'class': classify(self.holes[human], self.board[:n])})
        return {
            'board': ''.join(self.board),
            'pot': sum(self.contrib.values()),
            'nets': dict(nets),
            'shown': {n: ''.join(cards) for n, cards in shown.items()},
            'stacks': dict(self.t.stacks),
            'rebuys': dict(self.t.rebuys),
            'history': [event_json(e) for e in self.history],
            'human_hole': ''.join(self.holes[human]),
            'classes': classes,
            'positions': dict(self.positions),
        }
