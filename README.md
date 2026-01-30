# Florence-2 on AWS Inferentia2

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![AWS Inferentia2](https://img.shields.io/badge/AWS-Inferentia2-orange.svg)](https://aws.amazon.com/machine-learning/inferentia/)
[![Neuron SDK](https://img.shields.io/badge/Neuron%20SDK-2.x-green.svg)](https://awsdocs-neuron.readthedocs-hosted.com/)

High-performance deployment of Microsoft Florence-2 vision-language model on AWS Inferentia2.

## Performance

| Version | Single-Core QPS | Dual-Core QPS | Latency (CAPTION) |
|---------|-----------------|---------------|-------------------|
| CPU Baseline | 0.52 | - | 1930ms |
| FP32 Neuron | 2.82 | 5.64 | 393ms |
| **BF16 Neuron** | **4.09** | **8.18** | **252ms** |

**BF16 achieves 45% improvement over FP32, and 15.7x speedup over CPU.**

## Motivation

### Background

Florence-2 is a powerful vision-language model from Microsoft, supporting image captioning, object detection, OCR, and more. However, CPU inference is slow (~2s per image), making it impractical for production workloads.

### Technical Challenges

1. **DaViT Architecture Incompatibility**
   - Florence-2 uses DaViT (Dual Attention Vision Transformer) as its vision encoder
   - DaViT uses `parallel_for` loops and dynamic shapes internally, which the Neuron compiler cannot trace directly

2. **Dynamic Sequence Length**
   - The decoder is autoregressive, with input length increasing at each step
   - Neuron requires static shapes for compilation

3. **Precision vs Performance Trade-off**
   - FP32 provides high precision but slower inference
   - Need optimization without sacrificing accuracy

### Solutions

1. **Stage-wise Compilation**
   - Split DaViT into 4 independent stages, each with fixed input/output shapes
   - Bypasses dynamic shape limitations, enabling Neuron compiler optimization

2. **Bucket Strategy**
   - Pre-compile multiple decoder models for different sequence lengths (1, 4, 8, 16, 32, 64)
   - At runtime, select the smallest bucket >= current length and pad accordingly

3. **BF16 Optimization**
   - Load model with `torch_dtype=torch.bfloat16`
   - Reduces memory bandwidth by 50%, with hardware acceleration on NeuronCores
   - No observable accuracy degradation

## Project Structure

```
├── models/
│   ├── florence2/          # FP32 implementation
│   └── florence2_bf16/     # BF16 optimized (recommended for production)
├── README.md
└── requirements.txt
```

## Quick Start

### Requirements

- AWS Inferentia2 instance (inf2.xlarge or larger)
- Python 3.8+
- Neuron SDK 2.x

```bash
pip install torch-neuronx neuronx-cc transformers einops timm pillow
```

### Compile Models

```bash
# FP32 version
python -m models.florence2.compile --output ./compiled_fp32 --with-decoder

# BF16 version (recommended)
python -m models.florence2_bf16.compile --output-dir ./compiled_bf16
```

### Run Inference

```python
# BF16 version
from models.florence2_bf16.inference import Florence2NeuronBF16

model = Florence2NeuronBF16("./compiled_bf16", core_id="0")

# Image captioning
result = model("image.jpg", "<CAPTION>")
print(result)

# Object detection
result = model("image.jpg", "<OD>")

# OCR
result = model("image.jpg", "<OCR>")
```

### Maximize Throughput

inf2.xlarge has 2 NeuronCores. Run two independent processes to achieve 8+ QPS:

```bash
# Terminal 1
NEURON_RT_VISIBLE_CORES=0 python -m models.florence2_bf16.inference --image img.jpg

# Terminal 2
NEURON_RT_VISIBLE_CORES=1 python -m models.florence2_bf16.inference --image img.jpg
```

## Supported Tasks

| Task | Prompt | Description |
|------|--------|-------------|
| Caption | `<CAPTION>` | Brief image description |
| Detailed Caption | `<DETAILED_CAPTION>` | Comprehensive description |
| Object Detection | `<OD>` | Detect objects with bounding boxes |
| OCR | `<OCR>` | Extract text from image |
| Region Caption | `<REGION_CAPTION>` | Describe specific region |

## Version Comparison

| Version | Precision | Throughput | Use Case |
|---------|-----------|------------|----------|
| `models/florence2/` | FP32 | 2.82 QPS | Maximum precision required |
| `models/florence2_bf16/` | BF16 | 4.09 QPS | **Recommended for production** |

## Architecture

```
Image (768x768)
    │
    ▼
┌─────────────────────────────────────┐
│  Vision Encoder (DaViT, 4 stages)   │
│  stage0 → stage1 → stage2 → stage3  │
│  768→192  192→96   96→48   48→24    │
└─────────────────────────────────────┘
    │
    ▼ (576 tokens × 1024 dim)
┌─────────────────────────────────────┐
│  Projection Layer                   │
│  1024 → 768 + Position Embeddings   │
└─────────────────────────────────────┘
    │
    ▼ (577 tokens × 768 dim)
┌─────────────────────────────────────┐
│  Language Encoder (6 layers)        │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Decoder (6 layers, bucketed)       │
│  Buckets: 1, 4, 8, 16, 32, 64       │
└─────────────────────────────────────┘
    │
    ▼
  Output Text
```

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## Benchmarking Methodology

### Hardware Configuration
- Instance: inf2.8xlarge (2 NeuronCores, 32GB device memory)
- vCPU: 32 cores
- System Memory: 128GB
- Neuron SDK: 2.x
- PyTorch: 2.1+

### Test Protocol
1. **Warm-up**: 10 inference runs discarded
2. **Measurement**: 100 runs for latency, 5 minutes for throughput
3. **Input**: 768×768 RGB images (resized if necessary)
4. **Metrics**: P50 latency reported, QPS = total_requests / elapsed_time

### Reproducibility
```bash
# Single-core latency test
python -m models.florence2_bf16.benchmark --image test.jpg --warmup 10 --runs 100

# Dual-core throughput test (5 minutes)
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 0 &
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 1 &
wait
```

## Limitations

- **Input Size**: Fixed at 768×768 pixels (images are resized automatically)
- **Max Generation Length**: 64 tokens (configurable via decoder buckets)
- **Batch Size**: 1 (batching provides no benefit for this model size)
- **Inferentia1**: Not supported (requires Neuron SDK 2.x features)
- **Dynamic Tasks**: Region-based tasks require bbox coordinates in prompt

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| `RuntimeError: No Neuron devices` | Missing driver | `sudo apt install aws-neuronx-dkms` |
| Compilation OOM | Insufficient memory | Use inf2.8xlarge or larger for compilation |
| Slow first inference | Model loading | Add warm-up in initialization |
| Lower than expected QPS | Single process | Run dual processes on separate NeuronCores |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Citation

```bibtex
@software{florence2_neuron,
  title = {Florence-2 on AWS Inferentia2},
  author = {Ziyang Liao},
  year = {2026},
  url = {https://github.com/Ziyang-Liao/neuronx-distributed-inference}
}
```
