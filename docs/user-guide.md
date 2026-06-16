# 地理领域文档翻译系统 — 使用手册

**版本**：v1.0  
**日期**：2026-06-16  
**适用人员**：系统管理员、翻译作业操作人员

---

## 1. 系统能力说明

本系统支持将地理领域 Word 文档（`.docx`）在**中文 ↔ 英文**之间自动互译。

**能做到**：
- 自动识别源语言（字符集统计，无需手工指定）
- 翻译正文段落、表格、页眉、页脚
- 保留段落样式（标题级别、正文、列表等）
- 按术语表强制使用指定的专业术语译法
- 大文档（数百页）可靠处理，不截断

**做不到**：
- 段落内局部格式（部分文字加粗/斜体/颜色）在翻译后可能丢失 — 这是 Word 文档翻译的固有限制
- 文本框（Textbox）内容（需手工处理）
- PDF、PPT 等非 `.docx` 格式
- 图片内的文字

---

## 2. 前提条件

### 2.1 系统已部署

确认 CloudFormation Stack `geo-translation` 状态为 `CREATE_COMPLETE` 或 `UPDATE_COMPLETE`：

```bash
aws cloudformation describe-stacks \
  --stack-name geo-translation \
  --region us-east-1 \
  --query 'Stacks[0].StackStatus'
```

### 2.2 获取桶名称

```bash
aws cloudformation describe-stacks \
  --stack-name geo-translation \
  --region us-east-1 \
  --query 'Stacks[0].Outputs' \
  --output table
```

输出示例：

| Key | Value |
|---|---|
| InputBucketName | `geo-translation-input-<ACCOUNT_ID>` |
| OutputBucketName | `geo-translation-output-<ACCOUNT_ID>` |
| TermsBucketName | `geo-translation-terms-<ACCOUNT_ID>` |

### 2.3 SNS 邮件订阅确认

首次部署后，SNS 会向 `NotificationEmail` 发送确认邮件，**必须点击邮件中的 Confirm subscription 链接**，否则翻译完成通知无法送达。

---

## 3. 术语表配置

术语表是翻译质量的核心。术语表中的词条在翻译时**强制生效**（LLM 遇到对应词必须使用指定译法）。

### 3.1 格式规范

CSV 文件，UTF-8 编码，三列：

```
en_term,zh_term,definition
Moho Discontinuity,莫霍面,地壳与地幔的分界面
Plate Tectonics,板块构造,描述岩石圈板块运动的地质学理论
Isostasy,地壳均衡,地壳在地幔软流圈上保持平衡的状态
```

| 列名 | 说明 | 必填 |
|---|---|---|
| `en_term` | 英文术语（完整词组） | 是 |
| `zh_term` | 中文术语（完整词组） | 是 |
| `definition` | 术语定义（不注入提示词，仅供人工参考） | 否 |

**注意事项**：
- 不要有多余空格（`  莫霍面 ` 不会匹配 `莫霍面`）
- 缩写和全称建议分开列条，如 `GPS` 和 `Global Positioning System` 分别一行
- 文件必须有表头行（第一行为列名）
- 系统每次翻译重新加载术语表，更新后立即生效（无需重新部署）

### 3.2 上传术语表

```bash
aws s3 cp geo_terms.csv \
  s3://geo-translation-terms-<ACCOUNT_ID>/terms/geo_terms.csv \
  --region us-east-1
```

> **固定路径**：系统默认读取 `terms/geo_terms.csv`。若需更换路径，修改 CloudFormation Parameter `TermsKey`，然后更新 Stack。

### 3.3 术语表规模建议

| 规模 | 处理方式 | 说明 |
|---|---|---|
| < 1,000 条 | 全量注入 | 最简单，Kimi K2.5 上下文够用 |
| 1,000–10,000 条（推荐） | 内存子串匹配（当前默认） | 每段只注入命中的术语，效率高 |
| > 10,000 条 | 需向量检索（额外配置） | 会产生约 $350+/月 固定成本，需联系管理员 |

---

## 4. 翻译作业操作

### 4.1 上传文档

将需要翻译的 `.docx` 文件上传到 Input Bucket：

```bash
# 命令行方式
aws s3 cp 我的地质报告.docx \
  s3://geo-translation-input-<ACCOUNT_ID>/ \
  --region us-east-1
```

也可以在 AWS Console → S3 → `geo-translation-input-*` → 上传。

**触发时机**：文件上传完成后，EventBridge 在 **约 3–10 秒内**自动触发翻译流程，无需手动操作。

### 4.2 查看翻译进度

#### 方式一：查看 DynamoDB（推荐）

```bash
# 先获取 job_id（文件 SHA-256 前16位，可从 Step Functions 执行输入中查看）
aws dynamodb scan \
  --table-name geo-translation-jobs \
  --region us-east-1 \
  --filter-expression "#s = :s" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":s": {"S": "PROCESSING"}}' \
  --query 'Items[*].{job_id:job_id.S, done:done_chunks.N, total:total_chunks.N, src:source_key.S}'
```

输出示例：
```json
[{
  "job_id": "0e799af127879a20",
  "done": "18",
  "total": "24",
  "src": "geo_test_zh.docx"
}]
```

#### 方式二：Step Functions Console

AWS Console → Step Functions → `geo-translation-workflow` → 查看最新执行。

#### 方式三：等待邮件通知

翻译完成（成功或失败）后，系统自动发邮件到订阅的地址，邮件中包含输出文件的 S3 路径。

### 4.3 下载翻译结果

```bash
# 知道 job_id 时：
aws s3 cp \
  s3://geo-translation-output-<ACCOUNT_ID>/translated/{job_id}/{原文件名}_translated.docx \
  . \
  --region us-east-1

# 列出所有已翻译文件：
aws s3 ls s3://geo-translation-output-<ACCOUNT_ID>/translated/ \
  --recursive \
  --region us-east-1
```

**输出文件命名规则**：`translated/{job_id}/{原文件名去掉.docx}_translated.docx`

---

## 5. 部署与运维

### 5.1 首次部署

**前提**：已安装 AWS CLI、SAM CLI、Python 3.12

```bash
# 1. 进入项目目录
cd geo-translation

# 2. 构建（打包 Lambda Layer 和函数）
sam build -t infra/template.yaml --region us-east-1

# 3. 部署（首次会进入交互式引导）
sam deploy \
  --region us-east-1 \
  --stack-name geo-translation \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides "NotificationEmail=your@email.com" \
  --resolve-s3

# 4. 部署完成后上传术语表
aws s3 cp terms/geo_terms_sample.csv \
  s3://geo-translation-terms-$(aws sts get-caller-identity --query Account --output text)/terms/geo_terms.csv \
  --region us-east-1
```

### 5.2 更新代码或配置

```bash
# 修改代码或 template.yaml 后：
sam build -t infra/template.yaml --region us-east-1
sam deploy \
  --region us-east-1 \
  --stack-name geo-translation \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides "NotificationEmail=your@email.com" \
  --resolve-s3 \
  --no-confirm-changeset
```

### 5.3 更换翻译模型

仅修改 CloudFormation Parameter，**无需改任何代码**（得益于 Bedrock Converse API）：

```bash
sam deploy \
  --region us-east-1 \
  --stack-name geo-translation \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "NotificationEmail=your@email.com" \
    "ModelId=us.amazon.nova-pro-v1:0" \
  --resolve-s3 \
  --no-confirm-changeset
```

当前在 us-east-1 可用的 Kimi 系列模型：

| Model ID | 说明 |
|---|---|
| `moonshotai.kimi-k2.5`（当前） | 标准翻译，平衡质量与速度 |
| `moonshot.kimi-k2-thinking` | 带推理链，质量更高但更慢更贵 |

### 5.4 调整并发数

默认 10 个 chunk 并发翻译。如遇 Bedrock 限流（CloudWatch 告警 `bedrock-throttling` 触发），在 ASL 文件中降低并发：

```bash
# 编辑 statemachine/translation_workflow.asl.json
# 找到 "MaxConcurrency": 10 改为 "MaxConcurrency": 5
# 然后重新 sam build + sam deploy
```

### 5.5 清理 Stack（彻底删除）

```bash
# 先清空 S3 桶（CloudFormation 不能删除非空桶）
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
for bucket in input chunks results output terms; do
  aws s3 rm s3://geo-translation-${bucket}-${ACCOUNT_ID}/ --recursive --region us-east-1
done

# 删除 Stack
aws cloudformation delete-stack \
  --stack-name geo-translation \
  --region us-east-1
```

---

## 6. 常见问题

### Q1：上传文件后翻译没有自动触发

**检查项**：
1. 文件扩展名是否为 `.docx`（区分大小写，`.DOCX` 不会触发）
2. EventBridge 规则是否启用：Console → EventBridge → Rules → `geo-translation-docx-upload`，State 应为 Enabled
3. InputBucket 是否开启了 EventBridge 通知：S3 → Bucket → Properties → Amazon EventBridge → 状态 On

### Q2：翻译完成但内容没有变化（仍是原文）

通常是 chunk-translator Lambda 启动失败，查看 DLQ 是否有消息：

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws cloudformation describe-stacks --stack-name geo-translation --region us-east-1 --query 'Stacks[0].Outputs[?OutputKey==`DLQUrl`].OutputValue' --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --region us-east-1
```

若 DLQ 有消息，查 CloudWatch Logs：
```
/aws/lambda/geo-translation-chunk-translator
```

**常见原因**：Lambda 环境变量缺失、IAM 权限不足、Bedrock 模型未启用。

### Q3：收不到翻译完成邮件

1. 检查 SNS 订阅是否已确认（Console → SNS → Subscriptions，Status 应为 `Confirmed`）
2. 查垃圾邮件文件夹
3. 邮件主题前缀为 `[地理文档翻译]`

### Q4：同一文件重复上传，是否会重复计费翻译

**不会**。系统对文件内容计算 SHA-256，若 DynamoDB 中该哈希已有 SUCCEEDED 记录，直接返回已有结果，跳过全部翻译步骤。**仅文件内容修改后**（哈希值变化）才重新翻译。

### Q5：支持哪些语言对？

当前支持：**中文 → 英文** 和 **英文 → 中文**。  
语言检测阈值：前 20 个非空 chunk 中，CJK 汉字占比 > 15% 判为中文源文档。  
若文档中英混排比例接近 15%，可能误判，建议手工验证源语言判断结果（查 DynamoDB `source_lang` 字段）。

### Q6：表格内容格式看起来有些变化

这是段落级格式保留的已知限制。翻译后表格单元格的**文字内容正确**，但单元格内多个 run 的混合格式（如部分加粗）会被统一为首个 run 的格式。如需保留，请联系管理员启用 run 级翻译模式（成本和耗时约增加 3–5 倍）。

### Q7：术语表更新后需要重新翻译吗

已翻译的文档不会自动重译（幂等性设计）。如需用新术语表重新翻译，**重命名文件**（例如加版本后缀 `_v2.docx`）再上传，或手动在 DynamoDB 中删除对应 job_id 记录。

---

## 7. 资源清单（当前部署）

| 资源 | 名称/ARN |
|---|---|
| Stack | `geo-translation`（us-east-1） |
| Input Bucket | `geo-translation-input-<ACCOUNT_ID>` |
| Output Bucket | `geo-translation-output-<ACCOUNT_ID>` |
| Terms Bucket | `geo-translation-terms-<ACCOUNT_ID>` |
| State Machine | `geo-translation-workflow` |
| DynamoDB Table | `geo-translation-jobs` |
| DLQ | `geo-translation-dlq` |
| SNS Topic | `geo-translation-notifications` |
| Bedrock Model | `moonshotai.kimi-k2.5`（Kimi K2.5，us-east-1） |
