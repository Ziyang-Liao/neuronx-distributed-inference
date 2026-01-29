"""
Florence-2 for AWS Inferentia2

This package provides optimized Florence-2 inference on AWS Inferentia2.

## Problem Solved

Florence-2 uses DaViT (Dual Attention Vision Transformer) which contains:
- `parallel_for` loops not supported by Neuron compiler
- Dynamic shape operations incompatible with static tracing

## Solution

Split DaViT into 4 stages with static shapes, compile each separately.
Use bucketed decoder models for different generation lengths.

## Available Classes

- Florence2ForConditionalGeneration: Basic version, decoder on CPU (2x speedup)
- Florence2WithKVCache: With KV-Cache optimization (2.3x speedup)
- Florence2FullNeuron: All on Neuron, fastest (5x speedup) ← RECOMMENDED

## Usage

    # Compile models first
    python -m neuronx_distributed_inference.models.florence2.compile \\
        --output ./models --with-decoder
    
    # Use the fastest version
    from neuronx_distributed_inference.models.florence2 import Florence2FullNeuron
    
    model = Florence2FullNeuron('./models')
    result = model.generate(image, '<CAPTION>')
"""

from .modeling_florence2 import Florence2ForConditionalGeneration
from .modeling_florence2_kvcache import Florence2WithKVCache
from .modeling_florence2_full import Florence2FullNeuron
from .compile import compile_florence2

__all__ = [
    # Inference classes (ordered by performance)
    "Florence2FullNeuron",                 # Best: 5x speedup, all on Neuron
    "Florence2WithKVCache",                # Good: 2.3x speedup, KV-Cache
    "Florence2ForConditionalGeneration",   # Basic: 2x speedup, decoder on CPU
    
    # Compilation
    "compile_florence2",
]
