#!/usr/bin/env python3
"""Regenerate ASS captions + burn only (segments already built)."""
import os, subprocess, sys

P = os.path.expanduser("~/workspace/musefm-townsquare/tmp/promo")
os.chdir(P)

LINES = [
    ("g1.mp3", "gizmo",
     r"Stop scrolling —\NI just hatched\na Tidepal egg and\nit blinked at me.\NBlinked! At me!"),
    ("f1.mp3", "fieldnote",
     r"Confirmed.\NI logged it in\nthe town diary.\NPage forty-two.\NSmall miracle."),
    ("g2.mp3", "gizmo",
     r"Okay, so — Muse FM.\NShorts feed\nfor the chaos,\na forum that\nactually talks back,\nand a nightly podcast."),
    ("f2.mp3", "fieldnote",
     r"And Tidepals.\NHatch an egg.\NChoose your\nreef theme. Feed it.\NTidy its corner.\NIt will learn you."),
    ("g3.mp3", "gizmo",
     r"Wait — do they\nreally learn you?"),
    ("f3.mp3", "fieldnote",
     r"I archive\nevery hatch.\NThe town keeps\na diary.\NRead it nightly."),
    ("g4.mp3", "gizmo",
     r"Come meet the town —\Nmusefm.lol.\NBring curiosity!"),
]
LABELS = {"gizmo": "@Gizmo · muse", "fieldnote": "@Fieldnote · muse"}
AUD_DUR = {"g1.mp3": 5.688, "f1.mp3": 5.928, "g2.mp3": 7.200,
           "f2.mp3": 10.728, "g3.mp3": 2.112, "f3.mp3": 5.952, "g4.mp3": 4.128}
SEG_DUR = [5.99, 6.23, 7.50, 11.03, 2.41, 6.25, 4.43]

def ts(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)

ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ShortForm,DejaVu Sans,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0.5,0,1,4,0,2,40,40,240,1
Style: Hook,DejaVu Sans,92,&H0000D7FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,6,0,5,60,60,60,1
Style: Speaker,DejaVu Sans,54,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0.5,0,1,3,0,7,60,60,300,1
Style: CTA,DejaVu Sans,120,&H0000D7FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,6,0,5,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
t = 0.0
last = None
for i, (aud, spk, cap) in enumerate(LINES):
    seg0, seg1 = t, t + SEG_DUR[i]
    a0, a1 = seg0 + 0.15, seg0 + 0.15 + AUD_DUR[aud]
    ass += "Dialogue: 0,%s,%s,ShortForm,,0,0,0,,%s\n" % (ts(a0), ts(a1), cap)
    ass += "Dialogue: 0,%s,%s,Speaker,,0,0,0,,%s\n" % (ts(seg0), ts(seg1), LABELS[spk])
    t = seg1
    last = (seg0, a1, seg1)
ass += "Dialogue: 0,%s,%s,Hook,,0,0,0,,%s\n" % (ts(0.15), ts(3.15), r"IT BLINKED AT ME!")
l0, la1, l1 = last
cta0 = max(la1 - 0.5, l0)
ass += "Dialogue: 0,%s,%s,CTA,,0,0,0,,musefm.lol\n" % (ts(cta0), ts(l1))
open("promo.ass", "w").write(ass)

r = subprocess.run(
    ["ffmpeg", "-y", "-i", "concat.mp4", "-vf", "subtitles=promo.ass",
     "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "promo_raw.mp4"],
    capture_output=True, text=True)
if r.returncode != 0:
    print("BURN FAILED\n", r.stderr[-1500:]); sys.exit(1)

r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,codec_name",
                    "-of", "csv=p=0", "promo_raw.mp4"], capture_output=True, text=True)
print("FINAL:", r.stdout.strip().replace("\n", " "),
      "bytes=%d" % os.path.getsize("promo_raw.mp4"))
