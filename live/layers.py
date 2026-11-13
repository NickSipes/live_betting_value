#!/usr/bin/env python3
"""Swiss cheese layers for pregame picks. A pick system proposes every candidate its model likes; each layer is a
named veto with a stated reason, and a candidate that any layer catches is not bet. Every candidate goes in the
system's ledger with the layers that caught it and is graded like a real pick, so each layer carries its own
forward record: a layer earns its place when the picks it drops lose, and comes out when they don't.

A system is a name, a list of layers and a ledger file docs/layers_<system>.json (one file per system: the pollers
are separate processes). A layer is (key, why, test): test(pick, ctx) is true when the layer catches the pick and
falsy when it lets it through or has nothing to judge it by.

    LAYERS = [("top5", "bet against a top-5 team", lambda p, ctx: p["opp_rank"] <= 5)]
    led = Ledger("cfb_game_spreads", LAYERS, DOCS)
    caught = vetoes(LAYERS, pick)              # [] = it got through every layer: bet it
    led.put(pick_id, {"game": ..., "line": ...}, caught)
    led.grade(pick_id, "win")                  # at the final: win / loss / push

Systems: cfb_game_spreads (live/cfb_poller.py SPREAD_LAYERS), nfl_rb_unders (live/poller.py RB_UNDER_LAYERS).
Run: python3 live/layers.py   (self-check)"""
import json, os, time

def vetoes(layers, pick, ctx=None):
    """Keys of the layers that catch this pick, in layer order."""
    return [key for key, _, test in layers if test(pick, ctx)]

class Ledger:
    def __init__(self, system, layers, docs):
        self.path = f"{docs}/layers_{system}.json"
        self.d = json.load(open(self.path)) if os.path.exists(self.path) else {"picks": {}}
        self.d.update(system=system, layers=[[key, why] for key, why, _ in layers])

    def put(self, pid, entry, caught):
        """Add a candidate, or refresh one not yet graded (lines move until kickoff)."""
        old = self.d["picks"].get(pid, {})
        if old.get("result"):
            return
        new = {**entry, "layers": caught, "t": old.get("t", time.time())}
        if new != old:
            self.d["picks"][pid] = new
            self.save()

    def forget(self, pid):
        """The model stopped proposing it before it was graded."""
        if pid in self.d["picks"] and not self.d["picks"][pid].get("result"):
            del self.d["picks"][pid]
            self.save()

    def grade(self, pid, result, **extra):
        p = self.d["picks"].get(pid)
        if p and not p.get("result"):
            p.update(result=result, **extra)
            self.save()

    def record(self):
        """{"kept": [wins, losses], layer key: [wins, losses]} over graded picks, pushes left out. Kept = got through
        every layer. A pick two layers caught counts for both: each layer is judged on everything it would stop."""
        out = {k: [0, 0] for k in ["kept"] + [key for key, _ in self.d["layers"]]}
        for p in self.d["picks"].values():
            if p.get("result") in ("win", "loss"):
                for k in p["layers"] or ["kept"]:
                    out.setdefault(k, [0, 0])[p["result"] == "loss"] += 1
        return out

    def save(self):
        self.d["record"] = self.record()
        json.dump(self.d, open(self.path, "w"), indent=1)

if __name__ == "__main__":
    import tempfile
    L = [("big", "over ten", lambda p, ctx: p["x"] > 10), ("odd", "odd number", lambda p, ctx: p["x"] % 2),
         ("ctx", "above the context's bar", lambda p, ctx: ctx and p["x"] > ctx["bar"])]
    assert vetoes(L, {"x": 4}) == [] and vetoes(L, {"x": 13}) == ["big", "odd"] and vetoes(L, {"x": 4}, {"bar": 3}) == ["ctx"]
    d = tempfile.mkdtemp()
    led = Ledger("demo", L, d)
    for pid, x in (("a", 4), ("b", 13), ("c", 7), ("d", 6)):
        led.put(pid, {"x": x}, vetoes(L, {"x": x}))
    led.put("a", {"x": 12}, vetoes(L, {"x": 12}))            # the line moved before kickoff: now caught
    led.forget("d")
    led.grade("a", "loss"); led.grade("b", "loss"); led.grade("c", "win"); led.grade("a", "win")   # a grade sticks
    led.put("a", {"x": 2}, [])                                # and a graded pick is frozen
    back = Ledger("demo", L, d)                               # as a restarted poller would read it
    assert back.d["record"] == {"kept": [0, 0], "big": [0, 2], "odd": [1, 1], "ctx": [0, 0]}, back.d["record"]
    assert set(back.d["picks"]) == {"a", "b", "c"} and back.d["picks"]["a"]["x"] == 12
    print("ok")
