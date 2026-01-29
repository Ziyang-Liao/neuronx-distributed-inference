# Florence-2 for AWS Inferentia2

Deploy Microsoft Florence-2 vision-language model on AWS Inferentia2.

## Performance

| Version | CAPTION | vs CPU |
|---------|---------|--------|
| CPU baseline | 1930ms | 1x |
| Base (V+E Neuron) | 859ms | 2.2x |
| **Full Neuron** | **373ms** | **5.2x** |

## Quick Start

```bash
# 1. Compile (with decoder for best performance)
python -m neuronx_distributed_inference.models.florence2.compile \
    --output ./models --with-decoder

# 2. Inference
python -c "
from neuronx_distributed_inference.models.florence2 import Florence2FullNeuron
model = Florence2FullNeuron('./models')
print(model.generate('image.jpg', '<CAPTION>'))
"
```

## Three Versions

| Class | Decoder | Speed | Use Case |
|-------|---------|-------|----------|
| `Florence2ForConditionalGeneration` | CPU | 2x | Debugging |
| `Florence2WithKVCache` | CPU+Cache | 2.3x | Memory limited |
| **`Florence2FullNeuron`** | **Neuron** | **5x** | **Production** |

## Supported Tasks

- `<CAPTION>` - Short description
- `<DETAILED_CAPTION>` - Detailed description  
- `<OD>` - Object detection
- `<OCR>` - Text extraction

## Requirements

- inf2.8xlarge or larger
- torch-neuronx >= 2.1
- transformers >= 4.36

## How It Works

Florence-2's DaViT vision encoder uses dynamic shapes that Neuron can't trace.
We solve this by splitting into 4 stages with static shapes:

```
Image → Stage0 → Stage1 → Stage2 → Stage3 → Encoder → Decoder → Text
         ↓        ↓        ↓        ↓         ↓         ↓
       Neuron   Neuron   Neuron   Neuron    Neuron    Neuron
```

See `compile.py` for detailed implementation notes.
