"""Check that every bot the trainer offers can sit down and play.

    python smoke_test.py

For each bot: seat it against you, play a few hands with random actions through the
same request handlers the site uses, and make sure replaying the session gives the
same result (each request replays the table, so a bot that doesn't replay the same
way would break hands mid-game). Exits non-zero if any bot fails; the pokerbot sync
workflow runs this before it updates the deployed site.
"""
import json
import random
import sys

from server import bot_list, decode_token, dispatch, locate_pokerbot, play

HANDS = 6


def call(path, body):
    code, _, payload = dispatch('POST', path, '', json.dumps(body).encode())
    data = json.loads(payload)
    if code != 200:
        raise AssertionError(f'{path} -> {code}: {data.get("error")}')
    return data


def same(token):
    _, first = play(token)
    _, second = play(token)
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def exercise(bot_id, seed):
    rng = random.Random(seed)
    seats = [{'kind': 'human'}, {'kind': 'bot', 'bot': bot_id}, {'kind': 'bot', 'bot': bot_id},
             {'kind': 'bot', 'bot': 'REGULAR'}]
    data = call('/api/table', {'seats': seats, 'seed': seed})
    hands = 0
    for _ in range(400):
        if data['status'] == 'hand_end':
            hands += 1
            if hands >= HANDS:
                break
            data = call('/api/next_hand', {'session': data['session']})
            continue
        st = data['state']
        r = rng.random()
        if r < 0.2 and st['to_call'] > 0:
            action = {'kind': 'fold'}
        elif r < 0.45 and st['max_raise_to'] > st['committed'] + st['to_call']:
            action = {'kind': 'raise', 'to': rng.randint(st['min_raise_to'], st['max_raise_to'])}
        else:
            action = {'kind': 'call' if st['to_call'] else 'check'}
        data = call('/api/act', {'session': data['session'], **action})
    if not same(decode_token(data['session'])):
        raise AssertionError('replaying the same session gave a different result')
    return hands


def main():
    print(f'pokerbot: {locate_pokerbot()}')
    failed = []
    for b in bot_list():
        try:
            hands = sum(exercise(b['id'], seed) for seed in (1, 2))
            print(f'  ok    {b["id"]:<12} {b["group"]:<9} {hands} hands')
        except Exception as e:
            failed.append(b['id'])
            print(f'  FAIL  {b["id"]:<12} {type(e).__name__}: {e}')
    if failed:
        print(f'{len(failed)} bot(s) failed: {", ".join(failed)}')
        sys.exit(1)
    print(f'all {len(bot_list())} bots ok')


if __name__ == '__main__':
    main()
