"""
chunk-translator Lambda
Step 3 of the translation workflow (invoked per-item by Distributed Map):
  - Receives one chunk (paragraph / table cell / header / footer)
  - Matches relevant geo terms from the terms CSV (in-memory substring match)
  - Builds system + user prompts
  - Calls Bedrock Converse API (Kimi K2) with exponential-backoff retry
  - Writes translation result to S3 results/
  - Increments DynamoDB done_chunks counter
"""
import csv
import io
import json
import logging
import os
import random
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
TERMS_BUCKET = os.environ["TERMS_BUCKET"]
TERMS_KEY = os.environ.get("TERMS_KEY", "terms/geo_terms.csv")
JOBS_TABLE = os.environ["JOBS_TABLE"]
MODEL_ID = os.environ.get("MODEL_ID", "moonshotai.kimi-k2.5")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MAX_TERM_HITS = 30  # cap per chunk to avoid context bloat

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

# Terms cache — lives for the lifetime of the Lambda container
_TERMS: list[dict] | None = None

SYSTEM_PROMPT_TEMPLATE = """\
你是一名专业的地理学文献翻译专家。
翻译时请遵守以下规则：
1. 使用提供的专业术语表，遇到对应词汇必须使用术语表中的译法
2. 遵循{target_lang}的语序和表达习惯，避免直译造成的生硬感
3. 保持学术文献的正式文体
4. 只返回翻译结果，不要解释或添加注释"""

USER_TEMPLATE = """\
请将以下{source_lang}文本翻译为{target_lang}：

【相关专业术语参考】
{terms_block}

【待翻译文本】
{chunk_text}"""


# ── Term matching ─────────────────────────────────────────────────────────────

def load_terms() -> list[dict]:
    global _TERMS
    if _TERMS is None:
        try:
            obj = s3.get_object(Bucket=TERMS_BUCKET, Key=TERMS_KEY)
            text = obj["Body"].read().decode("utf-8")
            _TERMS = list(csv.DictReader(io.StringIO(text)))
            logger.info("Loaded %d terms from s3://%s/%s", len(_TERMS), TERMS_BUCKET, TERMS_KEY)
        except s3.exceptions.NoSuchKey:
            logger.warning("Terms file not found at %s/%s — proceeding without terms", TERMS_BUCKET, TERMS_KEY)
            _TERMS = []
    return _TERMS


def match_terms(chunk_text: str, source_lang: str) -> list[dict]:
    """Substring match: return terms whose source-language field appears in chunk_text."""
    terms = load_terms()
    field = "zh_term" if source_lang == "zh" else "en_term"
    hits = [t for t in terms if t.get(field) and t[field] in chunk_text]
    return hits[:MAX_TERM_HITS]


# ── Prompt building ───────────────────────────────────────────────────────────

def build_prompts(chunk: dict, terms: list[dict], source_lang: str) -> tuple[str, str]:
    target_lang = "英文" if source_lang == "zh" else "中文"
    source_lang_name = "中文" if source_lang == "zh" else "英文"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(target_lang=target_lang)

    terms_block = "\n".join(
        f"- {t['en_term']} = {t['zh_term']}" for t in terms
    ) or "（本段无匹配的专业术语）"

    user_prompt = USER_TEMPLATE.format(
        source_lang=source_lang_name,
        target_lang=target_lang,
        terms_block=terms_block,
        chunk_text=chunk["text"],
    )
    return system_prompt, user_prompt


# ── Bedrock Converse API with retry ──────────────────────────────────────────

RETRIABLE_ERRORS = {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"}


def translate_with_retry(system_prompt: str, user_prompt: str, max_retries: int = 5) -> str:
    for attempt in range(max_retries):
        try:
            response = bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={"maxTokens": 4096},
            )
            return response["output"]["message"]["content"][0]["text"]
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in RETRIABLE_ERRORS:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Bedrock %s on attempt %d, retrying in %.1fs", code, attempt + 1, wait)
                # Emit custom metric for CloudWatch alarm (docs/architecture.md §7.2)
                try:
                    cloudwatch.put_metric_data(
                        Namespace="GeoTranslation",
                        MetricData=[{
                            "MetricName": "BedrockThrottling",
                            "Value": 1,
                            "Unit": "Count",
                        }],
                    )
                except Exception:
                    pass  # metric emission is best-effort
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Translation failed after {max_retries} retries")


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Called once per chunk by Step Functions Distributed Map.
    event is the raw chunk item from the manifest JSON:
      { id, type, ref, text, style?, job_id, source_lang }
    """
    job_id: str = event["job_id"]
    source_lang: str = event["source_lang"]
    ref: str = event["ref"]
    chunk_text: str = event["text"]

    logger.info("Translating chunk %s (job=%s, lang=%s, len=%d)", ref, job_id, source_lang, len(chunk_text))

    terms = match_terms(chunk_text, source_lang)
    system_prompt, user_prompt = build_prompts(event, terms, source_lang)
    translated = translate_with_retry(system_prompt, user_prompt)

    # Persist result
    result = {"ref": ref, "translated": translated}
    result_key = f"results/{job_id}/{ref}.json"
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=result_key,
        Body=json.dumps(result, ensure_ascii=False),
        ContentType="application/json",
    )

    # Best-effort progress counter — failure here is non-fatal
    try:
        dynamodb.Table(JOBS_TABLE).update_item(
            Key={"job_id": job_id},
            UpdateExpression="ADD done_chunks :one",
            ExpressionAttributeValues={":one": 1},
        )
    except Exception as exc:
        logger.warning("DynamoDB counter update failed (non-fatal): %s", exc)

    return {"ref": ref, "result_key": result_key}
