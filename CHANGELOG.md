# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-01-29

### Added
- BF16 optimized implementation (`models/florence2_bf16/`)
- Projection layer compilation to Neuron (eliminates CPU bottleneck)
- Comprehensive benchmarking scripts
- Accuracy validation results

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
