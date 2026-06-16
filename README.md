# geo-translation

地理领域 Word 文档自动翻译系统，基于 AWS Serverless + Amazon Bedrock（Kimi K2.5）。

支持 `.docx` 文件中英双向互译，保留段落样式、表格结构、页眉页脚，通过地理专业术语表保证术语一致性。

本项目基于 AWS 中国博客文章 [《利用大模型实现地理领域文档中英文自动化翻译》](https://aws.amazon.com/cn/blogs/china/using-llm-to-achieve-automatic-translation-of-geographical-documents-from-chinese-to-english/) 的方案思路，在此基础上做了以下改进：

| 问题 | 改进 |
|---|---|
| Lambda 单函数超时风险 | Step Functions Distributed Map，每段独立并发，无上限 |
| LLM 判断语言，慢且贵 | 字符集统计（CJK 汉字占比），毫秒级确定性结果 |
| 术语全量注入提示词 | 按段子串匹配，只注入命中术语（≤30条），减少干扰 |
| 仅翻译正文段落 | 覆盖正文、表格、页眉、页脚 |
| 无重试，段落丢失无感知 | 指数退避重试 + SQS 死信队列 + CloudWatch 告警 |
| 重复上传重复计费 | SHA-256 内容哈希幂等去重 |
| 使用模型私有 API | Bedrock Converse API，换模型只改一个参数 |

---

## 架构

```
上传 .docx → S3 input
                │
         EventBridge (3–10s)
                │
         Step Functions
         ├── [1] doc-parser      解析结构 / 语言检测 / 幂等去重
         ├── [2] Distributed Map 并发翻译各 chunk（Bedrock Kimi K2.5）
         ├── [3] doc-assembler   重组 .docx，保留原始格式
         └── [4] SNS Publish     原生集成，邮件通知（无 Lambda）
                │
         S3 output → 下载译文
```

| 特性 | 说明 |
|---|---|
| 翻译模型 | `moonshotai.kimi-k2.5`（Bedrock Converse API，换模型只改参数） |
| 大文档 | Step Functions Distributed Map，支持上万段落，单文档 ≤ 15 分钟 |
| 术语表 | CSV 驱动，更新后立即生效，无需重新部署 |
| 幂等性 | SHA-256 内容哈希去重，重复上传不重复计费 |
| 加密 | 全链路 SSE-KMS（S3 / SNS / SQS 共享单个 CMK） |
| 成本 | ≈ $1.46 / 百页文档（变动成本，无固定费用） |

---

## 目录结构

```
geo-translation/
├── lambdas/
│   ├── doc_parser/         文档解析、语言检测、幂等检查
│   ├── chunk_translator/   单段翻译（Bedrock Converse + 术语匹配）
│   └── doc_assembler/      翻译结果回填重组
├── statemachine/
│   └── translation_workflow.asl.json   Step Functions 状态机
├── layers/docx/            python-docx Lambda Layer
├── infra/template.yaml     SAM 模板（CloudFormation）
├── terms/geo_terms_sample.csv  地理术语表示例（50条）
└── docs/
    ├── architecture.md     架构与设计决策
    ├── testing.md          测试方案与检查清单
    └── user-guide.md       部署与操作手册
```

---

## 快速开始

**前提**：已安装 AWS CLI、SAM CLI（`sam --version ≥ 1.100`）、Python 3.12。

```bash
# 1. 构建
sam build -t infra/template.yaml --region us-east-1

# 2. 部署
sam deploy \
  --region us-east-1 \
  --stack-name geo-translation \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides "NotificationEmail=your@email.com" \
  --resolve-s3

# 3. 上传术语表
INPUT_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name geo-translation --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`TermsBucketName`].OutputValue' \
  --output text)

aws s3 cp terms/geo_terms_sample.csv \
  s3://${INPUT_BUCKET}/terms/geo_terms.csv \
  --region us-east-1
```

部署完成后，将 `.docx` 文件上传到 Input Bucket 即自动触发翻译。

> **注意**：首次部署后需确认 SNS 邮件订阅（邮件标题含 `AWS Notification`）。

---

## 使用

详见 [docs/user-guide.md](docs/user-guide.md)。

## 架构说明

详见 [docs/architecture.md](docs/architecture.md)。

## 测试

详见 [docs/testing.md](docs/testing.md)。

---

## 已知限制

- 段落内局部格式（部分加粗/斜体/颜色）翻译后可能丢失
- 文本框（Textbox）内容不处理
- 仅支持 `.docx` 格式（不支持 PDF、PPT）
- Amazon Bedrock 不在 AWS 中国区提供，需部署在海外 Region

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 免责声明

本项目仅供学习与技术参考，不构成生产部署方案。运行过程中会创建 AWS 资源并产生费用，请在实验结束后及时清理。作者不对因使用本项目产生的任何费用或损失承担责任。本项目与 Amazon Web Services 无官方关联，相关服务的可用性与定价以 AWS 官方文档为准。生产环境使用前请根据实际需求进行安全评估与调整。
