#!/usr/bin/env python3
"""ESPN play-log names vs full player names. Run: python3 live/test_play_names.py"""
import poller as P

for text, full in [("A.St. Brown", "Amon-Ra St. Brown"), ("Bi.Robinson", "Bijan Robinson"), ("Ja.Williams", "Jameson Williams"),
                   ("Mi.Wilson", "Michael Wilson"), ("Z.Knight", "Bam Knight"), ("J.Dobbins", "J.K. Dobbins"),
                   ("M.Harrison", "Marvin Harrison Jr."), ("O.Gordon", "Ollie Gordon II")]:
    assert P.same_player(P.play_name(text), full), (text, full)
assert not P.same_player("mi.wilson", "Marvin Wilson")
assert not P.same_player("br.robinson", "Bijan Robinson")
assert not P.same_player("a.brown", "Amon-Ra Smith")
for text, name in [("A.St. Brown pass", "A.St. Brown"), ("(Shotgun) K.Van Noy left", "K.Van Noy"), ("J.Goff FUMBLES", "J.Goff")]:
    assert P.RUSHER.search(text).group(1) == name, text
assert P.PASS_TO.search("J.Goff pass short right to A.St. Brown to DET 40").group(1) == "A.St. Brown"
print("ok")
