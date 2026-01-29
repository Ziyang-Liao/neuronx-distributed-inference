# Florence-2 BF16 优化版本

在 AWS Inferentia2 上运行 Florence-2 的 BF16 精度实现，相比 FP32 版本提升 45% 吞吐量。

## 结果

| 指标 | FP32 | BF16 | 提升 |
|------|------|------|------|
| CAPTION 延迟 | 393ms | 252ms | **36%** |
| DETAILED_CAPTION 延迟 | 426ms | 280ms | **34%** |
| OD 延迟 | 347ms | 237ms | **32%** |
| OCR 延迟 | 333ms | 231ms | **31%** |
| 单核 QPS | 2.82 | 4.09 | **45%** |
| 双核 QPS | 5.64 | 8.18 | **45%** |

测试环境：inf2.8xlarge，5 分钟压力测试

## 为什么做 BF16 优化？

### 背景

FP32 版本已经比 CPU 快 5 倍，但我们想进一步提升性能。

### 尝试过的方案

1. **Batch Size 增大** ❌
   - 尝试 bs=4 批量推理
   - 结果：总延迟增加，单张图片延迟没有改善
   - 原因：Florence-2-base 只有 0.23B 参数，模型太小，batch 带来的并行收益被内存带宽抵消

2. **Neuron 编译器 O3 优化** ❌
   - 尝试 `--optlevel 3` 和 `--auto-cast all`
   - 结果：编译失败，Neuron 内部错误 "TritiumFusion: Should be able to fuse two loops!"
   - 原因：DaViT 架构的某些操作与 O3 优化不兼容

3. **BF16 精度** ✅
   - 使用 `torch_dtype=torch.bfloat16` 加载模型
   - 结果：成功！延迟降低 30-36%，吞吐量提升 45%
   - 无明显精度损失

### 为什么 BF16 有效？

1. **内存带宽减半**：BF16 是 16 位，FP32 是 32 位，数据传输量减半
2. **NeuronCore 原生支持**：Inferentia2 的 NeuronCore 对 BF16 有硬件加速
3. **模型大小减半**：编译后的模型从 ~3GB 降到 ~1.5GB

### 为什么选 BF16 而不是 FP16？

- BF16 (Brain Float 16)：1 位符号 + 8 位指数 + 7 位尾数
- FP16 (Half Float)：1 位符号 + 5 位指数 + 10 位尾数

BF16 的指数位与 FP32 相同 (8 位)，数值范围一致，不会溢出。FP16 指数位少，大数值容易溢出导致 NaN。

## 关键发现：Projection 层瓶颈

在优化过程中发现一个隐藏的性能瓶颈：

### 问题

Vision Encoder 输出是 BF16，但 Projection 层在 CPU 上运行时会：
1. BF16 → FP32 转换
2. CPU 计算
3. FP32 → BF16 转换

这个转换开销高达 **789ms**！

### 解决方案

将 Projection 层也编译到 Neuron：

```python
class Projection(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.proj = model.image_proj_norm  # Linear + LayerNorm
        self.pos_embed = model.image_pos_embed
    
    def forward(self, x):
        x = self.proj(x)
        x = x + self.pos_embed
        return x

# 编译
traced = torch_neuronx.trace(Projection(model), 
    torch.randn(1, 576, 1024, dtype=torch.bfloat16))
```

结果：Projection 从 789ms 降到 ~5ms。

## 文件说明

```
florence2_bf16/
├── compile.py    # 编译脚本 (包含 Projection 层)
├── inference.py  # 推理引擎
├── benchmark.py  # 性能测试
└── README.md     # 本文件
```

## 使用方法

### 编译

```bash
python compile.py --output-dir ./compiled_bf16
```

编译约 10 分钟，生成 12 个模型文件，总大小 ~1.5GB。

### 推理

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

### 压力测试

```bash
# 单核测试
python benchmark.py --image test.jpg --duration 60

# 双核测试 (最大吞吐)
python benchmark.py --stress --duration 300 --core 0 &
python benchmark.py --stress --duration 300 --core 1 &
```

## 与 FP32 版本的区别

| 方面 | FP32 | BF16 |
|------|------|------|
| 精度 | 32 位浮点 | 16 位浮点 |
| 模型大小 | ~3GB | ~1.5GB |
| 延迟 | 393ms | 252ms |
| 吞吐量 | 2.82 QPS | 4.09 QPS |
| Projection | CPU | Neuron |
| 精度损失 | 基准 | 无明显损失 |

## 最佳实践

1. **生产环境推荐使用 BF16 版本**
2. **双进程部署**：充分利用 2 个 NeuronCore
3. **预热**：首次推理加载模型较慢，建议启动时预热
4. **监控**：使用 `neuron-top` 监控 NeuronCore 利用率

## License

Apache 2.0
