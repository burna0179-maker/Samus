# ComfyUI AI Influencer Workflow — end-to-end pipeline
Source: ChatGPT recovery chat 37

**Canonical relationship:**
- [NEW] generation pipeline for `nova_hart_persona_spec.md`
- [EXPANDS §6 application] creative asset generation pod (not orchestration-critical)
- Reference implementation for face-consistent influencer content

## Pipeline diagram
```
[Face Generation]
     ↓
[FaceID Embed]  ← identity locked
     ↓
[Full Body ControlNet Pipeline]
     ↓
[AnimateDiff / SVD Motion]
     ↓
[SadTalker / Wav2Lip for speech]
     ↓
[Merge (optional)]
     ↓
[Upscale + Refinement]
     ↓
[Vertical Social Export]
```

## Stage 1 — Base identity (face)
**Goal:** Lock in brand-safe influencer face.

- **Models:** SDXL — JuggernautXL, RealVisXL, RevAnimatedXL
- **LoRA add-ons:** "Realistic Female Face", "Face Consistency / Character LORA"
- **Settings:** Steps 25-35, CFG 4-6, Sampler DPM++ 2M Karras, Resolution 832×1216 (portrait)
- **Output:** 6-12 master face reference images

## Stage 2 — Identity embedding
**Goal:** persistent identity across angles/poses.

- **IP Adapter FaceID** (recommended)
- **InsightFace / FaceID Plus**
- **ControlNet Face** for stability

Upload master face → system locks embedding.

## Stage 3 — Full-body generation
- **Models:** SDXL Realistic Vision, JuggernautXL, DreamShaperXL (or fitness/lifestyle/fashion-specific)
- **Additions:**
  - ControlNet OpenPose (poses)
  - ControlNet Depth / LineArt / Canny (structure)
- **Prompt strategy:** body type + clothing + environment
- **Resolution:** 1024×1536 or 1216×1792 (vertical social)

## Stage 4 — Motion (video)

### Option A — AnimateDiff XL
- Best for short "motion shots" (walking, hair flip, posing, turning, arm movement)
- Input: full-body still → AnimateDiff → 2-3 sec movement
- Depth model maintains structure
- FaceID adapter maintains identity during motion

### Option B — Stable Video Diffusion (SVD / SVD-XT)
- Better for cinematic content
- Frame-prep node → SVD → upscaler
- Add RIFE or FILM interpolation for 30-60 FPS

## Stage 5 — Talking (lip sync + voice)
- **SadTalker** — best expression + head movement
- **Wav2Lip** — simpler, stable
- **VASA-1 style** — when available

Process: feed face frame + voiceover (ElevenLabs / XTTS-v2 / Bark) → talking video

## Stage 6 — Merge (talking head + full body)
If needed: full-body idle loop + overlay talking head via:
- DaVinci Resolve Fusion
- Runway Gen-1 + Gen-2 in-painting
- Premiere with mask tracking

## Stage 7 — Final enhancement
- **ESRGAN / UHD4K Upscale**
- **FaceDetailer / FaceRefiner**
- **Film Grain Node** (subtle)
- **Color Grade Node** (Teal/Orange, Blue/Gold, Soft-Realistic)

**Goal:** make output look like studio-shot lifestyle photography, NOT AI-generated.

## Stage 8 — Export
- **Vertical 1080×1920**
- **4K when possible** (2160×3840)
- Compatible with IG / TikTok / YT Shorts
- Add overlays in CapCut / Premiere:
  - Text overlays
  - Branding
  - HustleForge color accents (#0A0D11 base, teal accent)
  - Music beat sync
  - CTA

## Integration with HustleForge SMMS
Output assets flow into:
1. SMMS content queue (per `pod_receptionist` + content pods)
2. Pillar-tagged (per `nova_hart_persona_spec.md` taxonomy)
3. Scheduled in CapCut/Buffer/native scheduler
4. Performance feedback → `crm_feedback_engine.py` style metrics

## Deferred
- Full ComfyUI `.json` workflow export
- LoRA fine-tuning on additional reference shots
- Multi-character workflows (Nova + cast)
- Brand-specific style transfer LoRAs per client
