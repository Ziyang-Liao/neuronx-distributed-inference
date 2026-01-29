# Florence-2 Neuron (BF16 Optimized)

High-performance Florence-2 inference on AWS Inferentia2/Trainium using bfloat16 precision.

## Performance

| Metric | FP32 | BF16 | Improvement |
|--------|------|------|-------------|
| CAPTION latency | 393ms | 252ms | **36%** |
| OD latency | 347ms | 237ms | **32%** |
| Single-core QPS | 2.82 | 4.09 | **45%** |
| Dual-core QPS | 5.64 | 8.18 | **45%** |

Tested on inf2.xlarge (2 NeuronCores).

## Requirements

- AWS Inferentia2 instance (inf2.xlarge or larger)
- Python 3.8+
- PyTorch 2.1+ with Neuron SDK

```bash
pip install torch-neuronx neuronx-cc transformers einops timm pillow requests
```

## Quick Start

### 1. Compile Models

```bash
python compile.py --output-dir ./compiled_bf16
```

Compilation takes ~10 minutes and creates:
- `stage0-3.pt` - Vision encoder stages
- `projection.pt` - Vision-to-language projection
- `encoder.pt` - Language encoder
- `decoder_{1,4,8,16,32,64}.pt` - Decoder buckets

### 2. Run Inference

```python
from inference import Florence2NeuronBF16

model = Florence2NeuronBF16("./compiled_bf16", core_id="0")

# Image captioning
result = model("image.jpg", "<CAPTION>")
print(result)

# Object detection
result = model("image.jpg", "<OD>")
print(result)

# OCR
result = model("image.jpg", "<OCR>")
print(result)
```

### 3. Benchmark

```bash
# Single-core benchmark
python benchmark.py --image test.jpg

# Dual-core stress test (5 minutes)
python benchmark.py --stress --duration 300 --core 0 &
python benchmark.py --stress --duration 300 --core 1 &
```

## Supported Tasks

| Task | Prompt | Description |
|------|--------|-------------|
| Caption | `<CAPTION>` | Brief image description |
| Detailed Caption | `<DETAILED_CAPTION>` | Detailed description |
| Object Detection | `<OD>` | Detect objects with bboxes |
| OCR | `<OCR>` | Extract text from image |
| Region Caption | `<REGION_CAPTION>` | Caption specific region |

## Architecture

```
Image (768x768)
    │
    ▼
┌─────────────────────────────────────┐
│  Vision Encoder (4 stages, Neuron)  │
│  stage0 → stage1 → stage2 → stage3  │
└─────────────────────────────────────┘
    │
    ▼ (576 x 1024)
┌─────────────────────────────────────┐
│  Projection Layer (Neuron)          │
│  + Position Embeddings              │
└─────────────────────────────────────┘
    │
    ▼ (577 x 768)
┌─────────────────────────────────────┐
│  Language Encoder (Neuron)          │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Decoder (Neuron, bucketed)         │
│  Buckets: 1, 4, 8, 16, 32, 64       │
└─────────────────────────────────────┘
    │
    ▼
  Output Text
```

## Dual-Core Deployment

For maximum throughput, run two independent processes:

```bash
# Terminal 1
NEURON_RT_VISIBLE_CORES=0 python inference.py --image img.jpg

# Terminal 2  
NEURON_RT_VISIBLE_CORES=1 python inference.py --image img.jpg
```

This achieves ~8 QPS on inf2.xlarge (2 NeuronCores).

## Files

```
florence2_neuron_bf16/
├── compile.py      # Model compilation script
├── inference.py    # Inference engine
├── benchmark.py    # Performance benchmarking
├── README.md       # This file
└── compiled_bf16/  # Compiled models (after running compile.py)
    ├── stage0.pt
    ├── stage1.pt
    ├── stage2.pt
    ├── stage3.pt
    ├── projection.pt
    ├── encoder.pt
    ├── decoder_1.pt
    ├── decoder_4.pt
    ├── decoder_8.pt
    ├── decoder_16.pt
    ├── decoder_32.pt
    └── decoder_64.pt
```

## License

Apache 2.0
