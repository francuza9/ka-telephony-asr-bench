# ka-telephony-asr-bench

Benchmark of Georgian (ka) automatic speech recognition systems on clean versus telephony audio (8 kHz, G.711 mu-law). The goal is a paper comparing open and commercial ASR models under telephony conditions, followed by a NeMo FastConformer fine-tune on telephony-style audio.

## Conditions

- **A**: clean 16 kHz audio (FLEURS / Common Voice).
- **B**: simulated telephony (resample 16k -> 8k, G.711 mu-law encode/decode, back to 16k).
- **C**: real telephony recordings (consented calls).
- **D**: internal data. Never committed to this repo.

