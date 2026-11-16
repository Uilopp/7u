"""Build an evidence-bound, lock-screen-safe daily memory pool on the PC.

The model writes <=15-character snippets anchored to exact source quotes.
Only this explicit maintenance/release operation calls the model; the APK
rotates the resulting local asset without daily network requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance import model_client as provider
from maintenance.archive.memory_pack import is_eligible
from maintenance.core.model_privacy import contains_secret, load_exclusions
from maintenance.core.process_lock import process_lock
from maintenance.core.schema import reject_synthetic_database
from maintenance.model_config import load_model_config
from maintenance.paths import ROOT

TZ = timezone(timedelta(hours=8))
KINDS = ("question", "statement", "memory")
INITIAL_COUNT = 60
INCREMENT_COUNT = 12
MINIMUM_CACHE = 30
MAXIMUM_POOL = 120
MAXIMUM_SELECTION_CALLS = 8
MAXIMUM_CANDIDATES = 120
MINIMUM_FRESH = 15
MINIMUM_PER_KIND = 3
PROMPT_PATH = Path(__file__).with_name("prompts") / "daily_memories.md"
REVIEW_PROMPT_PATH = Path(__file__).with_name("prompts") / "daily_memories_review.md"
GENERATOR_VERSION = 3

# Lock-screen text is deliberately stricter than private in-app retrieval.
_SENSITIVE = re.compile(
    r"月经|例假|大姨妈|痛经|经期|生理期|怀孕|避孕|流产|手术|医院|医生|看病|体检|"
    r"抑郁|焦虑|自杀|自残|死亡|癌|病情|生病|感冒|发烧|胃疼|肚子疼|疼痛|痛苦|吃药|药物|"
    r"做爱|性爱|性欲|裸体|裸照|内裤|内衣|乳房|胸部|屁股|射精|高潮|开房|强奸|"
    r"吵架|分手|讨厌|恶心|傻逼|他妈|妈的|气死|生气|委屈|难过|嫉妒|前任|"
    r"对不起|反省|拉黑|出轨|冷暴力|自卑|攻击|不喜欢|不爱|不开心|不快乐|"
    r"不想活|不理你|不理我|别烦|恨你|骗你|骗我|哭|骂|死了|拒绝|求你|求我|求了|"
    r"不好吃|不好看|色情|亲密|💩|屎|粪|厕所|"
    r"老师|同学|同事|室友|舍友|老板|家长|父母|妈妈|爸爸|弟弟|妹妹|哥哥|姐姐|"
    r"前男友|前女友|陌生人|身份证|学号|工号|银行卡|卡号|住址|地址|定位|"
    r"快递|收件|取件|手机号|电话|微信号|qq号|账户|账号|密码|验证码|工资|余额|转账|借钱|"
    r"api[ _-]?key|password|secret|access[ _-]?token|系统提示|提示词|忽略.{0,12}(规则|指令|提示)|"
    r"执行命令|扮演.{0,12}(角色|助手)|(?:system|assistant|developer)\s*[:：]|"
    r"powershell|cmd\.exe|sudo\s|rm\s+-|```",
    re.I,
)
_CONTACT = re.compile(
    r"https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{5,}\b|"
    r"(?:微信|QQ|电话|手机|联系)\s*[:：=]|[{}<>`]",
    re.I,
)
_CJK = re.compile(r"[\u3400-\u9fff]")
_GENERIC_ANCHOR = re.compile(
    r"^(?:很|真|太|特别|非常|超级|好)?(?:好玩|好吃|好看|可爱|喜欢|开心|快乐|一起|"
    r"今天|昨天|明天|当时|记得|还想|我们|你我|你们|宝宝|宝贝|游戏|画画|东西|那个|这个|效果|努力|混在一起)(?:啊|呀|呢|吗)?$"
)
_CURRENT_TIME = re.compile(r"今天|今日|明天|现在|刚才|刚刚|此刻")
_DANGLING = re.compile(r"(?:这|那)(?:个|颗|只|件|种|些|一颗|一个|一只|一件)(?:呢|呀|啊|吗|，|。|？|！|$)")


class DailyMemoryError(RuntimeError):
    pass


def _safe(text: object, *, target: bool = False) -> bool:
    if not isinstance(text, str) or not text or contains_secret(text):
        return False
    if _SENSITIVE.search(text) or _CONTACT.search(text):
        return False
    # Third-party identities should not become unsolicited lock-screen text.
    if target and re.search(r"[他她]", text):
        return False
    return not any(ord(c) < 32 and c not in "\n\t" for c in text)


def _fingerprint(quote: str) -> str:
    return re.sub(r"[\W_]+", "", quote, flags=re.UNICODE).casefold()


def _source_records(source: Path, *, fingerprint=False):
    reject_synthetic_database(source)
    excluded = load_exclusions(source.parent / "model_exclusions.json")
    conn = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    digest = hashlib.sha256(json.dumps(sorted(excluded), ensure_ascii=False).encode())
    try:
        rows = []
        for row in conn.execute(
            "SELECT msg_id,ts,speaker,text,normalized_kind,voice_state FROM normalized_messages ORDER BY ts,msg_id"
        ):
            # Hash the actual SQLite read snapshot, including WAL-visible edits;
            # hashing the raw .db file alone can miss recently committed rows.
            digest.update(b"\n")
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode())
            if is_eligible(row, excluded):
                rows.append(dict(row))
    finally:
        conn.close()
    if not rows:
        raise DailyMemoryError("没有可用聊天记录，无法准备每日短句。")
    latest = datetime.fromtimestamp(max(row["ts"] for row in rows), TZ).isoformat()
    records = {}
    for i, row in enumerate(rows):
        text = row["text"]
        # The lock screen uses authored text only, avoiding automatic ASR errors.
        if (
            row["normalized_kind"] == "voice"
            or not 6 <= len(text) <= 180
            or len(set(_CJK.findall(text))) < 4
            or not _safe(text, target=True)
        ):
            continue
        nearby = [r for r in rows[max(0, i - 8) : i + 9] if abs(r["ts"] - row["ts"]) <= 900]
        if sum(len(r["text"]) for r in nearby) > 3000 or any(
            not _safe(r["text"]) or len(r["text"]) > 350 for r in nearby
        ):
            continue

        def canonical(r):
            return {
                "msgId": str(r["msg_id"]),
                "date": datetime.fromtimestamp(r["ts"], TZ).date().isoformat(),
                "speaker": "partner" if r["speaker"] == "self" else "self",
                "text": r["text"],
            }

        records[str(row["msg_id"])] = {**canonical(row), "context": [canonical(r) for r in nearby]}
    return (latest, records, digest.hexdigest()) if fingerprint else (latest, records)


def _render(msg_id: str, quote: str, kind: str, source: dict, *, text, anchor) -> dict | None:
    if (
        kind not in KINDS
        or not isinstance(quote, str)
        or not 6 <= len(quote) <= 28
        or len(quote.encode("utf-16-le")) // 2 > 28
        or quote != quote.strip()
        or any(c in quote for c in "\n\r\t")
        or quote not in source["text"]
        or not _safe(quote, target=True)
        or len(set(_CJK.findall(quote))) < 4
    ):
        return None
    if (
        not isinstance(text, str)
        or not 2 <= len(text.encode("utf-16-le")) // 2 <= 15
        or text != text.strip()
        or any(c in text for c in "\n\r\t")
        or not _safe(text, target=True)
        or _CURRENT_TIME.search(text)
        or _DANGLING.search(text)
        or not isinstance(anchor, str)
        or not re.fullmatch(r"[\u3400-\u9fff]{2,6}", anchor)
        or _GENERIC_ANCHOR.fullmatch(anchor)
        or anchor not in quote
        or anchor not in text
    ):
        return None
    identifier = hashlib.sha256(f"{msg_id}\n{quote}\n{kind}\n{text}\n{anchor}".encode()).hexdigest()[:24]
    return {
        "id": identifier,
        "kind": kind,
        "text": text,
        "date": source["date"],
        "speaker": source["speaker"],
        "quote": quote,
        "msgId": msg_id,
        "anchor": anchor,
    }


def _validated(items, records):
    accepted, seen_ids, seen_quotes, seen_texts = [], set(), set(), set()
    if not isinstance(items, list):
        return accepted
    for proposal in items[:1000]:
        if not isinstance(proposal, dict):
            continue
        msg_id, quote = proposal.get("msgId"), proposal.get("quote")
        if not isinstance(msg_id, str) or msg_id not in records or not isinstance(quote, str):
            continue
        fingerprint = _fingerprint(quote)
        if msg_id in seen_ids or fingerprint in seen_quotes:
            continue
        item = _render(
            msg_id,
            quote,
            proposal.get("kind"),
            records[msg_id],
            text=proposal.get("text"),
            anchor=proposal.get("anchor"),
        )
        if item is not None:
            if _fingerprint(item["text"]) in seen_texts:
                continue
            accepted.append(item)
            seen_ids.add(msg_id)
            seen_quotes.add(fingerprint)
            seen_texts.add(_fingerprint(item["text"]))
    return accepted


def _varied(items):
    """Small topic ceilings, not a ranking score; prefer broad everyday details."""
    anchors, topics, result = Counter(), Counter(), []
    for item in items:
        combined = item["text"] + item["quote"]
        categories = [
            name
            for name, pattern in (
                ("drawing", r"画画|绘画|画图|画了|画的|涂鸦"),
                ("gaming", r"游戏|对局|通关|打团|段位|上分"),
            )
            if re.search(pattern, combined)
        ]
        if anchors[item["anchor"]] >= 2 or any(topics[category] >= 6 for category in categories):
            continue
        anchors[item["anchor"]] += 1
        topics.update(categories)
        result.append(item)
    return result


def _review_items(items, records, *, prompt, complete, config, call):
    """Separate model call; only an explicit, unique accept decision is usable."""
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "schema": {
                        "reviews": [
                            {"msgId": "消息ID字符串", "decision": "accept|reject", "reason": "简短理由"}
                        ]
                    },
                    "review_items": [
                        {**item, "context": records[item["msgId"]]["context"]} for item in items
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    review = {"messages": messages}
    call["review"] = review
    response = complete(
        messages,
        key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        max_tokens=5000,
        thinking="disabled",
    )
    review["response"] = response
    accepted = _accepted_review_ids(response, {item["msgId"] for item in items})
    review["accepted_ids"] = sorted(accepted)
    # Missing IDs fail closed: they never become silently accepted.
    return [item for item in items if item["msgId"] in accepted]


def _accepted_review_ids(response, known):
    decoded = json.loads(response.get("content", ""))
    decisions = decoded.get("reviews") if isinstance(decoded, dict) else None
    if not isinstance(decisions, list):
        raise DailyMemoryError("短句语义审核没有返回有效结论。")
    accepted, seen = set(), set()
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or decision.get("msgId") not in known
            or decision["msgId"] in seen
            or decision.get("decision") not in ("accept", "reject")
            or not isinstance(decision.get("reason"), str)
        ):
            raise DailyMemoryError("短句语义审核结论重复或格式不符。")
        seen.add(decision["msgId"])
        if decision["decision"] == "accept":
            accepted.add(decision["msgId"])
    return accepted


def _balanced(items, per_kind):
    counts, result = Counter(), []
    for item in items:
        if counts[item["kind"]] < per_kind:
            result.append(item)
            counts[item["kind"]] += 1
    return result


def _usable_pool(items):
    """A reusable pool must still provide all three notification directions."""
    return len(items) >= MINIMUM_CACHE and set(KINDS).issubset(item["kind"] for item in items)


def _releasable_pool(items):
    counts = Counter(item["kind"] for item in items)
    return MINIMUM_FRESH <= len(items) <= INITIAL_COUNT and all(
        MINIMUM_PER_KIND <= counts[kind] <= INITIAL_COUNT // 3 for kind in KINDS
    )


def _candidates(records, used_ids, attempted, limit=MAXIMUM_CANDIDATES):
    groups = defaultdict(list)
    for item in records.values():
        if item["msgId"] not in used_ids and item["msgId"] not in attempted:
            groups[item["date"][:7]].append(item)
    for group in groups.values():
        group.sort(key=lambda item: hashlib.sha256(item["msgId"].encode()).hexdigest())
    selected, chars = [], 0
    months = sorted(groups, reverse=True)
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for month in months:
            group = groups[month]
            if index >= len(group):
                continue
            item = group[index]
            size = len(json.dumps(item, ensure_ascii=False))
            if len(selected) >= limit or chars + size > 50000:
                return selected
            selected.append(item)
            chars += size
    return selected


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", mode="w", encoding="utf-8", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_daily_pool(
    source: Path,
    output: Path,
    *,
    config,
    cache_path: Path | None = None,
    audit_dir: Path | None = None,
    complete=None,
    emit=print,
    refresh=False,
    fresh=False,
):
    """Caller serializes archive maintenance; no credentials or raw chat in logs."""
    source, output = Path(source), Path(output)
    cache_path = Path(cache_path) if cache_path else source.parent / "daily_memories_cache.json"
    audit_dir = Path(audit_dir) if audit_dir else source.parent / "daily-memories-audit"
    if len({source.resolve(), output.resolve(), cache_path.resolve()}) != 3:
        raise DailyMemoryError("源档案、短句资产和缓存必须使用不同文件。")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    review_prompt = REVIEW_PROMPT_PATH.read_text(encoding="utf-8")
    review_prompt_hash = hashlib.sha256(review_prompt.encode()).hexdigest()
    latest, records, archive_fingerprint = _source_records(source, fingerprint=True)
    cache = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict) or cache.get("version") != 1:
                cache = {}
        except OSError, UnicodeError, json.JSONDecodeError:
            cache = {}
    old = _balanced(_varied(_validated(cache.get("items"), records)), MAXIMUM_POOL // 3)
    if (
        cache.get("generator_version") != GENERATOR_VERSION
        or cache.get("prompt_sha256") != prompt_hash
        or cache.get("review_prompt_sha256") != review_prompt_hash
    ):
        old = []  # Earlier policies have not passed this independent semantic review.
    usable_old = _usable_pool(old)
    last_curated = cache.get("curated_archive_latest", cache.get("archive_latest"))
    mode, selected, calls = "cached", [], []
    needs_generation = (
        fresh
        or refresh
        or last_curated != latest
        or not usable_old
        or cache.get("prompt_sha256") != prompt_hash
    )
    failure = None
    audit_path = audit_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")

    def save_audit(value):
        audit_text = json.dumps(value, ensure_ascii=False)
        if config.api_key:
            audit_text = audit_text.replace(config.api_key, "[MODEL_KEY_REDACTED]")
        _atomic_json(audit_path, json.loads(audit_text))

    if needs_generation:
        desired = INCREMENT_COUNT if usable_old and not fresh else INITIAL_COUNT
        per_kind = desired // 3
        used_ids = {item["msgId"] for item in old}
        used_quotes = {_fingerprint(item["quote"]) for item in old}
        used_texts = {_fingerprint(item["text"]) for item in old}
        if fresh:
            # Even an old-format pool can help avoid replaying the previous release.
            for item in cache.get("items", []) if isinstance(cache.get("items"), list) else []:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("msgId"), str):
                    used_ids.add(item["msgId"])
                if isinstance(item.get("quote"), str):
                    used_quotes.add(_fingerprint(item["quote"]))
        attempted = set()
        batch_limit = MAXIMUM_CANDIDATES
        selection_per_kind = 6
        complete = complete or provider.complete
        try:
            for round_number in range(MAXIMUM_SELECTION_CALLS):
                batch = _candidates(records, used_ids, attempted, limit=batch_limit)
                if not batch:
                    break
                counts = Counter(item["kind"] for item in selected)
                remaining = {kind: min(selection_per_kind, max(0, per_kind - counts[kind])) for kind in KINDS}
                messages = [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "needed_per_kind": remaining,
                                "schema": {
                                    "items": [
                                        {
                                            "msgId": "候选消息ID字符串",
                                            "quote": "逐字原话6–28字",
                                            "kind": "question|statement|memory",
                                            "text": "最终短句不超过15字含标点",
                                            "anchor": "quote和text共有的2–6字具体关键词",
                                        }
                                    ]
                                },
                                "candidates": batch,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                call = {"messages": messages}
                calls.append(call)
                try:
                    response = complete(
                        messages,
                        key=config.api_key,
                        base_url=config.base_url,
                        model=config.model,
                        max_tokens=12000,
                        thinking="disabled",
                    )
                except provider.ProviderLengthError:
                    call["failure_type"] = "ProviderLengthError"
                    batch_limit = max(30, batch_limit // 2)
                    selection_per_kind = 4
                    emit(
                        f"每日短句第 {round_number + 1} 轮达到输出上限，缩小候选后有限重试；已验证 {len(selected)} 条。"
                    )
                    save_audit(
                        {
                            "mode": "in_progress",
                            "calls": calls,
                            "verified_items": selected,
                            "archive_latest": latest,
                            "fresh": fresh,
                        }
                    )
                    continue
                call["response"] = response
                decoded = json.loads(response.get("content", ""))
                proposals = decoded.get("items") if isinstance(decoded, dict) else None
                batch_records = {item["msgId"]: item for item in batch}
                additions = _validated(proposals, batch_records)
                attempted.update(batch_records)
                additions = [item for item in _varied(selected + additions) if item not in selected]
                if additions:
                    save_audit(
                        {
                            "mode": "in_progress",
                            "calls": calls,
                            "verified_items": selected,
                            "archive_latest": latest,
                            "fresh": fresh,
                        }
                    )
                    additions = _review_items(
                        additions,
                        batch_records,
                        prompt=review_prompt,
                        complete=complete,
                        config=config,
                        call=call,
                    )
                for item in additions:
                    fingerprint = _fingerprint(item["quote"])
                    text_fingerprint = _fingerprint(item["text"])
                    if (
                        item["msgId"] in used_ids
                        or fingerprint in used_quotes
                        or text_fingerprint in used_texts
                        or counts[item["kind"]] >= per_kind
                    ):
                        continue
                    selected.append(item)
                    used_ids.add(item["msgId"])
                    used_quotes.add(fingerprint)
                    used_texts.add(text_fingerprint)
                    counts[item["kind"]] += 1
                save_audit(
                    {
                        "mode": "in_progress",
                        "calls": calls,
                        "verified_items": selected,
                        "archive_latest": latest,
                        "fresh": fresh,
                    }
                )
                emit(f"每日短句第 {round_number + 1} 轮完成，已验证 {len(selected)}/{desired} 条。")
                if len(selected) >= desired:
                    break
            if len(selected) < desired and not (fresh and _releasable_pool(selected)):
                raise DailyMemoryError("安全、均衡的候选短句不足。")
            mode = (
                ("fresh" if len(selected) == desired else "fresh_partial")
                if fresh
                else "extended"
                if old
                else "generated"
            )
            last_curated = latest
        except Exception as error:
            failure = type(error).__name__
            selected = []
            if usable_old and not fresh:
                mode = "retained_after_failure"
                emit(f"每日短句本次未补齐；保留 {len(old)} 条已重新校验的旧短句，下次发布会重试。")
            else:
                mode = "failed"
    # Prefer new items when trimming the oldest cache, while limiting every kind.
    items = selected if fresh else _balanced(_varied(selected + list(reversed(old))), MAXIMUM_POOL // 3)
    if not selected and not fresh:
        items = old
    pool = {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "prompt_sha256": prompt_hash,
        "review_prompt_sha256": review_prompt_hash,
        "archive_latest": latest,
        "archive_fingerprint": archive_fingerprint,
        "items": items,
    }
    audit = {
        "mode": mode,
        "archive_latest": latest,
        "curated_archive_latest": last_curated,
        "source_candidates": len(records),
        "old_valid": len(old),
        "added": len(selected),
        "total": len(items),
        "kind_counts": dict(Counter(item["kind"] for item in items)),
        "fresh": fresh,
        "failure_type": failure,
        "calls": calls,
        "pool": pool,
    }
    # Audit is private and contains source excerpts, but never the API credential.
    save_audit(audit)
    if fresh and (mode == "failed" or not _releasable_pool(items)):
        raise DailyMemoryError(
            "本次全新每日短句不足15条或某类不足3条；已停止发布，旧资产未修改，不回退旧池。"
        )
    if mode == "failed" or (not fresh and not _usable_pool(items)):
        raise DailyMemoryError(
            "每日短句生成未通过校验，且不足30条覆盖三种方向的可用旧短句；已停止发布，旧资产未修改。"
        )
    _atomic_json(cache_path, {**pool, "curated_archive_latest": last_curated})
    _atomic_json(output, pool)
    summary = {
        "status": mode,
        "items": len(items),
        "added": len(selected),
        "target": INITIAL_COUNT,
        "target_unfilled": fresh and len(items) < INITIAL_COUNT,
        "kind_counts": audit["kind_counts"],
        "archive_latest": latest,
        "audit": str(audit_path),
        "output": str(output),
    }
    emit(f"每日短句就绪：{len(items)} 条，新增 {len(selected)} 条；无每日 API 调用。")
    return summary


def reject_synthetic_pool(path):
    """A development fixture may be used by Flutter tests, never by a release."""
    path = Path(path)
    if not path.is_file():
        return
    pool = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(pool, dict) and (
        pool.get("is_synthetic") is True or str(pool.get("generator_version", "")).startswith("synthetic")
    ):
        raise DailyMemoryError("检测到合成测试短句池；正式发布只允许基于真实资料生成的短句。")


def validate_prepared_pool(source: Path, output: Path):
    """Explicit internal build path after human QC; never generates or mutates data."""
    source, output = Path(source), Path(output)
    reject_synthetic_pool(output)
    latest, records, archive_fingerprint = _source_records(source, fingerprint=True)
    try:
        raw = output.read_bytes()
        pool = json.loads(raw)
    except OSError, UnicodeError, json.JSONDecodeError:
        raise DailyMemoryError("已审查短句资产无法读取。") from None
    prompt_hash = hashlib.sha256(PROMPT_PATH.read_text(encoding="utf-8").encode()).hexdigest()
    review_prompt_hash = hashlib.sha256(REVIEW_PROMPT_PATH.read_text(encoding="utf-8").encode()).hexdigest()
    if (
        not isinstance(pool, dict)
        or pool.get("version") != 1
        or pool.get("generator_version") != GENERATOR_VERSION
        or pool.get("archive_latest") != latest
        or pool.get("prompt_sha256") != prompt_hash
        or pool.get("review_prompt_sha256") != review_prompt_hash
        or pool.get("archive_fingerprint") != archive_fingerprint
    ):
        raise DailyMemoryError("已审查短句与当前档案或提示词不一致，请重新生成并审查。")
    items = _varied(_validated(pool.get("items"), records))
    if items != pool.get("items") or not _releasable_pool(items):
        raise DailyMemoryError("已审查短句未通过至少15条且每类3条、15字长度、锚点或来源一致性校验。")
    return {
        "status": "prepared_verified",
        "items": len(items),
        "added": 0,
        "target": INITIAL_COUNT,
        "target_unfilled": len(items) < INITIAL_COUNT,
        "kind_counts": dict(Counter(item["kind"] for item in items)),
        "archive_latest": latest,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "output": str(output),
    }


def publish_reviewed_audit(source: Path, output: Path, *, audit_path: Path, cache_path: Path | None = None):
    """Internal recovery after a target-only shortfall, without any model calls.

    Caller holds the maintenance lock. Only exact explicit accepts from this
    archive and both current prompts are eligible; the original audit is kept.
    """
    source, output, audit_path = Path(source), Path(output), Path(audit_path)
    cache_path = Path(cache_path) if cache_path else source.parent / "daily_memories_cache.json"
    if len({source.resolve(), output.resolve(), cache_path.resolve(), audit_path.resolve()}) != 4:
        raise DailyMemoryError("恢复目标不能覆盖源档案或原始审计。")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    latest, records, fingerprint = _source_records(source, fingerprint=True)
    pool = audit.get("pool", {})
    if (
        not audit.get("fresh")
        or pool.get("generator_version") != GENERATOR_VERSION
        or pool.get("archive_latest") != latest
        or pool.get("archive_fingerprint") != fingerprint
        or pool.get("prompt_sha256")
        != hashlib.sha256(PROMPT_PATH.read_text(encoding="utf-8").encode()).hexdigest()
        or pool.get("review_prompt_sha256")
        != hashlib.sha256(REVIEW_PROMPT_PATH.read_text(encoding="utf-8").encode()).hexdigest()
    ):
        raise DailyMemoryError("审计与当前档案或提示词不一致，不能恢复。")
    items, seen_ids, seen_quotes, seen_texts, counts = [], set(), set(), set(), Counter()
    for call in audit.get("calls", []):
        if not call.get("review", {}).get("response") or not call.get("response"):
            continue
        batch = json.loads(call["messages"][-1]["content"])["candidates"]
        batch_records = {row["msgId"]: records[row["msgId"]] for row in batch if row["msgId"] in records}
        proposals = json.loads(call["response"]["content"]).get("items")
        additions = _validated(proposals, batch_records)
        reviewed_ids = _accepted_review_ids(call["review"]["response"], {item["msgId"] for item in additions})
        for item in additions:
            quote_key, text_key = _fingerprint(item["quote"]), _fingerprint(item["text"])
            if (
                item["msgId"] not in reviewed_ids
                or item["msgId"] in seen_ids
                or quote_key in seen_quotes
                or text_key in seen_texts
                or counts[item["kind"]] >= INITIAL_COUNT // 3
                or len(_varied(items + [item])) != len(items) + 1
            ):
                continue
            items.append(item)
            seen_ids.add(item["msgId"])
            seen_quotes.add(quote_key)
            seen_texts.add(text_key)
            counts[item["kind"]] += 1
    if not _releasable_pool(items):
        raise DailyMemoryError("明确通过审核的短句仍不足15条或某类不足3条，不能恢复发布。")
    pool = {**pool, "items": items}
    _atomic_json(cache_path, {**pool, "curated_archive_latest": latest})
    _atomic_json(output, pool)
    result = {
        **validate_prepared_pool(source, output),
        "status": "recovered_reviewed",
        "audit": str(audit_path),
        "added": len(items),
    }
    _atomic_json(audit_path.with_name(audit_path.stem + "-recovered.json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Explicitly select additional items even if the archive date is unchanged.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Generate a brand-new 60-item pool; never fall back to the old pool.",
    )
    args = parser.parse_args()
    config = load_model_config(ROOT / "data/model_config.dpapi")
    if config is None:
        raise DailyMemoryError("请先在维护窗口保存模型 API Key。")
    with process_lock(ROOT / "data/maintenance.lock"):
        result = build_daily_pool(
            ROOT / "data/couple_chat.db",
            ROOT / "app/assets/memory/daily_memories.json",
            config=config,
            refresh=args.refresh,
            fresh=args.fresh,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
