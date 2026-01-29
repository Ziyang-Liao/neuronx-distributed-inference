"""
Florence-2 Base Inference for AWS Inferentia2

This is the basic version with Vision Encoder + Language Encoder on Neuron,
and Language Decoder on CPU.

## Why Decoder on CPU?

The decoder is autoregressive - it generates one token at a time, and each
step depends on all previous tokens. This creates challenges for Neuron:

1. Input length changes every step (1 → 2 → 3 → ...)
2. Neuron requires static shapes at compile time
3. Compiling a model for every possible length is impractical

This basic version keeps the decoder on CPU for simplicity.
For better performance, use Florence2FullNeuron which uses bucketed decoders.

## Performance

    | Component        | Device | Time   |
    |------------------|--------|--------|
    | Vision Encoder   | Neuron | ~200ms |
    | Language Encoder | Neuron | ~100ms |
    | Language Decoder | CPU    | ~500ms |
    | Total            |        | ~800ms |

    vs CPU baseline: 2-3x speedup

## Usage

    from neuronx_distributed_inference.models.florence2 import Florence2ForConditionalGeneration
    
    model = Florence2ForConditionalGeneration('./compiled_models')
    result = model.generate(image, '<CAPTION>')
"""
import torch
import torch_neuronx
import os
from transformers import AutoProcessor, AutoModelForCausalLM
from PIL import Image

os.environ.setdefault("NEURON_RT_NUM_CORES", "2")

MODEL_NAME = "microsoft/Florence-2-base"
MAX_SEQ = 600


class Florence2ForConditionalGeneration:
    """Florence-2 with Vision + Encoder on Neuron, Decoder on CPU.
    
    This is the basic version. For maximum performance, use Florence2FullNeuron.
    
    Args:
        model_dir: Directory containing compiled .pt files
        mode: "multistage" (recommended) or "unified"
    """
    
    def __init__(self, model_dir: str, mode: str = "multistage"):
        print(f"Loading Florence-2 Neuron ({mode})...")
        
        # Load tokenizer and image processor
        self.processor = AutoProcessor.from_pretrained(MODEL_NAME, trust_remote_code=True)
        
        # Load full model - we'll use some components on CPU
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME, trust_remote_code=True,
            torch_dtype=torch.float32,
            attn_implementation="eager"
        )
        self.model.eval()
        self.mode = mode
        
        # Load Neuron-compiled vision encoder
        if mode == "multistage":
            self.stages = [
                torch.jit.load(f"{model_dir}/stage{i}.pt") 
                for i in range(4)
            ]
        else:
            self.vision = torch.jit.load(f"{model_dir}/vision_unified.pt")
        
        # Load Neuron-compiled language encoder
        self.encoder = torch.jit.load(f"{model_dir}/encoder_{MAX_SEQ}.pt")
        
        # Precompute position embeddings
        self._precompute_pos_embed()
        print("Ready!")
    
    def _precompute_pos_embed(self):
        """Precompute position embeddings for 24x24 feature grid."""
        with torch.no_grad():
            dummy = torch.randn(1, 3, 768, 768)
            vt_out = self.model.vision_tower.forward_features_unpool(dummy)
            self.pos_embed = self.model.image_pos_embed(
                vt_out.view(1, 24, 24, 1024)
            ).view(1, 576, 1024)
    
    def generate(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
        """Generate text from image.
        
        Args:
            image: PIL Image, file path, or URL
            task: Task prompt ("<CAPTION>", "<OD>", "<OCR>", etc.)
            max_new_tokens: Maximum tokens to generate
        
        Returns:
            Generated text string
        """
        # Handle different image input types
        if isinstance(image, str):
            if image.startswith(('http://', 'https://')):
                import requests
                from io import BytesIO
                image = Image.open(BytesIO(requests.get(image).content))
            else:
                image = Image.open(image)
        if hasattr(image, 'convert'):
            image = image.convert("RGB")
        
        inputs = self.processor(text=task, images=image, return_tensors="pt")
        
        with torch.no_grad():
            # ========================================================
            # Vision Encoding (Neuron)
            # ========================================================
            if self.mode == "multistage":
                x = inputs["pixel_values"]
                for stage in self.stages:
                    x = stage(x)
            else:
                x = self.vision(inputs["pixel_values"])
            
            # Add position embeddings and project
            x = x + self.pos_embed
            x = torch.cat([x.mean(1, keepdim=True), x], dim=1)
            img_emb = self.model.image_proj_norm(x @ self.model.image_projection)
            
            # ========================================================
            # Language Encoding (Neuron)
            # ========================================================
            txt_emb = self.model.language_model.model.shared(inputs["input_ids"])
            combined = torch.cat([img_emb, txt_emb], dim=1)
            
            # Pad to MAX_SEQ for static shape
            if combined.shape[1] < MAX_SEQ:
                pad = torch.zeros(1, MAX_SEQ - combined.shape[1], 768)
                combined = torch.cat([combined, pad], dim=1)
            
            enc_out = self.encoder(combined)
            
            # ========================================================
            # Autoregressive Decoding (CPU)
            # ========================================================
            # This is the slow part - runs on CPU because input length
            # changes every step (1 → 2 → 3 → ...)
            dec_ids = torch.tensor([[2]])  # BOS token
            
            for _ in range(max_new_tokens):
                # Embed current tokens
                dec_emb = self.model.language_model.model.shared(dec_ids)
                
                # Run decoder
                dec_out = self.model.language_model.model.decoder(
                    inputs_embeds=dec_emb,
                    encoder_hidden_states=enc_out,
                    encoder_attention_mask=torch.ones(1, enc_out.shape[1]),
                    use_cache=False  # No cache for simplicity
                )
                
                # Get next token
                logits = self.model.language_model.lm_head(dec_out.last_hidden_state)
                next_token = logits[:, -1, :].argmax(-1, keepdim=True)
                dec_ids = torch.cat([dec_ids, next_token], dim=1)
                
                # Stop at EOS
                if next_token.item() == 2:
                    break
        
        return self.processor.tokenizer.decode(dec_ids[0], skip_special_tokens=True)
    
    def __call__(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
        """Alias for generate()."""
        return self.generate(image, task, max_new_tokens)
