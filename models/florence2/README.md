# Florence-2 FP32 Implementation

FP32 precision implementation of Florence-2 on AWS Inferentia2.

## Performance

| Metric | Value |
|--------|-------|
| CAPTION Latency | 393ms |
| DETAILED_CAPTION Latency | 426ms |
| OD Latency | 347ms |
| OCR Latency | 333ms |
| Single-Core QPS | 2.82 |
| Dual-Core QPS | 5.64 |
| Speedup vs CPU | 5.2x |

Tested on inf2.8xlarge.

## Technical Background

### The Problem

Florence-2 uses DaViT (Dual Attention Vision Transformer) as its vision encoder. DaViT has two characteristics that prevent direct Neuron compilation:

1. **Dynamic Shape Propagation**
   ```python
   # Original implementation (transformers library)
   def forward_features(self, x):
       for conv, block in zip(self.convs, self.blocks):
           x, size = conv(x, size)  # size is a dynamic tuple
           x, size = block(x, size)
   ```
   The Neuron compiler requires static shapes for optimization.

2. **Unsupported Operations**
   DaViT attention blocks contain `parallel_for` loops that Neuron cannot trace.

### Solution: Stage-wise Compilation

Split DaViT into 4 independent stages, each with fixed input/output shapes:

```python
class Stage(torch.nn.Module):
    def __init__(self, conv, block, in_size, out_size):
        super().__init__()
        self.conv = conv
        self.block = block
        self.in_size = in_size   # Fixed at compile time
        self.out_size = out_size # Fixed at compile time
    
    def forward(self, x):
        x, _ = self.conv(x, self.in_size)
        x, _ = self.block(x, self.out_size)
        return x
```

### Stage Dimensions

| Stage | Input Shape | Output Shape | Channels |
|-------|-------------|--------------|----------|
| 0 | (1, 3, 768, 768) | (1, 36864, 128) | 128 |
| 1 | (1, 36864, 128) | (1, 9216, 256) | 256 |
| 2 | (1, 9216, 256) | (1, 2304, 512) | 512 |
| 3 | (1, 2304, 512) | (1, 576, 1024) | 1024 |

## Files

```
florence2/
├── compile.py                    # Compilation script with detailed comments
├── modeling_florence2.py         # Base inference class
├── modeling_florence2_full.py    # Full Neuron inference (recommended)
├── modeling_florence2_kvcache.py # KV-Cache variant (experimental)
├── __init__.py
└── README.md
```

## Usage

### Compilation

```bash
# Basic compilation (Vision + Encoder on Neuron)
python -m models.florence2.compile --output ./compiled_fp32

# Full compilation (+ Decoder on Neuron, recommended)
python -m models.florence2.compile --output ./compiled_fp32 --with-decoder
```

### Inference

```python
from models.florence2 import Florence2FullNeuron

model = Florence2FullNeuron("./compiled_fp32")
result = model.generate("image.jpg", "<CAPTION>")
print(result)
```

## Inference Classes

| Class | Decoder Location | Speedup | Description |
|-------|------------------|---------|-------------|
| `Florence2ForConditionalGeneration` | CPU | 2.2x | For debugging |
| `Florence2WithKVCache` | CPU + Cache | 2.3x | Memory constrained |
| `Florence2FullNeuron` | Neuron | 5.2x | **Production recommended** |

## Decoder Bucket Strategy

The decoder is autoregressive—input length increases by 1 at each generation step. Since Neuron requires static shapes:

1. Pre-compile decoder models for multiple sequence lengths: 1, 4, 8, 16, 32, 64
2. At runtime, select the smallest bucket >= current length
3. Pad input to the bucket size

```python
# Example: 5 tokens generated so far
# Select bucket 8, pad 3 positions
buckets = [1, 4, 8, 16, 32, 64]
seq_len = 5
bucket = next(b for b in buckets if b >= seq_len)  # = 8
```

## Why Not KV-Cache?

Florence-2 decoder has two attention types:
- Self-attention (requires cache)
- Cross-attention (requires encoder output cache)

Implementing dual-cache on Neuron is complex. Since Florence-2-base is small (6 layers), recomputation overhead is acceptable. The bucket strategy is simpler and provides sufficient performance.

## Optimization Recommendations

1. **Use BF16 version** if precision requirements allow—45% faster
2. **Dual-process deployment** on inf2.xlarge (2 NeuronCores)
3. **Warm-up** the model before serving—first inference loads compiled models

## License

Apache 2.0

## Version 2: Projection on Neuron

### What Changed

v2 compiles the projection layer to Neuron instead of running it on CPU.

| Component | v1 | v2 |
|-----------|----|----|
| Vision Stages | Neuron | Neuron |
| Projection | **CPU** | **Neuron** |
| Encoder | Neuron | Neuron |
| Decoder | Neuron | Neuron |

### Performance Improvement

| Metric | v1 | v2 | Improvement |
|--------|----|----|-------------|
| CAPTION Latency | 393ms | 370ms | **6%** |
| QPS | 2.82 | 2.71 | - |

Note: The improvement is modest because FP32 data transfer between CPU and Neuron is efficient. The bigger win is in BF16 where CPU lacks native support.

### Usage

```bash
# Compile v2
python -m models.florence2.compile_v2 --output ./compiled_fp32_v2

# Inference
python -m models.florence2.inference_v2 --image test.jpg --task "<CAPTION>"

# Benchmark
python -m models.florence2.inference_v2 --image test.jpg --benchmark
```

### Files

```
florence2/
├── compile.py       # v1: Projection on CPU
├── compile_v2.py    # v2: Projection on Neuron (recommended)
├── inference_v2.py  # v2 inference engine
├── modeling_*.py    # Model implementations
└── README.md
```
