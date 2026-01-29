# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] - 2026-01-29

### Added
- FP32 v2: Projection layer compiled to Neuron (`compile_v2.py`, `inference_v2.py`)

### Performance
- FP32 v2 CAPTION latency: 370ms (vs 393ms in v1, 6% improvement)

## [0.2.0] - 2026-01-29

### Added
- BF16 optimized implementation (`models/florence2_bf16/`)
- Projection layer compilation to Neuron (eliminates CPU bottleneck in BF16)
- Comprehensive benchmarking scripts
- Accuracy validation results
- Examples and tests

### Performance
- 45% throughput improvement over FP32
- Single-core: 4.09 QPS (vs 2.82 QPS)
- Dual-core: 8.18 QPS (vs 5.64 QPS)
- Latency: 252ms (vs 393ms)

## [0.1.0] - 2026-01-28

### Added
- Initial FP32 implementation (`models/florence2/`)
- Stage-wise compilation for DaViT vision encoder
- Decoder bucket strategy for variable-length generation
- Support for CAPTION, OD, OCR tasks

### Performance
- 5.2x speedup over CPU baseline
- Single-core: 2.82 QPS
- Latency: 393ms (CAPTION task)
