#!/usr/bin/env python3
"""
Florence-2 Neuron Inference (BF16 Optimized)

High-performance inference using pre-compiled Neuron models.
Supports all Florence-2 tasks: CAPTION, DETAILED_CAPTION, OD, OCR, etc.

Usage:
    python inference.py --image path/to/image.jpg --task "<CAPTION>"
    
Performance (inf2.xlarge, single NeuronCore):
    - CAPTION: ~250ms
    - OD: ~240ms  
    - Throughput: ~4 QPS per core, ~8 QPS dual-core
"""

import os
import argparse
import torch
import torch_neuronx
from PIL import Image
from transformers import AutoProcessor, AutoModelForCausalLM

DEFAULT_MODEL_DIR = "./compiled_bf16"
MAX_SEQ = 600
DECODER_BUCKETS = [1, 4, 8, 16, 32, 64]


class Florence2NeuronBF16:
    """
    Florence-2 inference engine using Neuron-compiled BF16 models.
    
    Args:
        model_dir: Directory containing compiled .pt files
        core_id: NeuronCore ID to use (0 or 1 for inf2.xlarge)
    """
    
    def __init__(self, model_dir=DEFAULT_MODEL_DIR, core_id="0"):
        os.environ["NEURON_RT_VISIBLE_CORES"] = core_id
        os.environ["NEURON_RT_NUM_CORES"] = "1"
        
        print(f"Loading Florence-2 Neuron BF16 (NC{core_id})...")
        
        # Load processor and model for embeddings
        self.processor = AutoProcessor.from_pretrained(
            "microsoft/Florence-2-base", trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            "microsoft/Florence-2-base", trust_remote_code=True,
            torch_dtype=torch.bfloat16, attn_implementation="eager")
        self.model.eval()
        
        # Load compiled Neuron models
        self.stages = [torch.jit.load(f"{model_dir}/stage{i}.pt") for i in range(4)]
        self.projection = torch.jit.load(f"{model_dir}/projection.pt")
        self.encoder = torch.jit.load(f"{model_dir}/encoder.pt")
        self.decoders = {b: torch.jit.load(f"{model_dir}/decoder_{b}.pt") 
                        for b in DECODER_BUCKETS}
        
        # Precompute position embeddings
        self._init_pos_embed()
        print("Ready!")
    
    def _init_pos_embed(self):
        """Precompute position embeddings for vision features."""
        with torch.no_grad():
            dummy = torch.randn(1, 3, 768, 768, dtype=torch.bfloat16)
            vt_out = self.model.vision_tower.forward_features_unpool(dummy)
            self.pos_embed = self.model.image_pos_embed(
                vt_out.view(1, 24, 24, 1024)).view(1, 576, 1024)
    
    def __call__(self, image, task="<CAPTION>", max_tokens=100):
        """
        Run inference on an image.
        
        Args:
            image: PIL Image or path to image file
            task: Florence-2 task prompt (e.g., "<CAPTION>", "<OD>", "<OCR>")
            max_tokens: Maximum tokens to generate
            
        Returns:
            Generated text response
        """
        if isinstance(image, str):
            image = Image.open(image).convert("RGB")
        
        inputs = self.processor(text=task, images=image, return_tensors="pt")
        
        with torch.no_grad():
            # Vision encoding (4 stages)
            x = inputs["pixel_values"].to(torch.bfloat16)
            for stage in self.stages:
                x = stage(x)
            
            # Add position embeddings and project
            x = x + self.pos_embed
            img_emb = self.projection(x)
            
            # Combine with text embeddings
            txt_emb = self.model.language_model.model.shared(inputs["input_ids"])
            combined = torch.cat([img_emb, txt_emb], dim=1)
            
            # Pad to fixed length
            pad_len = MAX_SEQ - combined.shape[1]
            if pad_len > 0:
                combined = torch.cat([combined, 
                    torch.zeros(1, pad_len, 768, dtype=torch.bfloat16)], dim=1)
            
            # Encode
            enc_out = self.encoder(combined)
            
            # Autoregressive decoding with bucketing
            dec_ids = torch.tensor([[2]])  # BOS token
            for _ in range(max_tokens):
                seq_len = dec_ids.shape[1]
                bucket = min(b for b in DECODER_BUCKETS if b >= seq_len)
                
                # Pad to bucket size
                if bucket > seq_len:
                    inp = torch.cat([dec_ids, 
                        torch.zeros(1, bucket - seq_len, dtype=torch.long)], dim=1)
                else:
                    inp = dec_ids
                
                logits = self.decoders[bucket](inp, enc_out)
                next_token = logits[:, seq_len - 1, :].argmax(-1, keepdim=True)
                dec_ids = torch.cat([dec_ids, next_token], dim=1)
                
                if next_token.item() == 2:  # EOS token
                    break
        
        return self.processor.tokenizer.decode(dec_ids[0], skip_special_tokens=True)


def main():
    parser = argparse.ArgumentParser(description="Florence-2 Neuron Inference")
    parser.add_argument("--image", required=True, help="Path to image")
    parser.add_argument("--task", default="<CAPTION>", help="Task prompt")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--core", default="0", help="NeuronCore ID")
    parser.add_argument("--max-tokens", type=int, default=100)
    args = parser.parse_args()
    
    model = Florence2NeuronBF16(args.model_dir, args.core)
    result = model(args.image, args.task, args.max_tokens)
    print(f"\nTask: {args.task}")
    print(f"Result: {result}")


if __name__ == "__main__":
    main()
