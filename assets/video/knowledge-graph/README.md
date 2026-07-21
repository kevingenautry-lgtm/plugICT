# PlugICT — Knowledge Graph Brand Film

A 1080p30 cinematic animation that presents PlugICT's ICT concept vault as a
glowing, living knowledge graph of 27 concepts, ending on the golden *plug*
**ICT** lockup and the "Search the ICT vault" tagline. Final output:
`assets/video/plugict-knowledge-graph.mp4`.

The renderer produces a 26s master; the shipped video is encoded with
`HEAD=1.5` (see `encode.sh`), trimming the first 1.5s of intro darkness so the
network appears sooner — final length **24.5s**.

Concepts shown: FVG, IFVG, Liquidity, Displacement, Market Structure Shift · MSS,
Order Block, Breaker, Mitigation Block, Premium/Discount, OTE, Judas Swing,
Kill Zone, Silver Bullet, Draw on Liquidity, Rebalancing, Consequent
Encroachment, Accumulation, Manipulation, Distribution, CISD, Unicorn Model,
SMT Divergence, Liquidity Sweep, Turtle Soup, Power of 3, BPR, Equal Highs/Lows.

## Narrative timeline

| Time | Beat |
|---|---|
| 0.0 – 2.4 s | Darkness; ambient particles fade in over midnight-blue haze |
| 2.4 – 12.5 s | Concept nodes ignite one by one (FVG → Distribution) and connect, the cloud expanding to fill the frame; bright-white, black-outlined labels fade in (auto-flipping to a node's left near the edge so nothing leaks off-frame) |
| 12.5 – 17.2 s | Full network alive — pulses travel key relationships (Liquidity ↔ Draw on Liquidity, Accumulation → Manipulation → Distribution, Kill Zone ↔ Silver Bullet…) while the camera glides through |
| 17.2 – 20.6 s | Every light in the scene streams inward, turning gold as it condenses into the (enlarged) *plug* **ICT** letterforms; warm flash + sub-bass impact |
| 20.6 – 26.0 s | Forge reveal: a flare sweeps left→right transmuting the particle-word into the solid golden lockup; divider draws from a glint; "Search the ICT vault" cascades in letter by letter; embers rise; fade out |

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
