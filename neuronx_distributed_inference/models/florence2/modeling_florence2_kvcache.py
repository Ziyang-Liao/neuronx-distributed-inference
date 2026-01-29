"""
Florence-2 with KV-Cache optimization for faster decoding.

This module provides KV-Cache enabled decoder for significantly faster
text generation, especially for long outputs.
"""
import torch
import torch_neuronx
import os
from transformers import AutoProcessor, AutoModelForCausalLM
from PIL import Image

os.environ.setdefault("NEURON_RT_NUM_CORES", "2")

MODEL_NAME = "microsoft/Florence-2-base"
MAX_SEQ = 600
MAX_GEN = 256  # Max generation length


class DecoderWithKVCache(torch.nn.Module):
    """Decoder wrapper that outputs KV-Cache for next step."""
    
    def __init__(self, decoder, lm_head, embed):
        super().__init__()
        self.decoder = decoder
        self.lm_head = lm_head
        self.embed = embed
        self.num_layers = len(decoder.layers)
        self.num_heads = decoder.layers[0].self_attn.num_heads
        self.head_dim = decoder.layers[0].self_attn.head_dim
    
    def forward(self, input_ids, encoder_hidden_states, encoder_attention_mask,
                past_key_values, cache_position):
        """
        Args:
            input_ids: (1, 1) - current token
            encoder_hidden_states: (1, enc_len, 768)
            encoder_attention_mask: (1, enc_len)
            past_key_values: tuple of (key, value) for each layer
            cache_position: scalar - current position in cache
        """
        inputs_embeds = self.embed(input_ids)
        
        # Build causal mask
        seq_len = cache_position + 1
        causal_mask = torch.triu(torch.ones(seq_len, seq_len) * float('-inf'), diagonal=1)
        causal_mask = causal_mask[-1:, :]  # Only last position
        
        hidden_states = inputs_embeds
        new_key_values = []
        
        for i, layer in enumerate(self.decoder.layers):
            past_kv = (past_key_values[i * 2], past_key_values[i * 2 + 1])
            
            # Self attention with cache
            hidden_states, new_kv = self._layer_forward_with_cache(
                layer, hidden_states, causal_mask,
                encoder_hidden_states, encoder_attention_mask,
                past_kv, cache_position
            )
            new_key_values.extend(new_kv)
        
        hidden_states = self.decoder.layer_norm(hidden_states)
        logits = self.lm_head(hidden_states)
        
        return logits, tuple(new_key_values)
    
    def _layer_forward_with_cache(self, layer, hidden_states, causal_mask,
                                   encoder_hidden_states, encoder_attention_mask,
                                   past_kv, cache_position):
        # Self attention
        residual = hidden_states
        hidden_states = layer.self_attn_layer_norm(hidden_states)
        
        # Compute Q, K, V
        q = layer.self_attn.q_proj(hidden_states)
        k = layer.self_attn.k_proj(hidden_states)
        v = layer.self_attn.v_proj(hidden_states)
        
        # Update cache
        past_k, past_v = past_kv
        # Scatter new k, v into cache at cache_position
        new_k = past_k.clone()
        new_v = past_v.clone()
        new_k[:, :, cache_position:cache_position+1, :] = k.view(1, self.num_heads, 1, self.head_dim)
        new_v[:, :, cache_position:cache_position+1, :] = v.view(1, self.num_heads, 1, self.head_dim)
        
        # Attention with full cache
        q = q.view(1, 1, self.num_heads, self.head_dim).transpose(1, 2)
        attn_weights = torch.matmul(q, new_k.transpose(-2, -1)) / (self.head_dim ** 0.5)
        attn_weights = attn_weights + causal_mask.unsqueeze(0).unsqueeze(0)
        attn_weights = torch.softmax(attn_weights, dim=-1)
        attn_output = torch.matmul(attn_weights, new_v)
        attn_output = attn_output.transpose(1, 2).reshape(1, 1, -1)
        attn_output = layer.self_attn.out_proj(attn_output)
        hidden_states = residual + attn_output
        
        # Cross attention
        residual = hidden_states
        hidden_states = layer.encoder_attn_layer_norm(hidden_states)
        q = layer.encoder_attn.q_proj(hidden_states)
        k = layer.encoder_attn.k_proj(encoder_hidden_states)
        v = layer.encoder_attn.v_proj(encoder_hidden_states)
        
        q = q.view(1, 1, self.num_heads, self.head_dim).transpose(1, 2)
        k = k.view(1, -1, self.num_heads, self.head_dim).transpose(1, 2)
        v = v.view(1, -1, self.num_heads, self.head_dim).transpose(1, 2)
        
        attn_weights = torch.matmul(q, k.transpose(-2, -1)) / (self.head_dim ** 0.5)
        attn_weights = torch.softmax(attn_weights, dim=-1)
        attn_output = torch.matmul(attn_weights, v)
        attn_output = attn_output.transpose(1, 2).reshape(1, 1, -1)
        attn_output = layer.encoder_attn.out_proj(attn_output)
        hidden_states = residual + attn_output
        
        # FFN
        residual = hidden_states
        hidden_states = layer.final_layer_norm(hidden_states)
        hidden_states = layer.fc1(hidden_states)
        hidden_states = torch.relu(hidden_states)
        hidden_states = layer.fc2(hidden_states)
        hidden_states = residual + hidden_states
        
        return hidden_states, (new_k, new_v)


class Florence2WithKVCache:
    """Florence-2 with KV-Cache for faster generation."""
    
    def __init__(self, model_dir: str, mode: str = "multistage"):
        print(f"Loading Florence-2 with KV-Cache ({mode})...")
        
        self.processor = AutoProcessor.from_pretrained(MODEL_NAME, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME, trust_remote_code=True,
            torch_dtype=torch.float32, attn_implementation="eager"
        )
        self.model.eval()
        self.mode = mode
        
        # Load vision + encoder (Neuron)
        if mode == "multistage":
            self.stages = [torch.jit.load(f"{model_dir}/stage{i}.pt") for i in range(4)]
        else:
            self.vision = torch.jit.load(f"{model_dir}/vision_unified.pt")
        self.encoder = torch.jit.load(f"{model_dir}/encoder_{MAX_SEQ}.pt")
        
        # Decoder stays on CPU with KV-Cache (Neuron tracing for dynamic cache is complex)
        self.decoder = self.model.language_model.model.decoder
        self.lm_head = self.model.language_model.lm_head
        self.embed = self.model.language_model.model.shared
        
        # Cache config
        self.num_layers = len(self.decoder.layers)
        self.num_heads = self.decoder.layers[0].self_attn.num_heads
        self.head_dim = self.decoder.layers[0].self_attn.head_dim
        
        self._precompute_pos_embed()
        print("Ready!")
    
    def _precompute_pos_embed(self):
        with torch.no_grad():
            dummy = torch.randn(1, 3, 768, 768)
            vt_out = self.model.vision_tower.forward_features_unpool(dummy)
            self.pos_embed = self.model.image_pos_embed(
                vt_out.view(1, 24, 24, 1024)
            ).view(1, 576, 1024)
    
    def _init_kv_cache(self, max_len=MAX_GEN):
        """Initialize empty KV-Cache."""
        cache = []
        for _ in range(self.num_layers):
            k = torch.zeros(1, self.num_heads, max_len, self.head_dim)
            v = torch.zeros(1, self.num_heads, max_len, self.head_dim)
            cache.extend([k, v])
        return cache
    
    def _decode_one_step(self, input_ids, encoder_out, cache, pos):
        """Decode one token with KV-Cache."""
        inputs_embeds = self.embed(input_ids)
        
        # Use model's decoder with past_key_values
        past_kv = tuple(
            (cache[i*2][:, :, :pos, :], cache[i*2+1][:, :, :pos, :])
            for i in range(self.num_layers)
        ) if pos > 0 else None
        
        out = self.decoder(
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_out,
            encoder_attention_mask=torch.ones(1, encoder_out.shape[1]),
            past_key_values=past_kv,
            use_cache=True
        )
        
        # Update cache - Florence-2 returns ((self_k, self_v, cross_k, cross_v), ...) per layer
        if out.past_key_values:
            for i, layer_cache in enumerate(out.past_key_values):
                # layer_cache = (self_attn_k, self_attn_v, cross_attn_k, cross_attn_v)
                self_k, self_v = layer_cache[0], layer_cache[1]
                cache[i*2][:, :, pos:pos+1, :] = self_k[:, :, -1:, :]
                cache[i*2+1][:, :, pos:pos+1, :] = self_v[:, :, -1:, :]
        
        logits = self.lm_head(out.last_hidden_state)
        return logits[:, -1, :].argmax(-1, keepdim=True)
    
    def generate(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
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
            # Vision (Neuron)
            if self.mode == "multistage":
                x = inputs["pixel_values"]
                for stage in self.stages:
                    x = stage(x)
            else:
                x = self.vision(inputs["pixel_values"])
            
            x = x + self.pos_embed
            x = torch.cat([x.mean(1, keepdim=True), x], dim=1)
            img_emb = self.model.image_proj_norm(x @ self.model.image_projection)
            
            # Encoder (Neuron)
            txt_emb = self.embed(inputs["input_ids"])
            combined = torch.cat([img_emb, txt_emb], dim=1)
            if combined.shape[1] < MAX_SEQ:
                combined = torch.cat([combined, torch.zeros(1, MAX_SEQ - combined.shape[1], 768)], dim=1)
            enc_out = self.encoder(combined)
            
            # Decoder with KV-Cache (CPU)
            cache = self._init_kv_cache(max_new_tokens + 1)
            dec_ids = torch.tensor([[2]])
            
            for pos in range(max_new_tokens):
                next_token = self._decode_one_step(dec_ids[:, -1:], enc_out, cache, pos)
                dec_ids = torch.cat([dec_ids, next_token], dim=1)
                if next_token.item() == 2:
                    break
        
        return self.processor.tokenizer.decode(dec_ids[0], skip_special_tokens=True)
    
    def __call__(self, image, task: str = "<CAPTION>", max_new_tokens: int = 50) -> str:
        return self.generate(image, task, max_new_tokens)
