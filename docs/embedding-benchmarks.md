Dual embedding architecture decided 2026-03-28:
- **Main memory** (turn-recall hooks): model2vec potion-base-32M, 512-dim, <1ms. Speed-critical.
- **Knowledge base** (LightRAG): EmbeddingGemma-300M ONNX fp32, 768-dim, ~20ms. Quality-critical.

## Hardware: RTX 2060 (4GB VRAM), 64GB RAM, Windows 10

| Model | Dims | MTEB | Retrieval | CPU ONNX | Notes |
|-------|------|------|-----------|----------|-------|
| model2vec potion-base-32M | 512 | 52 | 33 | <1ms | Current, static lookup |
| bge-small-en-v1.5 | 384 | 53 | 44 | ~5-8ms | Good upgrade for memory if needed |
| granite-embed-small-eng-r2 | 384 | 56 | 52 | ~8-10ms | ModernBERT, best small |
| EmbeddingGemma-300M | 768 | 70 | ~59 | ~20ms fp32, 194ms quantized | **Chosen for KB** |

**Key finding:** ONNX quantized EmbeddingGemma is 10x SLOWER than fp32 for single queries. Always use fp32 (onnx/model.onnx).

HuggingFace repos:
- `onnx-community/embeddinggemma-300m-ONNX` (ungated, model_kwargs={"file_name": "onnx/model.onnx"})
- `google/embeddinggemma-300m` (gated — requires license acceptance)
- `unsloth/embeddinggemma-300m` (ungated PyTorch)
