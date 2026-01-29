# Florence-2 on AWS Inferentia2

在 AWS Inferentia2 上部署 Microsoft Florence-2 视觉语言模型，实现高性能推理。

## 结果

| 版本 | 单核 QPS | 双核 QPS | 延迟 (CAPTION) |
|------|----------|----------|----------------|
| CPU 基线 | 0.52 | - | 1930ms |
| FP32 Neuron | 2.82 | 5.64 | 393ms |
| **BF16 Neuron** | **4.09** | **8.18** | **252ms** |

**BF16 版本相比 FP32 提升 45%，相比 CPU 提升 15.7 倍。**

## 为什么做这个项目？

### 问题背景

Florence-2 是微软开源的强大视觉语言模型，支持图像描述、目标检测、OCR 等多种任务。但在 CPU 上推理速度慢（~2秒/张），无法满足生产需求。

### 遇到的挑战

1. **DaViT 架构不兼容 Neuron**
   - Florence-2 使用 DaViT (Dual Attention Vision Transformer) 作为视觉编码器
   - DaViT 内部使用 `parallel_for` 循环和动态 shape，Neuron 编译器无法直接 trace

2. **动态序列长度**
   - Decoder 是自回归的，每步输入长度增加
   - Neuron 要求静态 shape

3. **精度与性能权衡**
   - FP32 精度高但速度慢
   - 需要找到不损失精度的优化方案

### 解决方案

1. **分阶段编译 (Stage-wise Compilation)**
   - 将 DaViT 拆分为 4 个独立阶段，每个阶段固定输入输出 shape
   - 绕过动态 shape 限制，让 Neuron 编译器能够优化

2. **Bucket 策略**
   - 预编译多个 Decoder 模型，对应不同序列长度 (1, 4, 8, 16, 32, 64)
   - 运行时选择最接近的 bucket，padding 到固定长度

3. **BF16 优化**
   - 使用 bfloat16 精度，内存减半，计算加速
   - 实测无精度损失

## 项目结构

```
├── models/
│   ├── florence2/          # FP32 版本 (基础实现)
│   └── florence2_bf16/     # BF16 版本 (推荐生产使用)
├── compile.py              # 快速编译入口 (BF16)
├── inference.py            # 快速推理入口 (BF16)
└── benchmark.py            # 性能测试
```

## 快速开始

### 环境要求

- AWS Inferentia2 实例 (inf2.xlarge 或更大)
- Python 3.8+
- Neuron SDK 2.x

```bash
pip install torch-neuronx neuronx-cc transformers einops timm pillow
```

### 编译模型

```bash
python compile.py --output-dir ./compiled_bf16
```

编译约需 10 分钟，生成：
- `stage0-3.pt` - 视觉编码器 4 个阶段
- `projection.pt` - 视觉到语言的投影层
- `encoder.pt` - 语言编码器
- `decoder_{1,4,8,16,32,64}.pt` - 不同长度的解码器

### 运行推理

```python
from inference import Florence2NeuronBF16

model = Florence2NeuronBF16("./compiled_bf16", core_id="0")

# 图像描述
result = model("image.jpg", "<CAPTION>")
print(result)

# 目标检测
result = model("image.jpg", "<OD>")

# OCR
result = model("image.jpg", "<OCR>")
```

### 最大化吞吐量

inf2.xlarge 有 2 个 NeuronCore，运行两个独立进程可达到 8+ QPS：

```bash
# 终端 1
NEURON_RT_VISIBLE_CORES=0 python inference.py --image img.jpg

# 终端 2
NEURON_RT_VISIBLE_CORES=1 python inference.py --image img.jpg
```

## 支持的任务

| 任务 | Prompt | 说明 |
|------|--------|------|
| 图像描述 | `<CAPTION>` | 简短描述 |
| 详细描述 | `<DETAILED_CAPTION>` | 详细描述 |
| 目标检测 | `<OD>` | 检测物体并返回边界框 |
| OCR | `<OCR>` | 提取图像中的文字 |
| 区域描述 | `<REGION_CAPTION>` | 描述指定区域 |

## 版本选择

| 版本 | 精度 | 速度 | 适用场景 |
|------|------|------|----------|
| `models/florence2/` | FP32 | 2.82 QPS | 需要最高精度 |
| `models/florence2_bf16/` | BF16 | 4.09 QPS | **推荐生产使用** |

## 架构图

```
Image (768x768)
    │
    ▼
┌─────────────────────────────────────┐
│  Vision Encoder (DaViT, 4 stages)   │
│  stage0 → stage1 → stage2 → stage3  │
│  768→192  192→96   96→48   48→24    │
└─────────────────────────────────────┘
    │
    ▼ (576 tokens × 1024 dim)
┌─────────────────────────────────────┐
│  Projection Layer                   │
│  1024 → 768 + Position Embeddings   │
└─────────────────────────────────────┘
    │
    ▼ (577 tokens × 768 dim)
┌─────────────────────────────────────┐
│  Language Encoder (6 layers)        │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Decoder (6 layers, bucketed)       │
│  Buckets: 1, 4, 8, 16, 32, 64       │
└─────────────────────────────────────┘
    │
    ▼
  Output Text
```

## License

Apache 2.0
