# 把 Florence-2 搬上 AWS Inferentia2：成本降 38%、推理快 15 倍的实战指南

> 当你需要对成百上千种物品做实时分类，GPU 方案太贵、纯 CPU 又太慢时，Inferentia2 + Florence-2 可能是最优解。

---

## 一、为什么写这篇博客

在电商、仓储、零售、物流等场景中，**物品分类**是一个极其高频的需求。我在实际项目中遇到了这样的挑战：

- 需要识别的物体种类极多，可能达到 **500 到 2000 种**，而且品类还在不断增加
- 传统分类模型（如 ResNet、EfficientNet）需要为每个类别准备训练数据，每新增一个品类就要重新训练，维护成本极高
- 业务要求低延迟（< 500ms），7×24 小时不间断运行，成本非常敏感

我花了几周时间，把微软的 Florence-2 视觉语言模型完整移植到了 AWS Inferentia2 上，最终实现了 BF16 精度下单核 4.09 QPS、双核 8.18 QPS 的吞吐，延迟仅 252ms。整个过程踩了不少坑，尤其是 Neuron SDK 对开源模型的兼容性问题。这篇博客完整记录了动机、技术挑战、解决方案和最终效果，希望对有类似需求的同学有帮助。

**项目地址**：[https://github.com/Ziyang-Liao/neuronx-distributed-inference](https://github.com/Ziyang-Liao/neuronx-distributed-inference)

---

## 二、应用场景：大规模物品分类的困境

### 2.1 业务背景

想象这样一个场景：你在运营一个大型仓储管理系统，每天有数万件商品需要入库、分拣、上架。货架上的商品种类可能有上千种——从电子产品到日用百货，从食品饮料到办公用品。

每一件商品都需要被准确识别和分类，才能：

- 自动分配到正确的库位
- 生成准确的库存报表
- 触发补货预警
- 支持自动化分拣流程

类似的场景还有很多：

| 场景 | 品类规模 | 延迟要求 | 日处理量 |
|------|---------|---------|---------|
| 仓储商品分拣 | 500-2000 种 | < 500ms | 10 万+ |
| 电商商品审核 | 1000+ 种 | < 1s | 50 万+ |
| 零售货架巡检 | 200-500 种 | < 500ms | 5 万+ |
| 快递包裹分类 | 100-300 种 | < 200ms | 100 万+ |
| 工业质检 | 50-200 种 | < 100ms | 10 万+ |

### 2.2 传统方案的痛点

面对这种大规模分类需求，传统方案各有各的问题：

**方案一：训练专用分类模型（ResNet / EfficientNet）**

这是最直觉的方案——训练一个 N 分类的 CNN 模型。但问题在于：

- 每新增一个品类，就需要收集该品类的训练图片（通常需要几百到几千张）
- 需要重新训练或微调模型，然后重新部署
- 当品类达到上千种时，长尾分布问题严重——热门品类样本充足，冷门品类样本稀缺
- 维护成本随品类数量线性增长

**方案二：调用大型 VLM API（GPT-4V / Claude Vision）**

大模型的理解能力确实强，但：

- API 调用成本高（GPT-4V 约 $0.01-0.03/张图片）
- 延迟通常在 2-10 秒，无法满足实时需求
- 日处理 10 万张图片，仅 API 费用就要 $1000-3000/天
- 依赖外部服务，存在可用性和数据隐私风险

**方案三：YOLO 系列目标检测**

YOLO 速度快，但：

- 需要为每个品类标注边界框数据，标注成本随品类数量爆炸式增长
- 1000 个品类 × 每类 500 张标注 = 50 万张标注图片
- 模型越大、品类越多，训练时间越长
- 新增品类同样需要重新标注和训练

### 2.3 为什么选择 Florence-2

Florence-2 是微软在 2024 年开源的轻量级视觉语言模型，它的独特之处在于：

**1. 零样本能力——新增品类无需重新训练**

Florence-2 通过自然语言 prompt 来理解任务。想识别一个新品类？只需要在 prompt 中描述它。不需要收集训练数据，不需要重新训练模型，不需要重新部署。这对于品类频繁变化的场景来说是革命性的。

**2. 极其轻量——0.23B 参数**

Florence-2-base 仅有 2.3 亿参数，比 GPT-4V 等大模型轻量了几个数量级。这意味着可以在单个加速器上运行，推理速度快，部署成本低。

**3. 多任务统一——一个模型搞定多种视觉任务**

一个 Florence-2 模型就能处理：图片描述（Captioning）、目标检测（Object Detection）、文字识别（OCR）、区域描述（Region Caption）、图像分割（Segmentation）。不需要为每个任务部署一个独立模型，大幅降低了系统复杂度。

**4. MIT 开源协议**，可以自由商用，没有授权风险。

**5. 与 CLIP 的关键区别**

CLIP 通过对比学习训练，擅长图文匹配，但它本质上是一个 embedding 模型，不能直接生成结构化输出（如边界框坐标）。Florence-2 采用生成式架构，能够输出文本描述、坐标、区域等多种格式，任务覆盖面更广。

---

## 三、成本分析：G4dn vs Inf2

选择 Inferentia2 的核心动机就是成本。让我们做一个详细的对比。

### 3.1 实例价格对比

以 us-east-1 区域 On-Demand 价格为例：

| 实例类型 | 加速器 | vCPU | 内存 | 价格 ($/hr) | 适用场景 |
|---------|--------|------|------|------------|---------|
| g4dn.xlarge | 1× NVIDIA T4 (16GB) | 4 | 16 GiB | $0.526 | GPU 推理基线 |
| g4dn.2xlarge | 1× NVIDIA T4 (16GB) | 8 | 32 GiB | $0.752 | 需要更多 CPU/内存 |
| inf2.xlarge | 2× NeuronCores (32GB) | 4 | 16 GiB | $0.758 | Neuron 推理 |

乍一看，inf2.xlarge ($0.758/hr) 比 g4dn.xlarge ($0.526/hr) 贵了 44%。但关键在于**吞吐量**。

### 3.2 吞吐量与单次推理成本

| 方案 | 实例 | 价格 ($/hr) | Florence-2 QPS | 每百万次推理成本 |
|------|------|------------|----------------|-----------------|
| CPU 基线 | c5.2xlarge | ~$0.34 | 0.52 | $181.6 |
| GPU (T4) | g4dn.xlarge | $0.526 | ~3.5 (估算) | $41.7 |
| Neuron FP32 | inf2.xlarge (单核) | $0.758 | 2.82 | $74.7 |
| Neuron FP32 | inf2.xlarge (双核) | $0.758 | 5.64 | $37.3 |
| **Neuron BF16** | **inf2.xlarge (双核)** | **$0.758** | **8.18** | **$25.7** |

> 注：G4dn 上的 QPS 为基于 T4 FP16 推理能力的估算值。

**关键发现：**

- BF16 Neuron 双核方案的每百万次推理成本仅 $25.7，比 GPU 方案低约 **38%**
- 比 CPU 方案低 **86%**
- inf2.xlarge 虽然单价更高，但双核并行带来的吞吐优势完全弥补了价格差距

### 3.3 年化成本估算

假设 7×24 小时运行，处理量为每天 50 万次推理：

| 方案 | 需要实例数 | 月成本 | 年成本 | 相比 GPU 节省 |
|------|-----------|--------|--------|-------------|
| GPU (g4dn.xlarge) | 2 台 | $758 | $9,096 | - |
| **Neuron BF16 (inf2.xlarge)** | **1 台** | **$546** | **$6,552** | **$2,544 (28%)** |

> 计算依据：50 万次/天 ÷ 86400 秒 ≈ 5.8 QPS。GPU 单台约 3.5 QPS 不够，需要 2 台；Neuron BF16 双核 8.18 QPS，1 台足够。

如果使用 Savings Plan 或 Reserved Instance，Inf2 的折扣通常更大，实际节省比例会更高。

### 3.4 隐性成本考量

除了实例费用，还需要考虑：

| 成本项 | GPU 方案 | Neuron 方案 | 说明 |
|--------|---------|------------|------|
| 实例数量 | 更多 | 更少 | 运维复杂度不同 |
| 模型适配工程 | 低（生态成熟） | **高（需要手动适配）** | 这是本文重点 |
| 长期维护 | 中 | 低 | 适配完成后运行稳定 |

这就引出了最大的挑战——**Neuron SDK 的兼容性问题**。

---

## 四、最大的挑战：Neuron SDK 的兼容性难题

### 4.1 为什么很多开源模型在 Neuron 上跑不起来？

如果你尝试过把 HuggingFace 上的模型直接部署到 Inferentia2，大概率会遇到编译失败。这不是个例，而是一个系统性问题。要理解原因，需要先了解 Neuron 编译器的工作原理。

AWS Neuron SDK 的编译器（neuronx-cc）基于 **XLA（Accelerated Linear Algebra）** 的静态图机制。它的工作流程是：

```
PyTorch 模型 → torch.jit.trace / torch_neuronx.trace → 静态计算图 → neuronx-cc 编译 → NeuronCore 可执行文件
```

这个流程有三个硬性约束：

**约束一：所有 Tensor 形状必须在编译时确定（静态 Shape）**

XLA 编译器需要在编译阶段就知道每个 tensor 的完整形状，不能有任何动态维度。这意味着：

```python
# ❌ 这种代码无法编译
output = model(input)  # 如果 input 的 shape 每次都不同

# ✅ 必须固定形状
input = torch.randn(1, 3, 768, 768)  # 编译时确定
output = model(input)
```

为什么 XLA 要求静态形状？因为静态形状允许编译器在编译阶段就完成内存分配规划、算子融合、流水线调度等优化。这些优化是 Inferentia2 高性能的基础，但代价就是灵活性的丧失。

**约束二：不支持依赖 Tensor 值的 Python 控制流**

`torch.jit.trace` 通过实际执行一次模型来记录计算图。这意味着所有 `if`、`for` 等控制流都会被"展平"为执行时走过的那条路径：

```python
# ❌ 无法正确 trace
def forward(self, x):
    if x.sum() > 0:      # 依赖 tensor 的值，trace 只会记录一条分支
        return self.path_a(x)
    else:
        return self.path_b(x)

# ❌ 无法正确 trace
def forward(self, x):
    for i in range(x.shape[0]):  # 如果 x.shape[0] 是动态的
        x = self.layer(x)
    return x
```

**约束三：算子覆盖不完整**

Neuron 编译器支持的 PyTorch 算子集合是 PyTorch 全集的一个子集。一些不常用的算子可能没有对应的 Neuron 实现，遇到这些算子时编译会直接报错。

### 4.2 哪些模型能直接跑，哪些不能？

AWS Neuron 团队已经为主流模型做了官方适配：

| 模型类型 | 代表模型 | Neuron 支持情况 |
|---------|---------|----------------|
| 文本编码器 | BERT, RoBERTa, DistilBERT | ✅ 官方支持 |
| 大语言模型 | Llama 2/3, Mistral, GPT-NeoX | ✅ 通过 NxD Inference 支持 |
| 图像分类 | ResNet, ViT, EfficientNet | ✅ 官方支持 |
| Stable Diffusion | SD 1.5, SDXL | ✅ 官方支持 |
| **视觉语言模型** | **Florence-2** | **❌ 不支持** |

Florence-2 不被支持的原因不是 AWS 不想支持，而是它的架构中有几个组件对 Neuron 编译器来说是"硬骨头"。

### 4.3 Florence-2 的三个编译障碍

**障碍一：DaViT 视觉编码器的动态操作**

Florence-2 使用 DaViT（Dual Attention Vision Transformer）作为视觉编码器。DaViT 的核心创新是同时使用"空间注意力"（spatial attention）和"通道注意力"（channel attention）来捕获全局上下文。

但 DaViT 的实现中使用了 `parallel_for` 循环和动态形状操作。具体来说，DaViT 是一个 4 阶段的层级结构，每个阶段的空间分辨率不同：

```
Stage 0: 768×768 → 192×192, 128 channels
Stage 1: 192×192 → 96×96,   256 channels
Stage 2: 96×96   → 48×48,   512 channels
Stage 3: 48×48   → 24×24,   1024 channels
```

在阶段之间的过渡中，存在动态的 reshape 和 window partition 操作，这些操作的形状依赖于输入，导致 `torch.jit.trace` 无法正确捕获完整的计算图。

**障碍二：自回归 Decoder 的动态序列长度**

Florence-2 的文本生成采用自回归（autoregressive）方式——每一步生成一个 token，然后把这个 token 拼接到输入序列中，作为下一步的输入。这意味着：

- 第 1 步：输入长度 = 1
- 第 2 步：输入长度 = 2
- 第 3 步：输入长度 = 3
- ...
- 第 N 步：输入长度 = N

每一步的输入形状都在变化，这直接违反了 Neuron 的静态形状要求。

**障碍三：Encoder-Decoder 的交叉注意力**

Florence-2 的 decoder 在每一层都需要对 encoder 的输出做交叉注意力（cross-attention）。encoder 输出的形状是固定的（577 tokens × 768 dim），但 decoder 的 query 长度在变化，导致交叉注意力的计算图也是动态的。

---

## 五、Florence-2 架构深度解析

在讲解决方案之前，有必要深入理解 Florence-2 的完整架构，这样才能理解为什么我们要采用特定的编译策略。

### 5.1 整体架构

Florence-2 采用经典的 encoder-decoder 架构，但视觉编码器部分使用了独特的 DaViT：

```
输入图片 (768×768 RGB)
       │
       ▼
┌──────────────────────────────┐
│   DaViT 视觉编码器 (4 stages) │  ← 障碍一：动态操作
│   stage0 → stage1 → stage2   │
│   → stage3                   │
│   768→192  192→96  96→48     │
│   →24                        │
└──────────────────────────────┘
       │
       ▼ (576 tokens × 1024 dim)
┌──────────────────────────────┐
│   投影层 (Projection)         │
│   1024 → 768                 │
│   + 位置编码                  │
└──────────────────────────────┘
       │
       ▼ (577 tokens × 768 dim)
┌──────────────────────────────┐
│   语言编码器 (6 层 Transformer)│
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│   自回归解码器 (6 层)          │  ← 障碍二：动态序列长度
│   + 交叉注意力               │  ← 障碍三：动态 cross-attention
└──────────────────────────────┘
       │
       ▼
     输出文本
```

### 5.2 DaViT 的双注意力机制

DaViT 的每个 stage 内部包含多个 Transformer block，每个 block 依次执行：

1. **空间窗口注意力（Spatial Window Attention）**：将特征图划分为固定大小的窗口（类似 Swin Transformer），在每个窗口内做自注意力。捕获局部空间关系。

2. **通道组注意力（Channel Group Attention）**：将通道维度分组，在组内做注意力。捕获全局通道间的关系。

这种双注意力设计让 DaViT 能同时建模局部空间特征和全局语义特征，效果优于单一注意力机制。但也正是这种复杂的注意力模式，导致了内部实现中的动态操作。

### 5.3 Decoder 的工作流程

以生成图片描述（`<CAPTION>`）为例：

```
步骤 1: 输入 <BOS> token → 输出 "A"
步骤 2: 输入 <BOS>, "A" → 输出 "cat"
步骤 3: 输入 <BOS>, "A", "cat" → 输出 "sitting"
步骤 4: 输入 <BOS>, "A", "cat", "sitting" → 输出 "on"
...
步骤 N: 输入 [...] → 输出 <EOS>
```

每一步的输入序列长度都在增长，这就是动态序列长度问题的根源。

---

## 六、三大核心解决方案

### 6.1 解决方案一：Stage-wise 编译（拆解 DaViT）

既然 DaViT 整体无法 trace，那就把它拆开。

DaViT 本身就是 4 个阶段的层级结构，每个阶段的输入输出形状是确定的。我们把每个 stage 单独提取出来，作为独立的 `nn.Module` 进行编译：

```python
# 伪代码：拆分 DaViT 为 4 个独立模块
class DaViTStage0(nn.Module):
    """stage0: (1, 3, 768, 768) → (1, 192, 192, 128)"""
    def __init__(self, original_model):
        super().__init__()
        self.patch_embed = original_model.patch_embed
        self.stages_0 = original_model.stages[0]

    def forward(self, pixel_values):
        x = self.patch_embed(pixel_values)
        x = self.stages_0(x)
        return x
```

每个 stage 的输入输出形状完全固定：

| Stage | 输入形状 | 输出形状 | 说明 |
|-------|---------|---------|------|
| stage0 | (1, 3, 768, 768) | (1, 192, 192, 128) | Patch embedding + 第一阶段 |
| stage1 | (1, 192, 192, 128) | (1, 96, 96, 256) | 下采样 2× |
| stage2 | (1, 96, 96, 256) | (1, 48, 48, 512) | 下采样 2× |
| stage3 | (1, 48, 48, 512) | (1, 24, 24, 1024) | 下采样 2× |

编译时分别 trace 每个 stage：

```python
import torch_neuronx

# 为每个 stage 创建固定形状的示例输入
example_stage0 = torch.randn(1, 3, 768, 768)
example_stage1 = torch.randn(1, 192, 192, 128)
example_stage2 = torch.randn(1, 96, 96, 256)
example_stage3 = torch.randn(1, 48, 48, 512)

# 分别编译
compiled_stage0 = torch_neuronx.trace(stage0_model, example_stage0)
compiled_stage1 = torch_neuronx.trace(stage1_model, example_stage1)
compiled_stage2 = torch_neuronx.trace(stage2_model, example_stage2)
compiled_stage3 = torch_neuronx.trace(stage3_model, example_stage3)
```

推理时按顺序串联执行：

```python
# 推理流水线
x = compiled_stage0(pixel_values)   # 768→192
x = compiled_stage1(x)              # 192→96
x = compiled_stage2(x)              # 96→48
x = compiled_stage3(x)              # 48→24
# x 的形状: (1, 24, 24, 1024)
# reshape 为 (1, 576, 1024) 作为视觉 token 序列
```

**为什么这样做有效？**

每个 stage 内部虽然有复杂的双注意力操作，但在固定输入形状下，所有中间 tensor 的形状也是确定的。`torch.jit.trace` 可以正确捕获单个 stage 的完整计算图，Neuron 编译器也能正常优化。

### 6.2 解决方案二：Bucket 策略（解决动态序列长度）

自回归 decoder 的序列长度每步都在变化，但我们可以用一个巧妙的方法绕过这个限制——**预编译多个固定长度的 decoder，运行时动态选择**。

我们预定义一组"桶"（bucket）大小：

```python
BUCKET_SIZES = [1, 4, 8, 16, 32, 64]
```

为每个桶大小编译一个独立的 decoder 模型：

```python
compiled_decoders = {}
for bucket_size in BUCKET_SIZES:
    example_input = torch.zeros(1, bucket_size, dtype=torch.long)
    compiled_decoders[bucket_size] = torch_neuronx.trace(
        decoder_model, (example_input, encoder_output)
    )
```

运行时，根据当前序列长度选择最小的 >= 当前长度的桶，不足部分用 padding 填充：

```python
def select_bucket(current_length):
    """选择最小的 >= current_length 的桶"""
    for size in BUCKET_SIZES:
        if size >= current_length:
            return size
    return BUCKET_SIZES[-1]  # 超过最大桶则截断

# 推理示例
current_tokens = [BOS_TOKEN]
for step in range(max_length):
    bucket = select_bucket(len(current_tokens))
    # padding 到桶大小
    padded = pad_to_length(current_tokens, bucket)
    # 使用对应桶的编译模型
    logits = compiled_decoders[bucket](padded, encoder_output)
    next_token = logits.argmax(-1)
    current_tokens.append(next_token)
    if next_token == EOS_TOKEN:
        break
```

**桶大小的选择策略：**

为什么选 `[1, 4, 8, 16, 32, 64]` 而不是 `[1, 2, 3, 4, ..., 64]`？

- 每个桶都需要单独编译，编译时间和存储空间与桶数量成正比
- 6 个桶已经能覆盖 1-64 的所有长度，最大浪费率（padding 比例）可控
- 实测中，大多数 CAPTION 任务的输出在 10-30 tokens，主要命中 16 和 32 号桶

| 实际长度 | 命中桶 | Padding 比例 | 说明 |
|---------|--------|-------------|------|
| 1 | 1 | 0% | 首个 token，零浪费 |
| 3 | 4 | 25% | 可接受 |
| 7 | 8 | 12.5% | 可接受 |
| 12 | 16 | 25% | 可接受 |
| 25 | 32 | 21.9% | 可接受 |
| 50 | 64 | 21.9% | 可接受 |

平均 padding 浪费约 15-25%，换来的是完全静态的计算图，这个 trade-off 非常值得。

### 6.3 解决方案三：BF16 优化

在解决了编译问题之后，下一步是性能优化。BF16（Brain Floating Point 16）是 Inferentia2 NeuronCore 原生支持的数据类型，相比 FP32 有显著优势：

**BF16 vs FP32 对比：**

| 特性 | FP32 | BF16 |
|------|------|------|
| 位宽 | 32 bit | 16 bit |
| 指数位 | 8 bit | 8 bit |
| 尾数位 | 23 bit | 7 bit |
| 数值范围 | ±3.4×10³⁸ | ±3.4×10³⁸ (相同) |
| 精度 | 高 | 略低 |
| 内存带宽 | 1× | **0.5×** |
| NeuronCore 加速 | 基线 | **硬件加速** |

BF16 保留了与 FP32 相同的指数位（8 bit），因此数值范围完全一致，不会出现 FP16 常见的溢出问题。代价是尾数精度从 23 bit 降到 7 bit，但对于推理任务来说，这点精度损失几乎不影响最终结果。

**实现方式：**

```python
from transformers import AutoModelForCausalLM
import torch

# 加载模型时直接指定 BF16
model = AutoModelForCausalLM.from_pretrained(
    "microsoft/Florence-2-base",
    torch_dtype=torch.bfloat16  # 关键：指定 BF16
)
```

**实测效果：**

| 指标 | FP32 | BF16 | 提升 |
|------|------|------|------|
| 单核 QPS | 2.82 | 4.09 | +45% |
| 双核 QPS | 5.64 | 8.18 | +45% |
| 延迟 (CAPTION) | 393ms | 252ms | -36% |
| 输出质量 | 基线 | 无明显差异 | - |

45% 的性能提升，零精度损失——这就是 BF16 的威力。

---

## 七、性能实测与分析

### 7.1 测试环境

| 配置项 | 值 |
|--------|-----|
| 实例类型 | inf2.8xlarge |
| NeuronCores | 2（使用 inf2.xlarge 等效配置） |
| 设备内存 | 32 GB |
| vCPU | 32 cores |
| 系统内存 | 128 GB |
| Neuron SDK | 2.x |
| PyTorch | 2.1+ |
| 模型 | Florence-2-base (0.23B) |

### 7.2 测试方法

为了确保数据的可靠性，我们采用了严格的测试协议：

1. **预热阶段**：丢弃前 10 次推理结果，确保模型已完全加载到 NeuronCore
2. **延迟测试**：连续运行 100 次推理，取 P50（中位数）延迟
3. **吞吐测试**：持续运行 5 分钟，计算 QPS = 总请求数 / 总耗时
4. **输入标准化**：所有测试图片统一 resize 到 768×768 RGB

```bash
# 单核延迟测试
python -m models.florence2_bf16.benchmark --image test.jpg --warmup 10 --runs 100

# 双核吞吐测试（5 分钟）
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 0 &
python -m models.florence2_bf16.benchmark --stress --duration 300 --core 1 &
wait
```

### 7.3 完整性能数据

**延迟对比（CAPTION 任务，P50）：**

| 版本 | 延迟 | 相比 CPU 加速比 |
|------|------|----------------|
| CPU (PyTorch) | 1930ms | 1× |
| FP32 Neuron (单核) | 393ms | 4.9× |
| **BF16 Neuron (单核)** | **252ms** | **7.7×** |

**吞吐对比：**

| 版本 | 单核 QPS | 双核 QPS | 相比 CPU 提升 |
|------|---------|---------|-------------|
| CPU (PyTorch) | 0.52 | - | 1× |
| FP32 Neuron | 2.82 | 5.64 | 10.8× |
| **BF16 Neuron** | **4.09** | **8.18** | **15.7×** |

**各任务延迟对比（BF16 单核）：**

| 任务 | Prompt | 典型输出长度 | 延迟 |
|------|--------|------------|------|
| 简短描述 | `<CAPTION>` | 10-20 tokens | ~252ms |
| 详细描述 | `<DETAILED_CAPTION>` | 30-60 tokens | ~400ms |
| 目标检测 | `<OD>` | 20-50 tokens | ~350ms |
| OCR | `<OCR>` | 5-30 tokens | ~200ms |

### 7.4 性能分析

**为什么 BF16 比 FP32 快 45%？**

主要原因有两个：

1. **内存带宽减半**：BF16 的数据宽度是 FP32 的一半，从 HBM 读取同样数量的参数只需要一半的带宽。对于 Transformer 这种内存带宽受限（memory-bound）的模型，这直接转化为接近 2× 的加速。

2. **NeuronCore 硬件加速**：Inferentia2 的 NeuronCore 对 BF16 矩阵乘法有专门的硬件加速单元，计算吞吐量高于 FP32。

实际加速比是 1.45×（而非理论上的 2×），因为推理过程中还有一些非计算开销（如 token 采样、数据传输等）不受数据类型影响。

**双核 vs 单核为什么是线性扩展？**

inf2.xlarge 的 2 个 NeuronCore 是完全独立的。我们通过 `NEURON_RT_VISIBLE_CORES` 环境变量将两个进程分别绑定到不同的 core，它们之间没有任何资源竞争。因此吞吐量几乎完美地线性扩展（4.09 × 2 = 8.18）。

---

## 八、快速上手指南

### 8.1 环境准备

**Step 1：启动 Inferentia2 实例**

推荐使用 inf2.xlarge（最小规格，2 NeuronCores）。编译阶段如果遇到 OOM，可以使用 inf2.8xlarge。

**Step 2：安装依赖**

```bash
# 安装 Neuron SDK
pip install torch-neuronx neuronx-cc

# 安装模型依赖
pip install transformers einops timm pillow
```

**Step 3：克隆项目**

```bash
git clone https://github.com/Ziyang-Liao/neuronx-distributed-inference.git
cd neuronx-distributed-inference
```

### 8.2 编译模型

编译是一次性操作，完成后编译产物可以保存复用。

```bash
# BF16 版本（推荐，性能最优）
python -m models.florence2_bf16.compile --output-dir ./compiled_bf16

# FP32 版本（如果需要最高精度）
python -m models.florence2.compile --output ./compiled_fp32 --with-decoder
```

编译过程会：
1. 从 HuggingFace 下载 Florence-2-base 模型
2. 将 DaViT 拆分为 4 个 stage 分别编译
3. 编译投影层和语言编码器
4. 为 6 个桶大小分别编译 decoder
5. 保存所有编译产物到指定目录

> ⏱ 编译时间约 15-30 分钟，取决于实例规格。

### 8.3 运行推理

```python
from models.florence2_bf16.inference import Florence2NeuronBF16

# 加载编译好的模型
model = Florence2NeuronBF16("./compiled_bf16", core_id="0")

# 图片描述
result = model("product.jpg", "<CAPTION>")
print(result)  # "A red Nike running shoe on a white background"

# 目标检测
result = model("shelf.jpg", "<OD>")
print(result)  # 返回检测到的物体及其边界框

# OCR 文字识别
result = model("label.jpg", "<OCR>")
print(result)  # 返回图片中的文字内容

# 详细描述
result = model("warehouse.jpg", "<DETAILED_CAPTION>")
print(result)  # 返回详细的场景描述
```

### 8.4 双核部署（最大化吞吐）

inf2.xlarge 有 2 个 NeuronCore，通过环境变量绑定不同的 core 即可实现双核并行：

```bash
# 终端 1：绑定 NeuronCore 0
NEURON_RT_VISIBLE_CORES=0 python -m models.florence2_bf16.inference --image img.jpg

# 终端 2：绑定 NeuronCore 1
NEURON_RT_VISIBLE_CORES=1 python -m models.florence2_bf16.inference --image img.jpg
```

在生产环境中，可以用两个独立的进程（或容器）分别绑定不同的 core，前面加一个负载均衡器做请求分发。

### 8.5 支持的任务列表

| 任务 | Prompt | 输出格式 | 适用场景 |
|------|--------|---------|---------|
| 简短描述 | `<CAPTION>` | 文本 | 商品快速分类 |
| 详细描述 | `<DETAILED_CAPTION>` | 文本 | 商品详情生成 |
| 目标检测 | `<OD>` | 文本 + 坐标 | 货架商品定位 |
| OCR | `<OCR>` | 文本 | 标签/条码识别 |
| 区域描述 | `<REGION_CAPTION>` | 文本 | 指定区域分析 |

---

## 九、架构图

<!-- TODO: 在此处插入完整的系统架构图 -->
<!-- 建议包含以下内容：
  1. 整体推理流水线：图片输入 → DaViT (4 stages) → 投影层 → 语言编码器 → Decoder (bucketed) → 输出
  2. 编译阶段的拆分策略示意图
  3. Bucket 选择的运行时流程
  4. 双核部署架构（负载均衡 → 2 个进程 → 2 个 NeuronCores）
-->

*（架构图待补充）*

---

## 十、这件事的价值

回顾整个项目，我认为它的价值体现在以下几个层面：

### 10.1 直接业务价值

**成本节省**：相比 GPU 方案，每百万次推理成本降低约 38%。对于日处理 50 万次的业务，年化节省约 $2,500。规模越大，节省越多。

**性能达标**：252ms 的延迟和 8.18 QPS 的吞吐完全满足实时物品分类的需求。相比 CPU 方案实现了 15.7 倍加速，让原本不可行的实时场景变得可行。

**运维简化**：Florence-2 的零样本能力意味着新增品类不需要重新训练模型、不需要重新编译、不需要重新部署。只需要更新 prompt 配置即可。这对于品类频繁变化的电商和仓储场景来说，极大地降低了运维负担。

### 10.2 技术价值

**方法论可复用**：本项目中使用的三个核心技术——Stage-wise 编译、Bucket 策略、BF16 优化——并不局限于 Florence-2。任何在 Neuron 上遇到类似问题的模型都可以参考这套方法论：

- 模型有动态操作？→ 拆分为静态子模块分别编译
- 序列长度动态变化？→ 预编译多个桶大小，运行时选择
- 需要更高性能？→ 使用 BF16 精度

**填补生态空白**：Florence-2 是一个非常有价值的模型，但在 Neuron 上没有官方支持。这个项目证明了即使是官方不支持的模型，通过合理的工程手段也能成功部署到 Inferentia2 上。这为其他想在 Neuron 上部署非主流模型的团队提供了参考。

### 10.3 对 Neuron 生态的思考

Neuron SDK 的最大优势是成本和性能，最大劣势是兼容性。目前 AWS 主要在 LLM 和 Stable Diffusion 等热门模型上投入了适配资源，但视觉语言模型、多模态模型等新兴领域的支持还比较薄弱。

我认为随着 Neuron SDK 的持续迭代（目前已到 2.27 版本），以及 PyTorch 2.x 的 `torch.compile` 对动态形状支持的改善，未来这类适配工作会越来越简单。但在当下，手动适配仍然是必要的，也是有价值的。

---

## 十一、局限性与未来方向

### 11.1 当前局限

| 局限 | 说明 | 影响 |
|------|------|------|
| 固定输入尺寸 | 图片必须 resize 到 768×768 | 极端长宽比图片可能损失信息 |
| 最大生成长度 64 tokens | 受限于最大桶大小 | 超长描述会被截断 |
| Batch size = 1 | 单次只处理一张图片 | 对该模型规模来说 batching 收益有限 |
| 仅支持 Inferentia2 | 需要 Neuron SDK 2.x | 不兼容 Inferentia1 |
| 编译时间长 | 首次编译约 15-30 分钟 | 编译产物可复用，只需一次 |

### 11.2 未来优化方向

**1. 扩展桶大小**

当前最大桶为 64 tokens，对于 `<DETAILED_CAPTION>` 等长输出任务可能不够。可以增加 128、256 等更大的桶，代价是额外的编译时间和存储空间。

**2. KV Cache 优化**

当前的 decoder 每一步都重新计算完整序列的注意力。引入 KV Cache 可以避免重复计算，进一步降低延迟。但这需要更复杂的编译策略。

**3. Florence-2-large 支持**

当前项目基于 Florence-2-base（0.23B）。Florence-2-large（0.77B）在精度上更优，但需要更多的设备内存。inf2.xlarge 的 32GB 设备内存应该足够容纳 large 版本。

**4. 多模型流水线**

在实际业务中，可能需要先用 Florence-2 做粗分类，再用专用模型做细分类。可以在同一个 Inferentia2 实例上部署多个模型，利用不同的 NeuronCore 实现流水线处理。

**5. 微调支持**

Florence-2 支持在特定领域数据上微调。如果能在 Trainium 上完成微调，然后在 Inferentia2 上部署推理，就能实现全 AWS 自研芯片的端到端工作流。

---

## 十二、常见问题排查

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `RuntimeError: No Neuron devices` | Neuron 驱动未安装 | `sudo apt install aws-neuronx-dkms` |
| 编译时 OOM | 实例内存不足 | 使用 inf2.8xlarge 或更大实例编译 |
| 首次推理很慢 | 模型加载到 NeuronCore | 在初始化阶段添加预热推理 |
| 吞吐低于预期 | 只用了单核 | 启动双进程分别绑定不同 NeuronCore |
| 输出被截断 | 超过最大桶大小 (64) | 增加更大的桶并重新编译 |
| BF16 输出异常 | 极端数值场景 | 回退到 FP32 版本验证 |

---

## 十三、总结

这个项目的核心故事很简单：

1. **需求**：大规模物品分类，品类多、延迟低、成本敏感
2. **选型**：Florence-2（轻量、零样本、多任务）+ Inferentia2（高性价比）
3. **挑战**：Neuron SDK 不支持 Florence-2 的 DaViT 架构和动态 decoder
4. **解决**：Stage-wise 编译 + Bucket 策略 + BF16 优化
5. **结果**：252ms 延迟、8.18 QPS 吞吐、比 GPU 成本低 38%

如果你也在寻找一个低成本、低延迟的视觉理解方案，希望这篇博客和开源项目能帮到你。

**项目地址**：[https://github.com/Ziyang-Liao/neuronx-distributed-inference](https://github.com/Ziyang-Liao/neuronx-distributed-inference)

欢迎 Star ⭐ 和 Issue 反馈！

---

## 参考资料

1. [Florence-2: Advancing a Unified Representation for a Variety of Vision Tasks (arXiv)](https://arxiv.org/abs/2311.06242)
2. [DaViT: Dual Attention Vision Transformers (ECCV 2022)](https://link.springer.com/chapter/10.1007/978-3-031-20053-3_5)
3. [AWS Neuron SDK 官方文档](https://awsdocs-neuron.readthedocs-hosted.com/)
4. [AWS Inferentia2 产品页](https://aws.amazon.com/machine-learning/inferentia/)
5. [Amazon EC2 On-Demand 定价](https://aws.amazon.com/ec2/pricing/on-demand/)
6. [HuggingFace Florence-2 模型卡](https://huggingface.co/microsoft/Florence-2-base)
7. [PyTorch XLA 动态形状文档](https://docs.pytorch.org/xla/master/notes/source_of_recompilation.html)
8. [Fine-Tuning Florence2 (HuggingFace Blog)](https://huggingface.co/blog/finetune-florence2)
