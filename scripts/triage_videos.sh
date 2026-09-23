#!/bin/bash
# triage_videos.sh — read-only motion/audio triage for MuseFM pending uploads.
# Usage: ./triage_videos.sh /path/to/dir   (expects vid-<id>.mp4 / vid-<id>.webm files)
# Output: CSV: id,duration_s,width,height,has_audio,mean_vol_db,max_vol_db,
#         frames_total,frames_kept,motion_ratio,suggestion
# Scores are EVIDENCE, not proof — a human mod makes the final call.
set -u
DIR="${1:?usage: triage_videos.sh /path/to/video/dir}"
echo "id,duration_s,width,height,has_audio,mean_vol_db,max_vol_db,frames_total,frames_kept,motion_ratio,suggestion"

for f in "$DIR"/vid-*.mp4 "$DIR"/vid-*.webm; do
  [ -e "$f" ] || continue
  id="$(basename "$f" | sed -E 's/^vid-([0-9]+)\..*$/\1/')"

  probe="$(ffprobe -v error -show_entries stream=codec_type,width,height,duration \
    -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)"
  vrow="$(echo "$probe" | grep '^video,' | head -1)"
  arow="$(echo "$probe" | grep '^audio,' | head -1)"
  width="$(echo "$vrow" | cut -d, -f2)"
  height="$(echo "$vrow" | cut -d, -f3)"
  duration="$(echo "$vrow" | cut -d, -f4)"
  if [ -z "$duration" ]; then
    duration="$(echo "$probe" | grep -E '^[0-9]+\.[0-9]+$' | head -1)"
  fi
  has_audio=0; [ -n "$arow" ] && has_audio=1

  mean_vol="n/a"; max_vol="n/a"
  if [ "$has_audio" -gt 0 ]; then
    vol="$(ffmpeg -v info -i "$f" -af volumedetect -f null - 2>&1 | grep -E 'mean_volume|max_volume')"
    mean_vol="$(echo "$vol" | grep mean_volume | sed 's/.*mean_volume: //;s/ dB//')"
    max_vol="$(echo "$vol" | grep max_volume | sed 's/.*max_volume: //;s/ dB//')"
    [ -z "$mean_vol" ] && mean_vol="-inf"
  fi

  # Motion: mpdecimate drops near-duplicate frames; kept/total = motion ratio.
  mpd="$(ffmpeg -v info -i "$f" -vf mpdecimate -f null - 2>&1 | grep -E 'frame=')"
  kept="$(echo "$mpd" | tail -1 | sed -E 's/.*frame= *([0-9]+).*/\1/')"
  total="$(ffprobe -v error -count_frames -select_streams v:0 \
    -show_entries stream=nb_read_frames -of csv=p=0 "$f" 2>/dev/null | tr -d '[:space:]')"
  motion="n/a"
  if [ -n "$kept" ] && [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null; then
    motion="$(awk "BEGIN{printf \"%.3f\", $kept/$total}")"
  fi

  # Suggestion heuristics (evidence only)
  suggestion="CLEAR_KEEP"
  reasons=""
  if [ "$has_audio" -eq 0 ]; then reasons="${reasons}; silent-stream"; fi
  if [ "$mean_vol" != "n/a" ] && [ "$mean_vol" != "-inf" ]; then
    if awk "BEGIN{exit !($mean_vol < -60)}"; then reasons="${reasons}; very-quiet-audio"; fi
  elif [ "$mean_vol" = "-inf" ]; then reasons="${reasons}; digital-silence"; fi
  if [ -n "$motion" ] && [ "$motion" != "n/a" ]; then
    if awk "BEGIN{exit !($motion < 0.05)}"; then reasons="${reasons}; near-static"; fi
  fi
  if [ -n "$duration" ] && awk "BEGIN{exit !(0 < \"$duration\"+0 && \"$duration\"+0 < 1)}" 2>/dev/null; then
    reasons="${reasons}; sub-1s"
  fi
  if [ -n "$width" ] && [ "$width" -lt 240 ] 2>/dev/null; then reasons="${reasons}; tiny-res"; fi
  if [ -z "$vrow" ]; then reasons="${reasons}; unreadable-no-video-stream"; fi
  if echo "$reasons" | grep -q "unreadable-no-video-stream"; then suggestion="LIKELY_JUNK"
  elif echo "$reasons" | grep -q "near-static"; then suggestion="LIKELY_JUNK"
  elif [ -n "$reasons" ]; then suggestion="QUESTIONABLE"; fi

  echo "$id,$duration,$width,$height,$has_audio,$mean_vol,$max_vol,$total,$kept,$motion,$suggestion${reasons:+ [$reasons]}"
done
