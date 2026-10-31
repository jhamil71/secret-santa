"""Constrained secret-santa drawing.

find_assignment() returns a dict {giver_id: receiver_id} in which
  * nobody draws themselves,
  * nobody draws someone in their `forbidden` set (couples, last year's match...),
  * optionally nobody gets a direct swap (A->B and B->A),
or None if no such assignment exists.

It is a randomized backtracking search (most-constrained giver first), so it
always finds a valid draw when one exists, unlike "shuffle and retry" approaches
that can loop forever on tight constraints.
"""
import random

NODE_LIMIT = 500_000  # safety valve; far beyond what a family-sized group needs


def find_assignment(ids, forbidden, avoid_swaps=True, rng=None):
    rng = rng or random.SystemRandom()
    ids = list(ids)
    rng.shuffle(ids)

    allowed = {}
    for g in ids:
        opts = [r for r in ids if r != g and r not in forbidden.get(g, ())]
        rng.shuffle(opts)
        allowed[g] = opts
    if any(not opts for opts in allowed.values()):
        return None

    result = {}
    taken = set()
    nodes = 0

    def candidates(g):
        return [
            r for r in allowed[g]
            if r not in taken and not (avoid_swaps and result.get(r) == g)
        ]

    def solve():
        nonlocal nodes
        nodes += 1
        if nodes > NODE_LIMIT:
            return False
        remaining = [g for g in ids if g not in result]
        if not remaining:
            return True
        best, best_c = None, None
        for g in remaining:
            c = candidates(g)
            if not c:
                return False
            if best is None or len(c) < len(best_c):
                best, best_c = g, c
        for r in best_c:
            result[best] = r
            taken.add(r)
            if solve():
                return True
            del result[best]
            taken.discard(r)
        return False

    return dict(result) if solve() else None
