# PlugICT — Knowledge Graph Brand Film

A 26-second, 1080p30 cinematic animation that presents PlugICT's ICT concept
vault as a glowing, living knowledge graph, ending on the PLUGICT wordmark and
the "Search from ICT vault" tagline. Final output:
`assets/video/plugict-knowledge-graph.mp4`.

## Narrative timeline

| Time | Beat |
|---|---|
| 0.0 – 2.4 s | Darkness; ambient particles fade in over midnight-blue haze |
| 2.4 – 12.5 s | Concept nodes ignite one by one (FVG → Distribution) and connect; labels fade in |
| 12.5 – 17.2 s | Full network alive — pulses travel key relationships (Liquidity ↔ Draw on Liquidity, Accumulation → Manipulation → Distribution, Kill Zone ↔ Silver Bullet…) while the camera glides through |
| 17.2 – 20.7 s | Every light in the scene streams into the center and condenses into the PLUGICT letterforms; sub-bass impact on the flash |
| 20.7 – 26.0 s | End card: gradient wordmark, divider, “Search from ICT vault”, fade out |

## How it's made

- `index.html` — deterministic canvas renderer. Every frame is a pure function
  of the frame index (`captureFrame(i)`): seeded PRNG, Catmull-Rom camera path,
  3D projection with depth-of-field, pre-rendered glow sprites, label collision
  avoidance, and a particle→wordmark morph sampled from the actual typeface.
- `render.mjs` — Playwright harness that captures frames as PNGs
  (`FRAMES=1,2,3` env var renders a preview subset).
- `encode.sh` — ffmpeg encode (libx264, CRF 17) plus a fully synthesized
  ambient score: two detuned drones, a slow air swell, a riser into the
  convergence, and a soft sub impact on the flash.

## Reproduce

```bash
# fonts: Inter + Space Grotesk must be installed (fc-cache visible)
node render.mjs frames          # ~3 min, 780 PNGs
./encode.sh frames ../plugict-knowledge-graph.mp4
```

Requires Node ≥ 20 with Playwright (Chromium) and ffmpeg with libx264/aac.
Typefaces: Space Grotesk (wordmark), Inter (labels/tagline) — OFL licensed.
