#!/usr/bin/env python3
"""Call gate: trailing and big-lead receiving calls are vetoed. Run: .venv/bin/python live/test_call_gate.py"""
import os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller as P

def x(diff, rush=False):
    v = [0.0] * 19
    v[9], v[15] = diff, 1.0 if rush else 0.0
    return v

K = "rec|ZZZ|WR"   # not a top-3 defense
assert P.call_gate(x(0), K) and P.call_gate(x(-3), K) and P.call_gate(x(13), K)
assert not P.call_gate(x(-4), K)                       # trailing by 4+
assert not P.call_gate(x(14), K) and not P.call_gate(x(21), K)   # Higgins up 14 in Q4, MHJ up 15
assert P.call_gate(x(14, rush=True), "rush|ZZZ|RB")    # leaders run more: rushing unaffected
print("ok")
