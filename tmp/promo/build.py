#!/usr/bin/env python3
"""Build the Fieldnote+Gizmo promo: per-line segments, concat, burn ASS captions."""
import json, os, subprocess, sys

P = os.path.expanduser("~/workspace/musefm-townsquare/tmp/promo")
FN_IMG = os.path.join(P, "media-generation-fieldnote-muse-0-26108a27-d8b3-4e1f-a1e6-a9dd6506efe3.webp")
GZ_IMG = os.path.join(P, "media-generation-gizmo-muse-0-2376c7cc-298e-49cd-84cf-ae14d5d98017.webp")
TTS = os.path.join(P, "tts", "lines")

# (audio, speaker, caption text with \N breaks, label)
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
IMGS = {"gizmo": GZ_IMG, "fieldnote": FN_IMG}
LABELS = {"gizmo": "@Gizmo · muse", "fieldnote": "@Fieldnote · muse"}

PAD = 0.30  # 0.15s head + 0.15s tail per segment

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("FAILED:", " ".join(cmd[:6]), "\n", r.stderr[-1500:])
        sys.exit(1)
    return r

def dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    return float(r.stdout.strip())

def ts(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)

segments, starts = [], []
t = 0.0
os.chdir(P)
for i, (aud, spk, cap) in enumerate(LINES):
    a = os.path.join(TTS, aud)
    d = dur(a)
    seg_dur = d + PAD
    n = max(1, round(seg_dur * 30))
    out = "seg_%02d.mp3.mp4" % i
    fc = (
        "[0:v]scale=2160:-2,"
        "zoompan=z='1+0.10*on/%d':x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)+14*sin(2*PI*on/75)':d=%d:s=1080x1920:fps=30[v0];"
        "[1:v]scale=360:360,setsar=1[in];"
        "[v0][in]overlay=W-w-50:300[v];"
        "[2:a]adelay=150|150,apad[a]" % (n, n)
    )
    run(["ffmpeg", "-y", "-framerate", "30", "-i", IMGS[spk], "-i",
         IMGS["fieldnote" if spk == "gizmo" else "gizmo"], "-i", a,
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-t", "%.3f" % seg_dur, "-c:v", "libx264", "-preset", "fast",
         "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         out])
    segments.append(out)
    starts.append((t, t + 0.15, t + 0.15 + d, t + seg_dur, spk, cap))
    t += seg_dur
    print("seg %d: %.2fs cum=%.2f" % (i, seg_dur, t), flush=True)

with open("concat.txt", "w") as f:
    for s in segments:
        f.write("file '%s'\n" % s)
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
     "-c", "copy", "concat.mp4"])

# ---- ASS captions ----
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
for (seg0, a0, a1, seg1, spk, cap) in starts:
    ass += "Dialogue: 0,%s,%s,ShortForm,,0,0,0,,%s\n" % (ts(a0), ts(a1), cap)
    ass += "Dialogue: 0,%s,%s,Speaker,,0,0,0,,%s\n" % (ts(seg0), ts(seg1), LABELS[spk])
# hook text over first 3s
ass += "Dialogue: 0,%s,%s,Hook,,0,0,0,,%s\n" % (ts(0.15), ts(3.15), r"IT BLINKED AT ME!")
# CTA over the last segment's tail
l0, la0, la1, l1, lspk, lcap = starts[-1]
cta0 = max(la1 - 0.5, l0)
ass += "Dialogue: 0,%s,%s,CTA,,0,0,0,,musefm.lol\n" % (ts(cta0), ts(l1))
with open("promo.ass", "w") as f:
    f.write(ass)

run(["ffmpeg", "-y", "-i", "concat.mp4", "-vf", "subtitles=promo.ass",
     "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "promo_raw.mp4"])

d = dur("promo_raw.mp4")
r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name",
         "-of", "csv=p=0", "promo_raw.mp4"])
print("FINAL:", r.stdout.strip().replace("\n", " "), "dur=%.1f" % d,
      "bytes=%d" % os.path.getsize("promo_raw.mp4"))
print(json.dumps({"total_dur": round(d, 2),
                  "bytes": os.path.getsize("promo_raw.mp4")}))
