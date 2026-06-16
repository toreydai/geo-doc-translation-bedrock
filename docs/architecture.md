# 地理领域文档翻译系统 — 架构文档

**版本**：v1.0  
**日期**：2026-06-16  
**状态**：已在 us-east-1 生产验证

---

## 1. 系统概述

本系统将地理领域 Word 文档（.docx）在中英双语间自动互译，保留原始段落样式、表格结构、页眉页脚，并通过地理专业术语表保证术语一致性。

### 1.1 核心需求

| # | 需求 | 实现方式 |
|---|---|---|
| R1 | 双向翻译（中→英 / 英→中） | 字符集统计自动识别语言，无需调用 LLM |
| R2 | 保留 Word 格式 | 段落级格式保留；表格、页眉、页脚全覆盖（详见 §5.3） |
| R3 | 地理专业术语一致 | 术语表 CSV 驱动，按段子串匹配注入提示词 |
| R4 | 自然流畅的目标语言表达 | System Prompt 明确指定语序和文体要求 |
| R5 | 大文档可靠性（>100 页） | Step Functions Distributed Map，单文档支持上万段落 |
| R6 | 可观测性 | DynamoDB 进度追踪 + CloudWatch 3 类告警 |
| R7 | 安全合规 | SSE-KMS 加密、最小权限 IAM、VPC Endpoint |

### 1.2 中国区部署说明

Amazon Bedrock 目前**不在 AWS 中国区**（cn-north-1 / cn-northwest-1）提供服务。

| 路径 | 方案 | 适用场景 |
|---|---|---|
| A（中国区） | Amazon Translate + Custom Terminology | 质量要求标准、成本极敏感 |
| B（海外区） | 海外 Bedrock（us-east-1）+ 数据跨境合规 | 需要 LLM 语义理解和术语上下文感知 |

本系统基于**路径 B**，部署在 us-east-1。

---

## 2. 整体架构

```
用户上传 .docx
      │
      ▼
S3 input-bucket ──── SSE-KMS 加密
      │
      │  S3 Event Notification → EventBridge Rule
      ▼
┌──────────────────────────────────────────────────────────┐
│                Step Functions 状态机                      │
│                                                          │
│  [0] InitJob (Pass)                                      │
│       注入 job_id="pending" 占位，确保失败通知始终可取到   │
│       该字段                                              │
│                  │                                       │
│  [1] ParseDocument          Lambda: doc-parser           │
│       · 文档内容 SHA-256 → job_id（16位 hex）            │
│       · DynamoDB 幂等检查：已 SUCCEEDED 则直接返回        │
│       · 解析 docx 结构（正文/表格/页眉/页脚）              │
│       · 字符集统计判断源语言                              │
│       · 每个 chunk 嵌入 job_id + source_lang             │
│       · 写 chunks/{job_id}/manifest.json → S3            │
│                  │                                       │
│  [2] TranslateChunks    Distributed Map (MaxConcurrency=10)│
│       ItemReader 从 S3 读 manifest.json                  │
│       每个 chunk 启动独立子执行 → chunk-translator Lambda  │
│       · 子串匹配相关术语（≤30条注入提示词）               │
│       · Bedrock Converse API（Kimi K2.5）                │
│       · 指数退避重试（最多5次）                           │
│       · 写 results/{job_id}/{ref}.json → S3              │
│       失败 chunk → SQS 死信队列                           │
│                  │                                       │
│  [3] AssembleDocument       Lambda: doc-assembler        │
│       · 分页读取 results/{job_id}/ 下所有结果             │
│       · 按 ref 键精确定位，段落级格式回填                  │
│       · 写 translated/{job_id}/{name}_translated.docx    │
│                  │                                       │
│  [4] NotifySuccess / NotifyFailure                       │
│       Step Functions 原生 SNS Publish → 邮件通知          │
└──────────────────────────────────────────────────────────┘
      │
      ▼
S3 output-bucket（翻译完成的 .docx）
```

---

## 3. AWS 服务清单

| 服务 | 用途 | 配置要点 |
|---|---|---|
| Amazon S3 | 5 个桶：input / chunks / results / output / terms | SSE-KMS，生命周期策略 |
| AWS Step Functions | 编排翻译流程（Standard Workflow） | Distributed Map，X-Ray 追踪 |
| AWS Lambda | 3 个函数：doc-parser / chunk-translator / doc-assembler | python3.12，1024 MB，900s timeout |
| Amazon Bedrock | LLM 翻译（Kimi K2.5，`moonshotai.kimi-k2.5`） | us-east-1，Converse API |
| Amazon DynamoDB | job 状态追踪 + 幂等去重 | PAY_PER_REQUEST，TTL 30 天 |
| Amazon SNS | 完成/失败邮件通知 | KMS 加密 |
| Amazon SQS | 段落翻译失败的死信队列 | KMS 加密，14 天保留 |
| Amazon CloudWatch | 日志、指标、3 类告警 | 详见 §7 |
| AWS EventBridge | S3 上传事件 → 触发 Step Functions | .docx 后缀过滤 |
| AWS KMS | 全链路加密（1 个 CMK，自动轮换） | 所有桶 + SNS + SQS 共享 |
| AWS Lambda Layer | python-docx 依赖层（doc-parser 和 doc-assembler 共用） | python3.12 |

---

## 4. 关键模块设计

### 4.1 语言检测

使用**字符集统计**替代 `langdetect`，地理文档中英混排时不会误判：

```python
CJK_RE = re.compile(r"[一-鿿]")

def detect_language(chunks):
    sample = "".join(c["text"] for c in chunks[:20])
    ratio = len(CJK_RE.findall(sample)) / len(sample)
    return "zh" if ratio > 0.15 else "en"
```

> **为什么不用 langdetect**：地理文档常夹杂大量英文术语（Moho Discontinuity、GPS、GNSS）和参考文献，`langdetect` 采样后容易误判为英文。字符集统计只看汉字占比，确定性强、零 API 费用、毫秒级。

### 4.2 文档解析与 chunk ref 设计

每个 chunk 的 `ref` 键是重组阶段的唯一定位符：

| chunk 类型 | ref 格式 | 示例 |
|---|---|---|
| 正文段落 | `paragraph_{para_idx}` | `paragraph_3` |
| 表格单元格 | `table_{t}_{r}_{c}` | `table_0_1_2` |
| 页眉段落 | `header_{s}_{p}` | `header_0_0` |
| 页脚段落 | `footer_{s}_{p}` | `footer_0_0` |

> **关键约束**：正文段落的 `para_idx` 来自 `enumerate(doc.paragraphs)`（包含空段落），而非"非空段落计数器"。两者一致，否则文档中有空段落时翻译会错位。

### 4.3 术语匹配（分级策略）

| 术语表规模 | 策略 | 理由 |
|---|---|---|
| < 1,000 条 | 全量注入到每个 chunk 提示词 | Kimi K2.5 128K 上下文足够，最简单 |
| 1,000–10,000 条（默认） | Lambda 内存子串匹配 | 零外部依赖，每 chunk 只注入命中术语（≤30条） |
| > 10,000 条 | 向量检索（OpenSearch Serverless 或 FAISS）| 此规模才值得向量库复杂度；FAISS 无常驻成本 |

**注意**：OpenSearch Serverless 最小 2 OCU 常驻 ≈ **$350+/月**，小术语表绝不开启。

### 4.4 Bedrock Converse API

使用 Bedrock **统一 Converse API**（非模型私有 `invoke_model`），换模型只改 `MODEL_ID` 常量：

```python
MODEL_ID = "moonshotai.kimi-k2.5"   # us-east-1 实际 model ID

response = bedrock.converse(
    modelId=MODEL_ID,
    system=[{"text": system_prompt}],
    messages=[{"role": "user", "content": [{"text": user_prompt}]}],
    inferenceConfig={"maxTokens": 4096},
)
```

重试策略：ThrottlingException / ServiceUnavailableException / ModelTimeoutException → 指数退避（2^n + jitter），最多 5 次。每次限流时向 CloudWatch 自定义命名空间 `GeoTranslation/BedrockThrottling` 上报一次计数。

### 4.5 格式保留策略（能力边界）

| 级别 | 能保留 | 不能保留 |
|---|---|---|
| **段落级（默认）** | 段落样式（标题/正文/列表）、对齐、整段字体字号 | 段落内局部加粗/斜体/颜色（inline 格式） |
| **run 级（可选）** | 大部分 inline 格式 | run 拆分/合并的边界错位（仍有概率）；调用量成倍增加 |

默认使用段落级，将整段译文写入第一个 run，清空其余 run，保留第一个 run 的格式作为段落主样式：

```python
def _set_paragraph_text(para, text):
    if not text:
        return
    if para.runs:
        para.runs[0].text = text
        for run in para.runs[1:]:
            run.text = ""
    else:
        para.add_run(text)
```

### 4.6 幂等性

doc-parser 入口对文档内容计算 SHA-256，取前 16 位 hex 作为 `job_id`。同一文件重复上传时，若 DynamoDB 中该 job 已是 `SUCCEEDED`，直接返回已有结果，**跳过翻译，不重复计费**。

---

## 5. 安全设计

### 5.1 IAM 最小权限

每个 Lambda 独立执行角色，仅授权所需操作：

```
doc-parser:        s3:GetObject (input-bucket/*)
                   s3:PutObject (chunks-bucket/*)
                   dynamodb:GetItem, PutItem (jobs 表)
                   kms:Decrypt, GenerateDataKey

chunk-translator:  s3:GetObject (terms-bucket/*)
                   s3:PutObject (results-bucket/*)
                   bedrock:InvokeModel, Converse
                   dynamodb:UpdateItem (jobs 表)
                   cloudwatch:PutMetricData (GeoTranslation namespace only)
                   kms:Decrypt, GenerateDataKey

doc-assembler:     s3:GetObject (input-bucket/*, results-bucket/*)
                   s3:PutObject (output-bucket/*)
                   dynamodb:UpdateItem (jobs 表)
                   kms:Decrypt, GenerateDataKey

states (SFN role):  sns:Publish (notifications topic only)
                   kms:Decrypt, GenerateDataKey
```

### 5.2 数据加密

| 存储层 | 方案 |
|---|---|
| S3 全部 5 个桶 | SSE-KMS（单个 CMK，启用自动轮换） |
| SNS Topic | KMS 加密（同一 CMK） |
| SQS DLQ | KMS 加密（同一 CMK） |
| DynamoDB | AWS 托管加密（按需计费默认开启） |
| Lambda 环境变量 | SAM Globals 继承 KMS（如有敏感变量可单独配置） |
| 传输层 | TLS 1.2+，全程 HTTPS |

### 5.3 网络隔离（可选增强）

当前部署使用公网 Endpoint（Lambda 默认 VPC）。生产强安全场景建议：

```
Lambda → VPC 私有子网
      → VPC Endpoint (Gateway)  → S3
      → VPC Endpoint (Interface) → Bedrock (PrivateLink)
      → VPC Endpoint (Interface) → DynamoDB
      → VPC Endpoint (Interface) → Step Functions
```

---

## 6. 成本模型

### 6.1 按文档变动成本

基准：**100 页 Word 文档（≈30,000 汉字，≈1,000 段落）**，默认架构（内存术语匹配，不含 OpenSearch）

| 服务 | 用量 | 单价 | 小计 |
|---|---|---|---|
| Bedrock Kimi K2.5（输入） | ~150K tokens | $0.60 / 1M tokens | $0.09 |
| Bedrock Kimi K2.5（输出） | ~200K tokens | $2.50 / 1M tokens | $0.50 |
| Lambda（3 函数，≈50,000 GB·s） | 50,000 GB·s | $0.0000166667/GB·s | $0.83 |
| Step Functions Distributed Map | ~1,000 状态转换 | $0.025/1K | $0.03 |
| S3 存储与请求（临时分块） | 忽略不计 | — | ~$0.01 |
| **变动成本合计** | | | **≈ $1.46/文档** |

### 6.2 月度固定成本

| 组件 | 月成本 | 说明 |
|---|---|---|
| Lambda / Step Functions / S3 / Bedrock | $0 | 不调用不计费 |
| **OpenSearch Serverless**（可选） | **≈ $350+** | 术语表 >10,000 条才启用；最小 2 OCU 常驻计费 |
| KMS 客户托管密钥 | $1/密钥 | |
| CloudWatch 日志 | 数美元 | 视日志量 |

### 6.3 横向对比

| 方案 | 100 页成本 | 备注 |
|---|---|---|
| 本方案（Kimi K2.5） | ≈ $1.46 | 术语感知，质量最高 |
| Amazon Translate | ≈ $0.15 | 质量略低；**中国区可用** |
| 人工专业翻译 | ¥3,000–6,000 | ¥100–200/千字 |

---

## 7. 可观测性

### 7.1 DynamoDB 进度表

```
表名：geo-translation-jobs
jobs 表结构：
  job_id (PK, String)  — 文档内容 SHA-256 前16位
  status               — PROCESSING | SUCCEEDED | FAILED
  source_lang          — zh | en
  source_bucket/key    — 原始文件位置
  chunks_manifest      — S3 分块清单路径
  total_chunks         — 总分块数
  done_chunks          — 已完成分块数（Atomic Counter）
  created_at / finished_at — ISO8601 时间戳
  output_key           — 翻译结果 S3 路径
  ttl                  — Unix timestamp，30 天后自动过期
```

### 7.2 CloudWatch 告警

| 告警名称 | 触发条件 | 动作 |
|---|---|---|
| `{stack}-sfn-failures` | Step Functions ExecutionsFailed ≥ 1（5 分钟窗口） | SNS 通知 |
| `{stack}-dlq-depth` | SQS DLQ 消息数 ≥ 1（1 分钟窗口） | SNS 通知 + 人工介入 |
| `{stack}-lambda-duration` | doc-assembler Duration p99 > 12 分钟（5 分钟窗口） | SNS 通知（接近超时） |
| `{stack}-bedrock-throttling` | 自定义指标 GeoTranslation/BedrockThrottling ≥ 10/分钟 | SNS 通知（考虑降低并发） |

---

## 8. 与原博客方案对比

| 维度 | 原博客方案 | 本方案 |
|---|---|---|
| 大文档可靠性 | ❌ Lambda 超时风险 | ✅ Step Functions Distributed Map |
| 语言检测 | ❌ 调 LLM，慢且贵 | ✅ 字符集统计，确定性、毫秒级 |
| 术语注入 | ❌ 全量注入，干扰模型 | ✅ 按段匹配；规模分级（内存/向量） |
| 格式覆盖 | ❌ 仅正文段落 | ✅ 正文/表格/页眉/页脚 + 如实说明格式边界 |
| 错误处理 | ❌ 无重试，段落丢失 | ✅ 指数退避 + 死信队列 + 空值保护 |
| 幂等性 | ❌ 重复上传重复计费 | ✅ 内容哈希去重 |
| 安全设计 | ❌ 未提及 | ✅ 最小权限 + KMS + VPC 规划 |
| 成本透明度 | ❌ 无估算 | ✅ 变动成本 ≈$1.46/百页；固定成本明确说明 |
| 中国区适用 | ❌ 未说明 Bedrock 不可用 | ✅ 路径 A/B 双方案 |
| 质量验证 | ❌ 无 | ✅ BLEU 评分 + 人工抽样 |

---

## 9. 目录结构

```
geo-translation/
├── docs/
│   ├── architecture.md              本文档
│   ├── testing.md                   测试文档
│   └── user-guide.md                使用手册
├── lambdas/
│   ├── doc_parser/handler.py        Step 1: 解析文档
│   ├── chunk_translator/handler.py  Step 2: 翻译段落（Distributed Map item）
│   └── doc_assembler/handler.py     Step 3: 重组文档
├── layers/docx/requirements.txt     python-docx Lambda Layer
├── statemachine/
│   └── translation_workflow.asl.json  Step Functions ASL
├── infra/
│   └── template.yaml               SAM 模板（CloudFormation）
└── terms/
    └── geo_terms_sample.csv        地理术语表示例（50条）
```
