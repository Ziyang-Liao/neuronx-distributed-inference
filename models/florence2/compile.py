"""
Florence-2 Model Compilation for AWS Inferentia2

This module compiles Florence-2 model components for Neuron acceleration.

## Why Compilation is Needed

Florence-2 uses DaViT (Dual Attention Vision Transformer) as its vision encoder.
DaViT contains operations that Neuron compiler cannot handle directly:

1. `parallel_for` loops in attention blocks
2. Dynamic shape operations (size tuples passed at runtime)

## Solution: Stage-wise Compilation

We split the vision encoder into 4 stages, each with static shapes:
- Stage 0: 768x768 → 192x192 (128 channels)
- Stage 1: 192x192 → 96x96 (256 channels)  
- Stage 2: 96x96 → 48x48 (512 channels)
- Stage 3: 48x48 → 24x24 (1024 channels)

Each stage wraps conv + block with hardcoded sizes, making them traceable.

## Usage

    # Basic compilation (Vision + Encoder on Neuron)
    python -m neuronx_distributed_inference.models.florence2.compile --output ./models
    
    # Full compilation (+ Decoder on Neuron, fastest)
    python -m neuronx_distributed_inference.models.florence2.compile \\
        --output ./models --with-decoder --max-gen 128
"""
import torch
import torch_neuronx
import os
import argparse
from transformers import AutoModelForCausalLM

MODEL_NAME = "microsoft/Florence-2-base"
IMG_SIZE = 768

# DaViT stage configurations
# These are the fixed sizes for each stage of the vision encoder
# Input sizes: what the conv layer expects
# Output sizes: what the block layer produces
SIZES = [(768, 768), (192, 192), (96, 96), (48, 48)]
OUT_SIZES = [(192, 192), (96, 96), (48, 48), (24, 24)]
OUT_DIMS = [128, 256, 512, 1024]  # Channel dimensions after each stage


def compile_florence2(output_dir: str, mode: str = "multistage", max_seq: int = 600, 
                      with_decoder: bool = False, max_gen: int = 256):
    """Compile Florence-2 for Neuron.
    
    Args:
        output_dir: Directory to save compiled .pt model files
        mode: "multistage" (recommended), "unified", or "all"
              - multistage: 4 separate models, better optimization
              - unified: single model, simpler but slower
        max_seq: Max sequence length for encoder (image tokens + text tokens)
                 577 image tokens + text tokens, 600 is safe default
        with_decoder: Also compile decoder for full Neuron acceleration
        max_gen: Max generation length (for decoder bucket sizes)
    """
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Loading {MODEL_NAME}...")
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, trust_remote_code=True,
        torch_dtype=torch.float32,
        attn_implementation="eager"  # Required: flash attention not supported
    )
    model.eval()
    
    # Compile vision encoder
    if mode in ["multistage", "all"]:
        _compile_multistage(model, output_dir)
    
    if mode in ["unified", "all"]:
        _compile_unified(model, output_dir)
    
    # Compile language encoder
    _compile_encoder(model, output_dir, max_seq)
    
    # Optionally compile decoder
    if with_decoder:
        _compile_decoder(model, output_dir, max_seq, max_gen)
    
    print(f"\n✓ Models saved to {output_dir}/")


def _compile_multistage(model, output_dir):
    """Compile 4-stage vision encoder (recommended).
    
    Why multi-stage?
    - DaViT's forward_features() uses dynamic sizes in a loop
    - We break it into 4 stages, each with static sizes
    - Neuron compiler optimizes smaller models better
    - Result: ~15% faster than unified approach
    
    Each stage wraps:
    - conv: Patch embedding / downsampling
    - block: Transformer blocks with attention
    """
    print("Compiling Multi-Stage Vision Encoder...")
    
    for i in range(4):
        # Create a wrapper that hardcodes the size parameters
        # This is the key trick: replace dynamic sizes with static ones
        class Stage(torch.nn.Module):
            def __init__(self, conv, block, in_size, out_size):
                super().__init__()
                self.conv = conv
                self.block = block
                # Hardcode sizes instead of passing at runtime
                self.in_size = in_size
                self.out_size = out_size
            
            def forward(self, x):
                # Original code: x, size = self.conv(x, size) - dynamic!
                # Our code: sizes are fixed attributes
                x, _ = self.conv(x, self.in_size)
                x, _ = self.block(x, self.out_size)
                return x
        
        stage = Stage(
            model.vision_tower.convs[i],
            model.vision_tower.blocks[i],
            SIZES[i],
            OUT_SIZES[i]
        )
        stage.eval()
        
        # Input shape depends on stage
        # Stage 0: raw image (1, 3, 768, 768)
        # Stage 1-3: flattened features from previous stage
        if i == 0:
            x = torch.randn(1, 3, IMG_SIZE, IMG_SIZE)
        else:
            # DaViT conv/block output is (batch, height*width, channels)
            x = torch.randn(1, OUT_SIZES[i-1][0] * OUT_SIZES[i-1][1], OUT_DIMS[i-1])
        
        print(f"  Stage {i}: {tuple(x.shape)}...")
        traced = torch_neuronx.trace(stage, x)
        traced.save(f"{output_dir}/stage{i}.pt")


def _compile_unified(model, output_dir):
    """Compile unified vision encoder (alternative approach).
    
    Why unified?
    - Simpler: single model file
    - Useful for comparison/debugging
    
    Why slower than multi-stage?
    - Larger model = less aggressive compiler optimization
    - ~15% slower in practice
    """
    print("Compiling Unified Vision Encoder...")
    
    class VisionUnified(torch.nn.Module):
        def __init__(self, vt):
            super().__init__()
            self.convs = vt.convs
            self.blocks = vt.blocks
        
        def forward(self, x):
            # Process all 4 stages in sequence
            for i, (conv, block) in enumerate(zip(self.convs, self.blocks)):
                x, _ = conv(x, SIZES[i])
                x, _ = block(x, OUT_SIZES[i])
            return x  # Output: (1, 576, 1024)
    
    vision = VisionUnified(model.vision_tower)
    vision.eval()
    traced = torch_neuronx.trace(
        vision, 
        torch.randn(1, 3, IMG_SIZE, IMG_SIZE),
        compiler_args=["--model-type=transformer"]
    )
    traced.save(f"{output_dir}/vision_unified.pt")


def _compile_encoder(model, output_dir, max_seq):
    """Compile language encoder.
    
    Why wrap the encoder?
    - Florence-2's encoder expects input_ids by default
    - We pass embeddings directly (image + text combined)
    - Wrapper accepts inputs_embeds and returns hidden states
    
    Why fixed max_seq?
    - Neuron requires static shapes
    - We pad shorter sequences to max_seq
    - 600 covers: 577 image tokens + ~23 text tokens
    """
    print(f"Compiling Language Encoder (max_seq={max_seq})...")
    
    class EncoderWrapper(torch.nn.Module):
        def __init__(self, encoder):
            super().__init__()
            self.encoder = encoder
        
        def forward(self, inputs_embeds):
            # Pass embeddings directly, return hidden states
            return self.encoder(inputs_embeds=inputs_embeds).last_hidden_state
    
    wrapper = EncoderWrapper(model.language_model.model.encoder)
    wrapper.eval()
    traced = torch_neuronx.trace(wrapper, torch.randn(1, max_seq, 768))
    traced.save(f"{output_dir}/encoder_{max_seq}.pt")


def _compile_decoder(model, output_dir, enc_len, max_gen):
    """Compile decoder with bucket strategy.
    
    Why buckets?
    - Decoder is autoregressive: input length grows each step
    - Neuron requires static shapes
    - Solution: compile multiple models for different lengths
    - At runtime, pad to nearest bucket size
    
    Bucket sizes: 1, 4, 8, 16, 32, 64, 128
    - Covers most generation scenarios
    - Padding overhead is minimal within buckets
    
    Why not KV-Cache on Neuron?
    - Florence-2's decoder has complex cache format (self + cross attention)
    - Bucket approach is simpler and still very fast
    """
    print(f"Compiling Decoder (enc_len={enc_len}, max_gen={max_gen})...")
    
    decoder = model.language_model.model.decoder
    lm_head = model.language_model.lm_head
    embed = model.language_model.model.shared
    
    class DecoderOneToken(torch.nn.Module):
        """Decoder that processes all tokens and returns logits.
        
        No KV-Cache: recomputes everything each step.
        Fast on Neuron because the model is small (6 layers).
        """
        def __init__(self, decoder, lm_head, embed):
            super().__init__()
            self.decoder = decoder
            self.lm_head = lm_head
            self.embed = embed
        
        def forward(self, input_ids, encoder_hidden_states):
            """
            Args:
                input_ids: (1, seq_len) all generated tokens so far
                encoder_hidden_states: (1, enc_len, 768) from encoder
            Returns:
                logits: (1, seq_len, vocab_size)
            """
            inputs_embeds = self.embed(input_ids)
            out = self.decoder(
                inputs_embeds=inputs_embeds,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=torch.ones(1, encoder_hidden_states.shape[1]),
                use_cache=False  # No cache, recompute each time
            )
            return self.lm_head(out.last_hidden_state)
    
    decoder_model = DecoderOneToken(decoder, lm_head, embed)
    decoder_model.eval()
    
    # Compile for different sequence lengths (bucket approach)
    buckets = [1, 4, 8, 16, 32, 64, 128]
    buckets = [b for b in buckets if b <= max_gen]
    
    for seq_len in buckets:
        print(f"  Decoder seq_len={seq_len}...")
        input_ids = torch.randint(0, 1000, (1, seq_len))
        enc_hidden = torch.randn(1, enc_len, 768)
        
        try:
            traced = torch_neuronx.trace(
                decoder_model,
                (input_ids, enc_hidden),
                compiler_args=["--model-type=transformer"]
            )
            traced.save(f"{output_dir}/decoder_{seq_len}.pt")
        except Exception as e:
            print(f"    Failed: {e}")
            break
    
    print(f"  Decoder compilation done!")


def main():
    parser = argparse.ArgumentParser(description="Compile Florence-2 for Neuron")
    parser.add_argument("--output", "-o", default="./compiled_models",
                        help="Output directory for compiled models")
    parser.add_argument("--mode", choices=["multistage", "unified", "all"], 
                        default="multistage",
                        help="Vision encoder mode: multistage (recommended), unified, or all")
    parser.add_argument("--max-seq", type=int, default=600,
                        help="Max sequence length for encoder")
    parser.add_argument("--with-decoder", action="store_true",
                        help="Also compile decoder for full Neuron acceleration")
    parser.add_argument("--max-gen", type=int, default=128,
                        help="Max generation length (determines decoder buckets)")
    args = parser.parse_args()
    
    compile_florence2(args.output, args.mode, args.max_seq, 
                      args.with_decoder, args.max_gen)


if __name__ == "__main__":
    main()
