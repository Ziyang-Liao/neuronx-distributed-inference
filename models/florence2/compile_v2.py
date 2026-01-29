#!/usr/bin/env python3
"""
Florence-2 FP32 Compilation v2 - With Projection on Neuron

Improvement over v1: Compiles projection layer to Neuron instead of CPU.
This reduces latency by ~20ms (393ms -> 370ms for CAPTION task).

Usage:
    python -m models.florence2.compile_v2 --output ./compiled_fp32_v2
"""
import os
import argparse
import torch
import torch_neuronx
from transformers import AutoModelForCausalLM

MODEL_NAME = "microsoft/Florence-2-base"
STAGE_SIZES = [((768,768),(192,192)), ((192,192),(96,96)), ((96,96),(48,48)), ((48,48),(24,24))]
STAGE_SHAPES = [(1,3,768,768), (1,36864,128), (1,9216,256), (1,2304,512)]
DECODER_BUCKETS = [1, 4, 8, 16, 32, 64]
MAX_SEQ = 600


class VisionStage(torch.nn.Module):
    """Vision encoder stage: conv + transformer block with fixed shapes."""
    def __init__(self, conv, block, in_size, out_size):
        super().__init__()
        self.conv, self.block = conv, block
        self.in_size, self.out_size = in_size, out_size
    
    def forward(self, x):
        x, _ = self.conv(x, self.in_size)
        x, _ = self.block(x, self.out_size)
        return x


class Projection(torch.nn.Module):
    """
    Projects vision features to language model dimension.
    
    v2 improvement: This layer is now compiled to Neuron instead of running on CPU.
    Reduces latency by avoiding CPU-Neuron data transfer overhead.
    """
    def __init__(self, model):
        super().__init__()
        self.proj = model.image_projection
        self.norm = model.image_proj_norm
    
    def forward(self, x):
        # Add CLS token (mean pooling of all patches)
        x = torch.cat([x.mean(1, keepdim=True), x], dim=1)
        # Project and normalize: (1, 577, 1024) -> (1, 577, 768)
        return self.norm(x @ self.proj)


class Encoder(torch.nn.Module):
    """Language encoder wrapper."""
    def __init__(self, model):
        super().__init__()
        self.enc = model.language_model.model.encoder
    
    def forward(self, x):
        return self.enc(inputs_embeds=x).last_hidden_state


class Decoder(torch.nn.Module):
    """Decoder with LM head for autoregressive generation."""
    def __init__(self, model):
        super().__init__()
        self.dec = model.language_model.model.decoder
        self.head = model.language_model.lm_head
    
    def forward(self, input_ids, encoder_hidden_states):
        emb = self.dec.embed_tokens(input_ids)
        out = self.dec(
            inputs_embeds=emb,
            encoder_hidden_states=encoder_hidden_states
        ).last_hidden_state
        return self.head(out)


def compile_florence2_v2(output_dir: str):
    """Compile Florence-2 FP32 with Projection on Neuron."""
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Loading {MODEL_NAME} (FP32)...")
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, trust_remote_code=True,
        torch_dtype=torch.float32, attn_implementation="eager")
    model.eval()
    vt = model.vision_tower
    
    # Compile vision stages
    print("\n=== Vision Stages ===")
    for i, ((in_s, out_s), shape) in enumerate(zip(STAGE_SIZES, STAGE_SHAPES)):
        print(f"Compiling stage{i}...")
        stage = VisionStage(vt.convs[i], vt.blocks[i], in_s, out_s)
        traced = torch_neuronx.trace(stage.eval(), torch.randn(*shape))
        traced.save(f"{output_dir}/stage{i}.pt")
    
    # Compile projection (NEW in v2)
    print("\n=== Projection (v2: on Neuron) ===")
    traced = torch_neuronx.trace(
        Projection(model).eval(), 
        torch.randn(1, 576, 1024))
    traced.save(f"{output_dir}/projection.pt")
    
    # Compile encoder
    print("\n=== Encoder ===")
    traced = torch_neuronx.trace(
        Encoder(model).eval(), 
        torch.randn(1, MAX_SEQ, 768))
    traced.save(f"{output_dir}/encoder.pt")
    
    # Compile decoder buckets
    print("\n=== Decoder Buckets ===")
    dec = Decoder(model).eval()
    enc_out = torch.randn(1, MAX_SEQ, 768)
    for b in DECODER_BUCKETS:
        print(f"Compiling decoder_{b}...")
        traced = torch_neuronx.trace(
            dec, 
            (torch.zeros(1, b, dtype=torch.long), enc_out))
        traced.save(f"{output_dir}/decoder_{b}.pt")
    
    print(f"\n✓ Models saved to {output_dir}/")
    print(f"  - stage0-3.pt (vision encoder)")
    print(f"  - projection.pt (v2: on Neuron)")
    print(f"  - encoder.pt")
    print(f"  - decoder_{{1,4,8,16,32,64}}.pt")


def main():
    parser = argparse.ArgumentParser(description="Compile Florence-2 FP32 v2")
    parser.add_argument("--output", "-o", default="./compiled_fp32_v2",
                        help="Output directory for compiled models")
    args = parser.parse_args()
    compile_florence2_v2(args.output)


if __name__ == "__main__":
    main()
