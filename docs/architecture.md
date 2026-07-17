# 架构文档

## 目标

验证一套全 Serverless 的地理领域 `.docx` 文档中英自动翻译系统：用 Step Functions Distributed Map 对大文档做无上限并发分段翻译，用 Amazon Bedrock（Kimi K2.5）做语义翻译，同时保留 Word 原始格式并保证地理专业术语一致。

## 组件

- **存储**：5 个 S3 桶（input / chunks / results / output / terms），全部 SSE-KMS 加密（单个 CMK，自动轮换）
- **触发**：S3 Event Notification → EventBridge Rule，按 `.docx` 后缀过滤
- **编排**：AWS Step Functions（Standard Workflow），状态机 `statemachine/translation_workflow.asl.json`，开启 X-Ray 追踪
- **计算**：3 个 Lambda（python3.12，1024MB，900s timeout）
  - `doc-parser`：解析 docx 结构（正文/表格/页眉/页脚）、字符集统计判断源语言、SHA-256 内容哈希做幂等去重
  - `chunk-translator`：Step Functions Distributed Map（`MaxConcurrency=10`）的子执行体，按段子串匹配术语表（≤30条注入提示词）后调用 Bedrock Converse API 翻译，指数退避重试最多 5 次
  - `doc-assembler`：按 `ref` 键精确回填译文，段落级格式保留，重组为 `.docx`
- **模型**：`moonshotai.kimi-k2.5`，通过 Bedrock **Converse API** 调用，部署在 `us-east-1`（Bedrock 不在 AWS 中国区提供）
- **状态与可观测性**：DynamoDB 记录 job 状态与幂等键（TTL 30 天）；SQS 死信队列承接翻译失败的 chunk；CloudWatch 3 类告警（Step Functions 失败、DLQ 堆积、doc-assembler 耗时逼近超时）
- **通知**：Step Functions 原生 SNS Publish（无需额外 Lambda），完成/失败均发邮件

## 架构图

```mermaid
flowchart TB
  User["用户上传 .docx"] --> InputS3["S3 input-bucket\nSSE-KMS"]
  InputS3 -->|S3 Event| EB["EventBridge Rule\n.docx 后缀过滤"]
  EB --> SFN

  subgraph SFN["Step Functions 状态机 (Standard Workflow)"]
    Init["[0] InitJob (Pass)"]
    Parse["[1] ParseDocument\nLambda: doc-parser\n语言检测 / 幂等去重"]
    Map["[2] TranslateChunks\nDistributed Map, MaxConcurrency=10"]
    Assemble["[3] AssembleDocument\nLambda: doc-assembler"]
    NotifyOK["[4] NotifySuccess"]
    NotifyFail["NotifyFailure"]
    Init --> Parse --> Map --> Assemble --> NotifyOK
    Parse -.失败.-> NotifyFail
    Map -.失败.-> NotifyFail
    Assemble -.失败.-> NotifyFail
  end

  subgraph MapDetail["Distributed Map 子执行（每 chunk 一次）"]
    Translator["chunk-translator Lambda\n术语匹配 + Bedrock Converse"]
    DLQ["SQS 死信队列\n重试耗尽后进入"]
    Translator -->|重试耗尽| DLQ
  end

  Map --> Translator
  Translator -->|InvokeModel| Bedrock["Amazon Bedrock (us-east-1)\nmoonshotai.kimi-k2.5"]

  ChunksS3["S3 chunks-bucket\nmanifest.json"]
  ResultsS3["S3 results-bucket"]
  TermsS3["S3 terms-bucket\ngeo_terms.csv"]
  OutputS3["S3 output-bucket"]
  DDB["DynamoDB\njob 状态 + 幂等键"]
  SNS["SNS Topic\n完成/失败邮件通知"]

  Parse --> ChunksS3
  Parse <--> DDB
  TermsS3 --> Translator
  Translator --> ResultsS3
  ResultsS3 --> Assemble
  InputS3 --> Assemble
  Assemble --> OutputS3
  Translator -.更新进度.-> DDB
  Assemble -.更新状态.-> DDB
  NotifyOK --> SNS
  NotifyFail --> SNS
```

用户上传 `.docx` 到 input-bucket 后，S3 事件经 EventBridge 触发状态机。`doc-parser` 先对文档内容算 SHA-256 生成 `job_id`，若 DynamoDB 中已是 `SUCCEEDED` 则直接短路返回，否则解析文档结构、判断源语言，把分段清单写入 chunks-bucket 的 `manifest.json`。

`TranslateChunks` 用 Distributed Map 按清单并发拉起最多 10 个 `chunk-translator` 子执行，每个子执行读取术语表命中项拼进提示词，调用 Bedrock Converse API 完成单段翻译并写入 results-bucket；失败的 chunk 重试耗尽后进入 SQS 死信队列。`doc-assembler` 汇总所有分段结果，按 `ref` 键回填进原始 `.docx` 结构，写入 output-bucket。全流程无论成功失败，Step Functions 都会调用 SNS 发送邮件通知。

## 请求路径图

```mermaid
sequenceDiagram
  participant U as 用户
  participant S3in as S3 input-bucket
  participant EB as EventBridge
  participant SFN as Step Functions
  participant Parser as doc-parser Lambda
  participant DDB as DynamoDB
  participant Map as Distributed Map
  participant Trans as chunk-translator Lambda
  participant Bedrock as Bedrock Converse (Kimi K2.5)
  participant Assembler as doc-assembler Lambda
  participant S3out as S3 output-bucket
  participant SNS as SNS

  U->>S3in: 上传 .docx
  S3in->>EB: S3 Event Notification
  EB->>SFN: 触发状态机执行
  SFN->>Parser: ParseDocument
  Parser->>DDB: 查 job_id（SHA-256）是否已 SUCCEEDED
  alt 已存在且成功
    DDB-->>Parser: 已有结果
    Parser-->>SFN: 直接返回，跳过翻译
  else 新文档
    Parser->>Parser: 解析结构 + 语言检测
    Parser-->>SFN: 写 manifest.json，进入下一步
    SFN->>Map: TranslateChunks (MaxConcurrency=10)
    par 每个 chunk 并发执行
      Map->>Trans: 子执行 1..N
      Trans->>Trans: 术语子串匹配（≤30条）
      Trans->>Bedrock: Converse API 翻译
      Bedrock-->>Trans: 译文
      Trans-->>Map: 写 results/{job_id}/{ref}.json
    end
    Map-->>SFN: 全部 chunk 完成
    SFN->>Assembler: AssembleDocument
    Assembler->>S3out: 写回 {name}_translated.docx
    SFN->>SNS: NotifySuccess
    SNS-->>U: 完成邮件通知
  end
```

## 关键技术点

- **语言检测用字符集统计而非 LLM**：采样前 20 个 chunk，统计 CJK 汉字占比 > 0.15 判定为中文，避免中英混排的地理文档被 `langdetect` 误判，且零 API 费用、毫秒级
- **chunk `ref` 设计**：正文 `paragraph_{para_idx}`（`para_idx` 来自 `enumerate(doc.paragraphs)`，含空段落）、表格 `table_{t}_{r}_{c}`、页眉 `header_{s}_{p}`、页脚 `footer_{s}_{p}`，是重组阶段精确回填的唯一定位符
- **术语匹配分级**：< 1,000 条全量注入提示词；1,000–10,000 条（默认）用 Lambda 内存子串匹配，每 chunk 只注入命中术语；> 10,000 条才考虑向量检索（OpenSearch Serverless 最小 2 OCU 常驻约 $350+/月，小术语表绝不启用）
- **格式保留边界**：默认段落级保留（样式、对齐、整段字体字号），段落内局部加粗/斜体/颜色等 inline 格式不保证保留
- **幂等性**：`job_id` = 文档内容 SHA-256 前 16 位，重复上传同一文件直接复用已有结果，不重复计费
- **中国区适用性**：Amazon Bedrock 不在 AWS 中国区提供，本系统需部署在海外 Region（如 `us-east-1`）；若必须在中国区落地，可改用 Amazon Translate + Custom Terminology 路线，质量略降但满足数据不出境要求
- **成本量级**：以 100 页 Word（约 1,000 段落）为基准，变动成本约 $1.46/文档（Bedrock 输入输出 tokens 约占 $0.59，Lambda 约 $0.83），不调用则无固定费用（术语表超 10,000 条启用 OpenSearch Serverless 除外）
