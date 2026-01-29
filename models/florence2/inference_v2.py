#!/usr/bin/env python3
"""
Florence-2 FP32 Inference v2 - With Projection on Neuron

Performance (inf2.xlarge, single NeuronCore):
    - CAPTION: ~370ms (vs 393ms in v1, 6% improvement)
    - QPS: ~2.7 per core

Usage:
    python -m models.florence2.inference_v2 --image path/to/image.jpg
"""
import os
import argparse
import time
import torch
import torch_neuronx
from PIL import Image
from transformers import AutoProcessor, AutoModelForCausalLM

DEFAULT_MODEL_DIR = "./compiled_fp32_v2"
MAX_SEQ = 600
DECODER_BUCKETS = [1, 4, 8, 16, 32, 64]


class Florence2NeuronFP32v2:
    """
    Florence-2 FP32 inference with Projection on Neuron.
    
    v2 improvement: Projection layer runs on Neuron instead of CPU,
    reducing data transfer overhead and improving latency by ~6%.
    """
    
    def __init__(self, model_dir=DEFAULT_MODEL_DIR, core_id="0"):
        os.environ["NEURON_RT_VISIBLE_CORES"] = core_id
        os.environ["NEURON_RT_NUM_CORES"] = "1"
        
        print(f"Loading Florence-2 Neuron FP32 v2 (NC{core_id})...")
        
        # Load processor and model for embeddings
        self.processor = AutoProcessor.from_pretrained(
            "microsoft/Florence-2-base", trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            "microsoft/Florence-2-base", trust_remote_code=True,
            torch_dtype=torch.float32, attn_implementation="eager")
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
            dummy = torch.randn(1, 3, 768, 768)
            vt_out = self.model.vision_tower.forward_features_unpool(dummy)
            self.pos_embed = self.model.image_pos_embed(
                vt_out.view(1, 24, 24, 1024)).view(1, 576, 1024)
    
    def __call__(self, image, task="<CAPTION>", max_tokens=100):
        """
        Run inference on an image.
        
        Args:
            image: PIL Image or path to image file
            task: Task prompt ("<CAPTION>", "<OD>", "<OCR>", etc.)
            max_tokens: Maximum tokens to generate
            
        Returns:
            Generated text response
        """
        if isinstance(image, str):
            image = Image.open(image).convert("RGB")
        
        inputs = self.processor(text=task, images=image, return_tensors="pt")
        
        with torch.no_grad():
            # Vision encoding (4 stages on Neuron)
            x = inputs["pixel_values"]
            for stage in self.stages:
                x = stage(x)
            
            # Add position embeddings and project (on Neuron)
            x = x + self.pos_embed
            img_emb = self.projection(x)
            
            # Combine with text embeddings
            txt_emb = self.model.language_model.model.shared(inputs["input_ids"])
            combined = torch.cat([img_emb, txt_emb], dim=1)
            
            # Pad to fixed length
            pad_len = MAX_SEQ - combined.shape[1]
            if pad_len > 0:
                combined = torch.cat([combined, 
                    torch.zeros(1, pad_len, 768)], dim=1)
            
            # Encode (on Neuron)
            enc_out = self.encoder(combined)
            
            # Autoregressive decoding with bucketing (on Neuron)
            dec_ids = torch.tensor([[2]])  # BOS token
            for _ in range(max_tokens):
                seq_len = dec_ids.shape[1]
                bucket = min(b for b in DECODER_BUCKETS if b >= seq_len)
                
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
    parser = argparse.ArgumentParser(description="Florence-2 FP32 v2 Inference")
    parser.add_argument("--image", required=True, help="Path to image")
    parser.add_argument("--task", default="<CAPTION>", help="Task prompt")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--core", default="0", help="NeuronCore ID")
    parser.add_argument("--benchmark", action="store_true", help="Run benchmark")
    args = parser.parse_args()
    
    model = Florence2NeuronFP32v2(args.model_dir, args.core)
    
    if args.benchmark:
        from PIL import Image
        image = Image.open(args.image).convert("RGB")
        
        # Warmup
        print("\nWarming up...")
        for _ in range(5):
            model(image, "<CAPTION>")
        
        # Benchmark
        print("\nBenchmark: 20 runs")
        times = []
        for i in range(20):
            t0 = time.time()
            model(image, "<CAPTION>")
            times.append((time.time() - t0) * 1000)
        
        print(f"Average: {sum(times)/len(times):.0f}ms")
        print(f"QPS: {1000/(sum(times)/len(times)):.2f}")
    else:
        result = model(args.image, args.task)
        print(f"\nTask: {args.task}")
        print(f"Result: {result}")


if __name__ == "__main__":
    main()
