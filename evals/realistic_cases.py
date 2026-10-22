"""Versioned synthetic work-handoff cases, not sampled production conversations."""

from __future__ import annotations

import json
from pathlib import Path
import random

from evals.recall_cases import grade_answer

DATASET_VERSION = "realistic-v1"
DATASET_SEED = 20261021
DATASET = Path(__file__).parent / "datasets" / "recall50-realistic-v1.json"
FAMILIES = ("handoff", "revisions", "collisions", "live-files", "mixed")

# Distinct task briefs and typed answers. Numbers, paths, lists and negative
# constraints are mixed; these are not 50 copies of one project-ID lookup.
BRIEFS = [
    ("music", "Vue 音乐工作台", "接手播放器发布，保留播放列表和搜索体验", {
        "api_port": 8187, "route_prefix": "/listen", "page_size": 24,
        "package_manager": "pnpm", "theme": "moss-dark", "preview_branch": "release/player-rc2",
        "allowed_audio": ["mp3", "flac"], "telemetry_enabled": False}),
    ("orders", "订单服务", "交接重复订单修复和灰度发布", {
        "api_port": 9073, "route_prefix": "/v2/orders", "page_size": 50,
        "retry_limit": 2, "idempotency_header": "X-Order-Nonce", "release_branch": "fix/order-race",
        "payment_methods": ["card", "balance"], "auto_refund": False}),
    ("uploads", "文件上传网关", "交接大文件断点上传的上线约束", {
        "api_port": 8239, "route_prefix": "/assets/upload", "max_upload_mb": 32,
        "chunk_kib": 512, "artifact_dir": "var/upload-staging", "retention_hours": 36,
        "allowed_extensions": ["png", "pdf"], "public_bucket": False}),
    ("search", "商品搜索", "接手中文分词与过滤器联调", {
        "api_port": 9311, "route_prefix": "/catalog/search", "page_size": 18,
        "timeout_ms": 1800, "index_alias": "catalog-zh-green", "owner_team": "discovery-cn",
        "sort_order": ["relevance", "updated_at"], "fuzzy_match": False}),
    ("billing", "账单导出", "交接财务导出任务，保留金额精度", {
        "api_port": 8751, "route_prefix": "/billing/export", "decimal_places": 2,
        "timezone": "Asia/Shanghai", "output_dir": "reports/monthly-cn", "batch_size": 120,
        "formats": ["csv", "xlsx"], "include_test_accounts": False}),
    ("notify", "通知中心", "接手短信限流与模板审核", {
        "api_port": 8467, "route_prefix": "/notify/send", "rate_limit": 75,
        "retry_limit": 3, "template_id": "CN-SERVICE-07", "quiet_hours_start": "22:30",
        "channels": ["email", "sms"], "marketing_enabled": False}),
    ("inventory", "库存同步", "交接仓库同步与冲突处理", {
        "api_port": 9127, "route_prefix": "/stock/sync", "batch_size": 240,
        "poll_seconds": 45, "source_warehouse": "WH-HZ-02", "conflict_policy": "remote-wins",
        "sync_fields": ["quantity", "reserved"], "allow_negative": False}),
    ("auth", "登录网关", "接手会话续期和跨域配置", {
        "api_port": 8641, "route_prefix": "/auth/session", "session_minutes": 90,
        "clock_skew_seconds": 20, "cookie_name": "mini_session_v2", "same_site": "Lax",
        "allowed_origins": ["https://console.example.test"], "public_signup": False}),
    ("analytics", "数据看板", "交接活跃指标与筛选项缓存", {
        "api_port": 9533, "route_prefix": "/metrics/daily", "cache_ttl_seconds": 420,
        "window_days": 14, "metric_name": "active_accounts_v3", "export_path": "exports/active.json",
        "dimensions": ["region", "plan"], "sample_test_events": False}),
    ("jobs", "后台任务队列", "接手失败任务重投和优先级调度", {
        "api_port": 8821, "route_prefix": "/jobs/admin", "concurrency": 6,
        "visibility_seconds": 150, "queue_name": "reconcile-normal", "dead_letter_dir": "var/dead-letter",
        "priorities": ["urgent", "normal", "bulk"], "retry_permanent_errors": False}),
]


def build_specs():
    specs = []
    for family_index, family in enumerate(FAMILIES):
        for index, (slug, title, goal, _) in enumerate(BRIEFS):
            seed = DATASET_SEED + family_index * 101 + index * 17
            rng = random.Random(seed)
            # A mix of triggered and untriggered cases under production settings.
            pressure = "high" if (index + family_index) % 2 == 0 else "medium"
            specs.append({"id": f"{family}-{slug}", "family": family, "brief": slug,
                          "title": title, "goal": goal, "seed": seed, "pressure": pressure,
                          "placement": "distributed",
                          "history_pairs": rng.randint(56, 68) if pressure == "high" else rng.randint(30, 44),
                          "noise_lines": rng.randint(7, 14)})
    return specs


def load_specs(path=DATASET):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (data.get("version") != DATASET_VERSION or data.get("seed") != DATASET_SEED
            or data.get("cases") != build_specs()):
        raise ValueError("Realistic dataset differs from its versioned deterministic definition")
    return data["cases"]


def changed(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, list):
        return list(reversed(value)) + ["legacy"]
    return str(value) + "-draft"


def materialize(spec):
    _, title, goal, base = next(brief for brief in BRIEFS if brief[0] == spec["brief"])
    rng = random.Random(spec["seed"])
    project = f"{spec['brief']}-{spec['seed'] % 997:03d}"
    expected = dict(base)
    # Independently seeded numbers avoid a shared answer across the five families.
    for key, value in list(expected.items()):
        if isinstance(value, int) and not isinstance(value, bool):
            expected[key] = value + rng.randint(0, 4)
    count = spec["history_pairs"]
    fragments = [[] for _ in range(count)]
    keys = list(expected)
    anchor_end = count // 2 - 2 if spec["family"] in {"revisions", "mixed"} else count - 3
    anchors = rng.sample(range(2, anchor_end), len(keys))
    json_value = lambda value: json.dumps(value, ensure_ascii=False)
    for number, (key, value) in enumerate(expected.items()):
        anchor = anchors[number]
        if spec["family"] in {"revisions", "mixed"}:
            fragments[anchor].append(
                f"交接记录：项目 {project}，生产环境，旧版已确认 {key} = {json_value(changed(value))}。")
            fragments[count // 2 + number].append(
                f"决定 R{number+20}：{project} 生产环境的 {key} 已确认改成 {json_value(value)}，"
                "此决定替代此前同字段约定并立即生效。")
        else:
            fragments[anchor].append(
                f"项目负责人确认：{project} 的生产配置 {key} = {json_value(value)}。")
        if spec["family"] in {"collisions", "mixed"}:
            fragments[(anchor + 1) % count].append(
                f"隔壁项目 {project}-legacy（不是本项目）生产环境的 {key} = {json_value(changed(value))}。")
            fragments[(anchor + 2) % count].append(
                f"{project} 的 staging 环境：{key} = {json_value(changed(value))}；仅用于联调。")
        if spec["family"] in {"revisions", "collisions", "mixed"}:
            fragments[count - 3 - number].append(
                f"待讨论提案：建议把 {project} 的 {key} 调到 {json_value(changed(value))}；"
                "尚未批准，不改变现行约定。")

    files = {}
    if spec["family"] in {"live-files", "mixed"}:
        current = {key: changed(expected[key]) for key in keys[:3]}
        expected.update(current)
        files["config/production.json"] = json.dumps(
            {"project": project, "environment": "production", "settings": current}, ensure_ascii=False, indent=2)
        # Its historical read deliberately differs from the real file today.
        fragments[count - 5].append(
            "交接提醒：config/production.json 之后又在工作区被人工修改过；"
            "涉及文件中已有的字段，最终答案以当前文件内容为准，历史只是旧快照。")

    noise_topics = ["图标间距与导航对齐", "文档链接整理", "测试报告归档", "CI 缓存诊断", "无障碍标签核对"]
    history = []
    for index in range(count):
        lines = []
        for row in range(spec["noise_lines"]):
            ticket = rng.randrange(10000, 99999)
            lines.append(f"检查单 T-{ticket}: {rng.choice(noise_topics)}；对象 sample-{index:02d}-{row}，"
                         f"耗时 {rng.randrange(12, 750)}ms，结论已记录。与生产发布参数无关。")
        position = rng.randrange(len(lines) + 1)
        lines[position:position] = fragments[index]
        text = f"{title} · 第 {index+1} 段工作记录\n" + "\n".join(lines)
        tool_id = f"handoff_{index:03d}"
        if spec["family"] in {"handoff", "revisions"} and index % 4 == 0:
            history.extend([{"role": "user", "content": text},
                            {"role": "assistant", "content": "收到，后续按已确认事项执行。"}])
        else:
            history.extend([
                {"role": "user", "content": f"继续检查 {title} 的交接记录片段 {index+1}。"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                 "name": "read_file", "input": {"path": f"handoff/record-{index:03d}.log"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
                {"role": "assistant", "content": "这段已核对。"},
            ])
    unknowns = ["incident_commander", "signed_release_digest"]
    prompt = (
        f"你正在接手「{title}」，当前目标：{goal}。目标项目为 {project} 的 production 环境。\n"
        "请核对此前交接内容，把已确认的现行配置写入 answer.json。后续已批准的决定替代旧约定，"
        "未批准的提案、其他项目及 staging 环境不生效。工作区里当前存在的生产配置文件优先于历史快照，"
        "但文件没有的字段仍按交接约定。不要猜测未给出的信息，未知写 null。\n"
        "只写一个 JSON 对象，恰好包含这些字段，保持数值、布尔和数组类型及数组顺序："
        + json.dumps([*expected, *unknowns], ensure_ascii=False)
        + "。不要修改其他文件；核对完成后结束任务。"
    )
    # A common environment note establishes archive semantics in both arms;
    # no case-specific answer, keyword, or archive filename is supplied.
    environment = (
        "历史中的 handoff/record-*.log 是过去读取时的路径，原日志不在本次工作区。"
        "若上下文被压缩，使用压缩标记中给出的实际归档路径，通过基础文件工具核对原对话。"
        "归档是历史快照，不能替代当前工作区文件。"
    )
    history.insert(0, {"role": "user", "content": environment})
    return {"spec": spec, "project": project, "prompt": prompt, "history": history,
            "expected": expected, "absent_keys": unknowns, "files": files}


def grade(workspace, case):
    result = grade_answer(workspace, case)
    changed_files = []
    for relative, original in case.get("files", {}).items():
        try:
            if (workspace / relative).read_text(encoding="utf-8") != original:
                changed_files.append(relative)
        except OSError:
            changed_files.append(relative)
    result["modified_input_files"] = changed_files
    result["passed"] = result["passed"] and not changed_files
    return result


def dataset_document():
    return {"name": "minicode-realistic-recall50", "version": DATASET_VERSION,
            "seed": DATASET_SEED, "description": "50 synthetic work-handoff conditions, 8 facts + 2 unknowns; not production samples.",
            "cases": build_specs()}


if __name__ == "__main__":
    DATASET.write_text(json.dumps(dataset_document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(DATASET)
