# Florence-2 FP32 版本

在 AWS Inferentia2 上运行 Florence-2 的 FP32 精度实现。

## 结果

| 指标 | 数值 |
|------|------|
| CAPTION 延迟 | 393ms |
| DETAILED_CAPTION 延迟 | 426ms |
| OD 延迟 | 347ms |
| OCR 延迟 | 333ms |
| 单核 QPS | 2.82 |
| 双核 QPS | 5.64 |
| vs CPU 加速 | 5.2x |

测试环境：inf2.8xlarge

## 为什么需要这个版本？

### 问题

Florence-2 使用 DaViT (Dual Attention Vision Transformer) 作为视觉编码器。DaViT 的实现有两个特点让 Neuron 编译器无法直接处理：

1. **动态 shape 传递**
   ```python
   # 原始代码 (transformers 库)
   def forward_features(self, x):
       for conv, block in zip(self.convs, self.blocks):
           x, size = conv(x, size)  # size 是动态的 tuple
           x, size = block(x, size)
   ```
   Neuron 编译器需要静态 shape 才能优化。

2. **parallel_for 循环**
   DaViT 的 attention 块内部使用了 Neuron 不支持的操作。

### 解决方案：分阶段编译

将 DaViT 的 4 个阶段拆分为独立模块，每个模块固定输入输出 shape：

```python
class Stage(torch.nn.Module):
    def __init__(self, conv, block, in_size, out_size):
        super().__init__()
        self.conv = conv
        self.block = block
        self.in_size = in_size   # 固定！
        self.out_size = out_size # 固定！
    
    def forward(self, x):
        x, _ = self.conv(x, self.in_size)
        x, _ = self.block(x, self.out_size)
        return x
```

### 各阶段 shape

| Stage | 输入 | 输出 | 通道数 |
|-------|------|------|--------|
| 0 | (1, 3, 768, 768) | (1, 36864, 128) | 128 |
| 1 | (1, 36864, 128) | (1, 9216, 256) | 256 |
| 2 | (1, 9216, 256) | (1, 2304, 512) | 512 |
| 3 | (1, 2304, 512) | (1, 576, 1024) | 1024 |

## 文件说明

```
florence2/
├── compile.py                    # 编译脚本，详细注释
├── modeling_florence2.py         # 基础推理类
├── modeling_florence2_full.py    # 全 Neuron 推理 (推荐)
├── modeling_florence2_kvcache.py # KV-Cache 版本 (实验性)
├── __init__.py                   # 导出接口
└── README.md                     # 本文件
```

## 使用方法

### 编译

```bash
# 基础编译 (Vision + Encoder)
python -m models.florence2.compile --output ./compiled_fp32

# 完整编译 (+ Decoder，推荐)
python -m models.florence2.compile --output ./compiled_fp32 --with-decoder
```

### 推理

```python
from models.florence2 import Florence2FullNeuron

model = Florence2FullNeuron("./compiled_fp32")
result = model.generate("image.jpg", "<CAPTION>")
print(result)
```

## 三种推理类

| 类名 | Decoder 位置 | 速度 | 说明 |
|------|--------------|------|------|
| `Florence2ForConditionalGeneration` | CPU | 2.2x | 调试用 |
| `Florence2WithKVCache` | CPU + Cache | 2.3x | 内存受限时 |
| `Florence2FullNeuron` | Neuron | 5.2x | **生产推荐** |

## Decoder Bucket 策略

Decoder 是自回归的，每步输入长度 +1。Neuron 需要静态 shape，所以我们：

1. 预编译多个 Decoder：seq_len = 1, 4, 8, 16, 32, 64
2. 运行时选择 >= 当前长度的最小 bucket
3. Padding 到 bucket 长度

```python
# 例：当前生成了 5 个 token
# 选择 bucket 8，padding 3 个位置
buckets = [1, 4, 8, 16, 32, 64]
seq_len = 5
bucket = next(b for b in buckets if b >= seq_len)  # = 8
```

## 为什么不用 KV-Cache？

Florence-2 的 Decoder 有两种 attention：
- Self-attention (需要 cache)
- Cross-attention (需要 encoder 输出的 cache)

在 Neuron 上实现双 cache 比较复杂，而 Florence-2 模型较小 (6 层)，重新计算的开销可接受。Bucket 策略更简单且性能足够。

## 性能优化建议

1. **使用 BF16 版本**：如果精度允许，BF16 版本快 45%
2. **双进程部署**：inf2.xlarge 有 2 个 NeuronCore，跑两个进程
3. **预热**：首次推理会加载模型，后续更快

## License

Apache 2.0
