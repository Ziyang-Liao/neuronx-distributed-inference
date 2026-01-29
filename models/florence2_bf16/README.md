# Florence-2 BF16 Optimized Implementation

BF16 precision implementation of Florence-2 on AWS Inferentia2, achieving 45% throughput improvement over FP32.

## Performance

| Metric | FP32 | BF16 | Improvement |
|--------|------|------|-------------|
| CAPTION Latency | 393ms | 252ms | **36%** |
| DETAILED_CAPTION Latency | 426ms | 280ms | **34%** |
| OD Latency | 347ms | 237ms | **32%** |
| OCR Latency | 333ms | 231ms | **31%** |
| Single-Core QPS | 2.82 | 4.09 | **45%** |
| Dual-Core QPS | 5.64 | 8.18 | **45%** |

Tested on inf2.8xlarge with 5-minute stress test.

## Motivation

### Background

The FP32 implementation already achieves 5x speedup over CPU. This version explores further optimization opportunities.

### Approaches Evaluated

1. **Increased Batch Size** ❌
   - Tested bs=4 batch inference
   - Result: Total latency increased, per-image latency unchanged
   - Analysis: Florence-2-base has only 0.23B parameters; the model is too small to benefit from batching—memory bandwidth overhead negates parallelism gains

2. **Neuron Compiler O3 Optimization** ❌
   - Tested `--optlevel 3` with `--auto-cast all`
   - Result: Compilation failed with internal error "TritiumFusion: Should be able to fuse two loops!"
   - Analysis: Certain DaViT operations are incompatible with aggressive O3 optimizations

3. **BF16 Precision** ✅
   - Load model with `torch_dtype=torch.bfloat16`
   - Result: 30-36% latency reduction, 45% throughput improvement
   - No observable accuracy degradation

### Why BF16 Works

1. **Reduced Memory Bandwidth**: BF16 is 16-bit vs FP32 32-bit—halves data transfer
2. **Native Hardware Support**: Inferentia2 NeuronCores have BF16 hardware acceleration
3. **Smaller Model Size**: Compiled models reduced from ~3GB to ~1.5GB

### Why BF16 Over FP16?

- BF16 (Brain Float 16): 1 sign + 8 exponent + 7 mantissa bits
- FP16 (Half Float): 1 sign + 5 exponent + 10 mantissa bits

BF16 has the same exponent range as FP32 (8 bits), preventing overflow on large values. FP16 has limited exponent range, risking NaN on large activations.

## Key Discovery: Projection Layer Bottleneck

During optimization, a hidden performance bottleneck was identified:

### The Problem

Vision encoder outputs BF16 tensors, but the projection layer running on CPU performs:
1. BF16 → FP32 conversion
2. CPU computation
3. FP32 → BF16 conversion

This conversion overhead was **789ms**—larger than the entire vision encoder!

### The Solution

Compile the projection layer to Neuron:

```python
class Projection(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.proj = model.image_proj_norm  # Linear + LayerNorm
        self.pos_embed = model.image_pos_embed
    
    def forward(self, x):
        x = self.proj(x)
        x = x + self.pos_embed
        return x

# Compile to Neuron
traced = torch_neuronx.trace(Projection(model), 
    torch.randn(1, 576, 1024, dtype=torch.bfloat16))
```

Result: Projection latency reduced from 789ms to ~5ms.

## Files

```
florence2_bf16/
├── compile.py    # Compilation script (includes projection layer)
├── inference.py  # Inference engine
├── benchmark.py  # Performance benchmarking
└── README.md
```

## Usage

### Compilation

```bash
python -m models.florence2_bf16.compile --output-dir ./compiled_bf16
```

Compilation takes ~10 minutes, producing 12 model files totaling ~1.5GB.

### Inference

```python
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

### Stress Test

```bash
# Single-core benchmark
python -m models.florence2_bf16.benchmark --image test.jpg --duration 60

# Dual-core benchmark (maximum throughput)
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 0 &
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 1 &
```

## Comparison with FP32

| Aspect | FP32 | BF16 |
|--------|------|------|
| Precision | 32-bit float | 16-bit float |
| Model Size | ~3GB | ~1.5GB |
| Latency | 393ms | 252ms |
| Throughput | 2.82 QPS | 4.09 QPS |
| Projection Layer | CPU | Neuron |
| Accuracy Loss | Baseline | None observed |

## Best Practices

1. **Use BF16 for production deployments**
2. **Dual-process deployment** to fully utilize both NeuronCores
3. **Warm-up** on startup—first inference loads compiled models
4. **Monitor** with `neuron-top` to verify NeuronCore utilization

## License

Apache 2.0
