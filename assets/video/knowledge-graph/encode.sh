#!/usr/bin/env bash
# Encode the rendered frame sequence into the final film with an ambient score.
# Usage: ./encode.sh <frames_dir> <out.mp4>
set -euo pipefail

FRAMES_DIR="${1:?frames dir}"
OUT="${2:?output mp4}"

# Ambient bed, synthesized (26 s):
#   a0  55 Hz drone with slow tremolo        — foundation
#   a1  82.41 Hz (E2) detune layer           — warmth
#   a2  brown noise, lowpassed, slow swell   — air / space
#   a3  wind riser into the convergence      — 17.2 s .. 20.35 s
#   a4  soft sub impact at the flash         — 20.3 s
ffmpeg -y \
  -framerate 30 -i "$FRAMES_DIR/frame_%04d.png" \
  -f lavfi -i "sine=frequency=55:duration=26" \
  -f lavfi -i "sine=frequency=82.41:duration=26" \
  -f lavfi -i "anoisesrc=color=brown:duration=26:seed=42:sample_rate=44100" \
  -f lavfi -i "anoisesrc=color=pink:duration=26:seed=7:sample_rate=44100" \
  -f lavfi -i "sine=frequency=42:duration=26" \
  -filter_complex "\
[1]volume=0.30,tremolo=f=0.13:d=0.35[a0];\
[2]volume=0.15,tremolo=f=0.11:d=0.30[a1];\
[3]lowpass=f=350,volume='0.030+0.024*sin(PI*t/26)':eval=frame[a2];\
[4]lowpass=f=1800,highpass=f=250,volume='if(between(t,17.2,20.35),0.05*pow((t-17.2)/3.15,2),0)':eval=frame[a3];\
[5]volume='if(lt(t,20.30),0,if(lt(t,20.36),(t-20.30)/0.06,exp(-(t-20.36)*1.7)))*0.5':eval=frame[a4];\
[a0][a1][a2][a3][a4]amix=inputs=5:normalize=0,\
lowpass=f=2400,volume=2.0,afade=t=in:d=2.5,afade=t=out:st=23.4:d=2.6,\
alimiter=limit=0.6,aformat=sample_rates=44100:channel_layouts=stereo[aout]" \
  -map 0:v -map "[aout]" \
  -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -b:a 192k -shortest \
  "$OUT"

echo "wrote $OUT"
