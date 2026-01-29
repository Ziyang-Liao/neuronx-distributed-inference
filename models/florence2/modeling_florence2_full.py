"""
Florence-2 Full Neuron Inference

This is the recommended version for production use.
All components run on Neuron accelerator for maximum performance.

## Architecture

    Image (768x768)
         │
         ▼
    ┌─────────────────────────────────────┐
    │  Vision Encoder (4 stages)          │  ← Neuron
    │  Stage 0-3: DaViT with static sizes │
    └─────────────────────────────────────┘
         │
         ▼ (1, 576, 1024) features
    ┌─────────────────────────────────────┐
    │  Position Embedding + Projection    │  ← CPU (small ops)
    │  → (1, 577, 768) image embeddings   │
    └─────────────────────────────────────┘
         │
         ▼ concat with text embeddings
    ┌─────────────────────────────────────┐
    │  Language Encoder                   │  ← Neuron
    │  BART encoder, 6 layers             │
    └─────────────────────────────────────┘
         │
         ▼ (1, 600, 768) encoder output
    ┌─────────────────────────────────────┐
    │  Language Decoder (bucketed)        │  ← Neuron
    │  BART decoder, 6 layers             │
    │  Uses bucket models: 1,4,8,16,32,64 │
    └─────────────────────────────────────┘
         │
         ▼
    Generated Text

## Performance

    | Task              | CPU    | Full Neuron | Speedup |
    |-------------------|--------|-------------|---------|
    | CAPTION           | 1930ms | 373ms       | 5.2x    |
    | DETAILED_CAPTION  | 2630ms | 427ms       | 6.2x    |

## Usage

    from neuronx_distributed_inference.models.florence2 import Florence2FullNeuron
    
    model = Florence2FullNeuron('./compiled_models')
    result = model.generate(image, '<CAPTION>')
"""
import torch
import torch_neuronx
import os
from transformers import AutoProcessor, AutoModelForCausalLM
from PIL import Image

# Set Neuron runtime to use 2 cores (sufficient for Florence-2)
os.environ.setdefault("NEURON_RT_NUM_CORES", "2")

MODEL_NAME = "microsoft/Florence-2-base"
MAX_SEQ = 600  # Max encoder sequence length


class Florence2FullNeuron:
    """Florence-2 with all components on Neuron accelerator.
    
    This is the fastest version, achieving 5-6x speedup over CPU.
    
    Components:
    - Vision Encoder: 4 traced stage models
    - Language Encoder: 1 traced encoder model
    - Language Decoder: Multiple traced models for different lengths (buckets)
    
    Args:
        model_dir: Directory containing compiled .pt files
        mode: "multistage" (recommended) or "unified"
    
    Example:
        >>> model = Florence2FullNeuron('./compiled_models')
        >>> result = model.generate(image, '<CAPTION>')
        >>> print(result)
        'a green car parked in front of a yellow building'
    """
    
    def __init__(self, model_dir: str, mode: str = "multistage"):
        print(f"Loading Florence-2 Full Neuron ({mode})...")
        
        # Load processor for tokenization and image preprocessing
        self.processor = AutoProcessor.from_pretrained(MODEL_NAME, trust_remote_code=True)
        
        # Load base model for components not on Neuron
        # (position embeddings, projection layers, embedding lookup)
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME, trust_remote_code=True,
            torch_dtype=torch.float32,
            attn_implementation="eager"
        )
        self.model.eval()
        self.mode = mode
        
        # ============================================================
        # Load Vision Encoder (Neuron)
        # ============================================================
        # Multi-stage: 4 separate models, each handles one DaViT stage
        # This is faster because Neuron optimizes smaller models better
        if mode == "multistage":
            self.stages = [
                torch.jit.load(f"{model_dir}/stage{i}.pt") 
                for i in range(4)
            ]
        else:
            # Unified: single model for entire vision encoder
            self.vision = torch.jit.load(f"{model_dir}/vision_unified.pt")
        
        # ============================================================
        # Load Language Encoder (Neuron)
        # ============================================================
        self.encoder = torch.jit.load(f"{model_dir}/encoder_{MAX_SEQ}.pt")
        
        # ============================================================
        # Load Decoder Buckets (Neuron)
        # ============================================================
        # Why buckets? Neuron requires static shapes, but decoder input
        # length grows during generation. We compile models for different
        # lengths and pad to the nearest bucket at runtime.
        self.decoder_buckets = {}
        for bucket in [1, 4, 8, 16, 32, 64, 128, 256]:
            path = f"{model_dir}/decoder_{bucket}.pt"
            if os.path.exists(path):
                self.decoder_buckets[bucket] = torch.jit.load(path)
        
        self.bucket_sizes = sorted(self.decoder_buckets.keys())
        print(f"  Decoder buckets: {self.bucket_sizes}")
        
        # ============================================================
        # CPU Fallback Components
        # ============================================================
        # Used when sequence length exceeds largest bucket
        self.cpu_decoder = self.model.language_model.model.decoder
        self.lm_head = self.model.language_model.lm_head
        self.embed = self.model.language_model.model.shared
        
        # Precompute position embeddings (constant for 768x768 images)
        self._precompute_pos_embed()
        print("Ready!")
    
    def _precompute_pos_embed(self):
        """Precompute position embeddings for the 24x24 feature grid.
        
        Why precompute?
        - Position embeddings are constant for fixed image size (768x768)
        - Computing once saves ~10ms per inference
        - Output grid is always 24x24 = 576 positions
        """
        with torch.no_grad():
            # Run dummy image through vision tower to get output shape
            dummy = torch.randn(1, 3, 768, 768)
            vt_out = self.model.vision_tower.forward_features_unpool(dummy)
            # Compute position embeddings for 24x24 grid
            self.pos_embed = self.model.image_pos_embed(
                vt_out.view(1, 24, 24, 1024)
            ).view(1, 576, 1024)
    
    def _get_bucket(self, seq_len):
        """Find smallest bucket that fits the sequence length.
        
        Example: seq_len=5 → bucket=8
                 seq_len=17 → bucket=32
        """
        for b in self.bucket_sizes:
            if b >= seq_len:
                return b
        return None  # Exceeds all buckets, use CPU fallback
    
    def _decode_neuron(self, input_ids, enc_out):
        """Decode using Neuron with bucket padding.
        
        Args:
            input_ids: (1, seq_len) tokens generated so far
            enc_out: (1, enc_len, 768) encoder hidden states
        
        Returns:
            logits for the last token position
        """
        seq_len = input_ids.shape[1]
        bucket = self._get_bucket(seq_len)
        
        if bucket is None:
            # Sequence too long, fall back to CPU
            return self._decode_cpu(input_ids, enc_out)
        
        # Pad input to bucket size
        # Example: seq_len=5, bucket=8 → pad 3 zeros
        if seq_len < bucket:
            pad = torch.zeros(1, bucket - seq_len, dtype=torch.long)
            input_ids_padded = torch.cat([input_ids, pad], dim=1)
        else:
            input_ids_padded = input_ids
        
        # Run Neuron decoder
        logits = self.decoder_buckets[bucket](input_ids_padded, enc_out)
        
        # Return logits for the last REAL token (not padding)
        return logits[:, seq_len - 1, :]
    
    def _decode_cpu(self, input_ids, enc_out):
        """Fallback CPU decode for sequences exceeding bucket sizes."""
        inputs_embeds = self.embed(input_ids)
        out = self.cpu_decoder(
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=enc_out,
            encoder_attention_mask=torch.ones(1, enc_out.shape[1]),
            use_cache=False
        )
        return self.lm_head(out.last_hidden_state)[:, -1, :]
    
    def generate(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
        """Generate text from image.
        
        Args:
            image: PIL Image, file path, or URL
            task: Task prompt. Options:
                - "<CAPTION>": Short description
                - "<DETAILED_CAPTION>": Detailed description
                - "<MORE_DETAILED_CAPTION>": Very detailed
                - "<OD>": Object detection
                - "<OCR>": Text extraction
            max_new_tokens: Maximum tokens to generate
        
        Returns:
            Generated text string
        """
        # ============================================================
        # 1. Preprocess Image
        # ============================================================
        if isinstance(image, str):
            if image.startswith(('http://', 'https://')):
                import requests
                from io import BytesIO
                image = Image.open(BytesIO(requests.get(image).content))
            else:
                image = Image.open(image)
        if hasattr(image, 'convert'):
            image = image.convert("RGB")
        
        # Processor handles: resize to 768x768, normalize, tokenize task
        inputs = self.processor(text=task, images=image, return_tensors="pt")
        
        with torch.no_grad():
            # ========================================================
            # 2. Vision Encoding (Neuron)
            # ========================================================
            # Process image through 4 DaViT stages
            if self.mode == "multistage":
                x = inputs["pixel_values"]
                for stage in self.stages:
                    x = stage(x)
                # Output: (1, 576, 1024) - 24x24 grid, 1024 channels
            else:
                x = self.vision(inputs["pixel_values"])
            
            # ========================================================
            # 3. Add Position Embeddings + Projection (CPU)
            # ========================================================
            # Add precomputed position embeddings
            x = x + self.pos_embed
            
            # Add CLS token (mean of all spatial positions)
            # This gives the model a global image representation
            x = torch.cat([x.mean(1, keepdim=True), x], dim=1)  # (1, 577, 1024)
            
            # Project to language model dimension (1024 → 768)
            img_emb = self.model.image_proj_norm(
                x @ self.model.image_projection
            )  # (1, 577, 768)
            
            # ========================================================
            # 4. Language Encoding (Neuron)
            # ========================================================
            # Get text embeddings for task prompt
            txt_emb = self.embed(inputs["input_ids"])
            
            # Concatenate image + text embeddings
            combined = torch.cat([img_emb, txt_emb], dim=1)
            
            # Pad to MAX_SEQ for traced encoder (requires static shape)
            if combined.shape[1] < MAX_SEQ:
                pad = torch.zeros(1, MAX_SEQ - combined.shape[1], 768)
                combined = torch.cat([combined, pad], dim=1)
            
            # Run encoder on Neuron
            enc_out = self.encoder(combined)
            
            # ========================================================
            # 5. Autoregressive Decoding (Neuron with buckets)
            # ========================================================
            # Start with BOS token (id=2 for BART)
            dec_ids = torch.tensor([[2]])
            
            for _ in range(max_new_tokens):
                # Get next token logits from Neuron decoder
                logits = self._decode_neuron(dec_ids, enc_out)
                
                # Greedy decoding: pick highest probability token
                next_token = logits.argmax(-1, keepdim=True)
                
                # Append to sequence
                dec_ids = torch.cat([dec_ids, next_token], dim=1)
                
                # Stop if EOS token (id=2 for BART)
                if next_token.item() == 2:
                    break
        
        # Decode token IDs to text
        return self.processor.tokenizer.decode(dec_ids[0], skip_special_tokens=True)
    
    def __call__(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
        """Alias for generate() for convenience."""
        return self.generate(image, task, max_new_tokens)
