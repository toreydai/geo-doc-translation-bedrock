# 地理领域文档翻译系统 — 测试文档

**版本**：v1.0  
**日期**：2026-06-16  

---

## 1. 测试策略

### 1.1 测试范围

| 层次 | 内容 | 方式 |
|---|---|---|
| 单元 | 语言检测、chunk 解析、术语匹配、格式回填 | 本地 Python 直接调用 |
| 集成 | Lambda 函数独立调用（doc-parser / chunk-translator / doc-assembler） | `sam local invoke` 或 Lambda 控制台 |
| 端到端 | 上传 .docx → EventBridge → Step Functions → 下载译文 | S3 上传触发 |
| 质量 | 翻译准确性：术语命中率、BLEU 分数、人工抽样 | 双语对照语料 |

### 1.2 不在测试范围内

- OpenSearch Serverless 术语向量检索（术语表 < 10,000 条不启用）
- run 级格式保留（默认关闭）
- 中国区路径 A（Amazon Translate）

---

## 2. 端到端测试

### 2.1 测试文档规格

| 项目 | 说明 |
|---|---|
| 文件名 | `geo_test_zh.docx`（中文 → 英文） |
| 内容 | 地壳均衡理论综述，涵盖莫霍面、板块构造、岩石分类、地形地貌等地理专业内容 |
| 标题段落 | 1 个 |
| 正文段落 | 8 个（含一级标题） |
| 表格 | 1 个（4行×3列，12个单元格） |
| 页眉/页脚 | 各 1 个 |
| 总 chunk 数 | 24 |

### 2.2 执行步骤

```bash
# 1. 获取 Input Bucket 名称
INPUT_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name geo-translation --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`InputBucketName`].OutputValue' \
  --output text)

# 2. 上传测试文档
aws s3 cp geo_test_zh.docx s3://${INPUT_BUCKET}/ --region us-east-1

# 3. 等待约 45 秒，查看执行状态
aws stepfunctions list-executions \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name geo-translation --region us-east-1 \
    --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
    --output text) \
  --region us-east-1

# 4. 查看翻译进度（DynamoDB）
aws dynamodb scan \
  --table-name geo-translation-jobs \
  --region us-east-1 \
  --filter-expression "#s = :s" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":s": {"S": "PROCESSING"}}' \
  --query 'Items[*].{job_id:job_id.S, done:done_chunks.N, total:total_chunks.N}'

# 5. 下载结果
OUTPUT_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name geo-translation --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`OutputBucketName`].OutputValue' \
  --output text)

aws s3 ls s3://${OUTPUT_BUCKET}/translated/ --recursive --region us-east-1
```

### 2.3 期望结果

| 阶段 | 状态 | 期望耗时 |
|---|---|---|
| InitJob (Pass) | SUCCEEDED | < 1s |
| ParseDocument | SUCCEEDED | ~2s |
| TranslateChunks（24 chunks，并发 10） | SUCCEEDED | 30–90s |
| AssembleDocument | SUCCEEDED | ~3s |
| NotifySuccess | SUCCEEDED | ~1s |
| **总耗时** | **SUCCEEDED** | **< 2 分钟** |

### 2.4 翻译质量验收标准

#### 正文段落

检查以下类型的翻译是否正确：

| 检查点 | 验收标准 |
|---|---|
| 专业术语 | 与术语表 CSV 中对应译法完全一致 |
| 语序 | 不出现明显直译生硬感（如"驱动下发生水平运动"不应译成逐字对应） |
| 标题级别 | 段落样式与原文一致（标题不变成正文样式） |

#### 表格

| 检查点 | 验收标准 |
|---|---|
| 单元格内容 | 每个单元格独立翻译，内容正确 |
| 表格结构 | 行列数不变，无单元格合并/拆分 |

#### 页眉 / 页脚

- 页眉、页脚文本已翻译
- 页眉/页脚位置未改变

#### 术语命中率

统计测试文档中出现的所有术语表词条，验证译文中对应位置是否使用了术语表指定的译法：

- **基准要求**：术语命中率 ≥ 90%
- **计算方式**：见 §4.3

---

## 3. 质量评估方法

### 3.1 自动评估（BLEU）

对预留的双语对照语料（推荐 100 句）计算 BLEU 分数，在每次模型版本升级后重跑：

```python
from sacrebleu.metrics import BLEU

bleu = BLEU()
score = bleu.corpus_score(
    hypotheses=translated_sentences,
    references=[reference_sentences]
)
print(f"BLEU: {score.score:.2f}")
```

参考值：专业翻译 BLEU 通常在 30–50，LLM 翻译在术语场景下可达 40+。

### 3.2 人工抽样评估

每月随机抽取 3 份已翻译文档，由地理专业人员按以下维度评分（1–5 分）：

| 评分维度 | 说明 |
|---|---|
| 术语准确性 | 专业术语是否与术语表一致 |
| 语序自然度 | 是否符合目标语言习惯，无直译痕迹 |
| 格式保留 | 段落样式、表格结构、页眉页脚是否完整 |
| 语义完整性 | 原意是否完整传达，有无漏译或错译 |

### 3.3 术语命中率计算

```python
def calc_term_hit_rate(chunks, results, terms, source_lang):
    field = "zh_term" if source_lang == "zh" else "en_term"
    target_field = "en_term" if source_lang == "zh" else "zh_term"
    total, hits = 0, 0
    for chunk in chunks:
        for term in terms:
            if term[field] in chunk["text"]:
                total += 1
                if term[target_field] in results[chunk["ref"]]["translated"]:
                    hits += 1
    return hits / total if total else 1.0
```

---

## 4. 回归测试检查清单

每次修改代码或更新术语表后执行：

- [ ] doc-parser 正确解析 paragraph / table_cell / header / footer
- [ ] `para_idx` 与 `assemble_docx` 的 `enumerate` 索引一致（用含空段落的文档测试）
- [ ] 术语匹配：含专业术语的段落命中率 ≥ 90%
- [ ] Bedrock Converse 调用成功，响应格式 `response["output"]["message"]["content"][0]["text"]`
- [ ] 翻译结果写入 `results/{job_id}/{ref}.json`，格式 `{"ref": "...", "translated": "..."}`
- [ ] doc-assembler 读取全部 results 并正确回填（含多段落表格单元格）
- [ ] 幂等性：同一文件第二次上传，Step Functions 在 ParseDocument 后直接结束，不触发翻译
- [ ] DLQ 深度为 0（无 chunk 失败进入死信队列）
- [ ] SNS 邮件通知正常到达
