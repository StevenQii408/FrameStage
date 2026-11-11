import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.memory_guard import MemoryGuard
from app.providers.base import FrameInput, ShotContext
from app.providers.mock import MockVisionProvider
from app.services.video_processor import DetectedShot, detect_scenes, extract_frames, probe_video
from evals.manifest import ManifestError, load_manifest, resolve_clips
from evals.scoring import score_observation, score_transitions


THRESHOLDS = {
    "hard_cut_f1": 0.90,
    "frame_success_rate": 1.0,
    "black_frame_rate_max": 0.05,
    "blur_frame_rate_max": 0.10,
    "vlm_parse_rate": 1.0,
    "action_non_unknown_rate": 0.90,
    "shot_size_adjacent_accuracy": 0.85,
    "movement_accuracy": 0.80,
    "lens_accuracy": 0.75,
    "confidence_rule_rate": 1.0,
    "minimum_available_memory_gb": 16.0,
}


def _ratio(numerator: int, denominator: int, empty: Optional[float] = None) -> Optional[float]:
    return numerator / denominator if denominator else empty


def _dependency_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_revision(root: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted"


def _frame_quality(path: Path) -> Dict[str, Any]:
    try:
        import cv2

        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("OpenCV 无法读取图像")
        brightness = float(image.mean())
        blur_score = float(cv2.Laplacian(image, cv2.CV_64F).var())
        return {"readable": True, "brightness": brightness, "blur_score": blur_score, "black": brightness < 10.0, "blurred": blur_score < 50.0}
    except Exception as exc:
        return {"readable": False, "brightness": None, "blur_score": None, "black": False, "blurred": False, "error": str(exc)}


def _provider(name: str, settings):
    if name == "mock":
        return MockVisionProvider(settings)
    if name == "bailian":
        return BailianVisionProvider(settings)
    raise ValueError("provider 必须是 mock 或 bailian；本地模型推理已停用")


def _transitions(clip: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"time": float(shot["start_time"]), "type": shot["transition_in"], "shot_number": shot["number"]}
        for shot in clip["shots"][1:]
    ]


def _aggregate(report: Dict[str, Any], settings) -> None:
    boundary_tp = boundary_fp = boundary_fn = 0
    dissolve_expected = dissolve_detected = 0
    expected_frames = generated_frames = readable_frames = black_frames = blurred_frames = 0
    attempted = successful = 0
    action_observable = action_non_unknown = 0
    size_observable = size_correct = 0
    movement_observable = movement_correct = 0
    lens_observable = lens_correct = 0
    confidence_total = confidence_pass = 0
    elapsed_values: List[float] = []
    memory_values: List[float] = []
    failure_count = 0
    montage_labeled = montage_relation_correct = montage_scene_correct = 0

    for clip in report["clips"]:
        boundary = clip["video_metrics"]["transitions"]
        boundary_tp += boundary["tp"]
        boundary_fp += boundary["fp"]
        boundary_fn += boundary["fn"]
        dissolve_expected += boundary["by_type"]["dissolve"]["expected"]
        dissolve_detected += boundary["by_type"]["dissolve"]["detected"]
        frames = clip["video_metrics"]["frames"]
        expected_frames += frames["expected"]
        generated_frames += frames["generated"]
        readable_frames += frames["readable"]
        black_frames += frames["black"]
        blurred_frames += frames["blurred"]
        failure_count += len(clip.get("errors", []))
        for relation in clip.get("montage_metrics", {}).get("label_scores", []):
            montage_labeled += 1
            montage_relation_correct += int(relation["relation_correct"])
            montage_scene_correct += int(relation["scene_change_correct"])
        for shot in clip["model_metrics"]:
            attempted += 1
            if shot.get("error"):
                failure_count += 1
                continue
            successful += 1
            elapsed_values.append(float(shot["elapsed_seconds"]))
            for key in ("memory_before_gb", "memory_after_gb"):
                if shot.get(key) is not None:
                    memory_values.append(float(shot[key]))
            score = shot["score"]
            if score["observable"]["subject_action"]:
                action_observable += 1
                action_non_unknown += int(not score["unknown"]["subject_action"])
            if score["observable"]["shot_size"]:
                size_observable += 1
                size_correct += int(score["shot_size_adjacent"])
            if score["observable"]["camera_movement"]:
                movement_observable += 1
                movement_correct += int(score["movement_correct"])
            if score["observable"]["lens"]:
                lens_observable += 1
                lens_correct += int(score["lens_correct"])
            confidence_total += 1
            confidence_pass += int(score["confidence_rule_pass"])

    precision = _ratio(boundary_tp, boundary_tp + boundary_fp, 1.0)
    recall = _ratio(boundary_tp, boundary_tp + boundary_fn, 1.0)
    hard_cut_f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else 0.0
    metrics = {
        "hard_cut": {"tp": boundary_tp, "fp": boundary_fp, "fn": boundary_fn, "precision": precision, "recall": recall, "f1": hard_cut_f1},
        "dissolve": {"expected": dissolve_expected, "detected": dissolve_detected, "recall": _ratio(dissolve_detected, dissolve_expected)},
        "frames": {
            "expected": expected_frames,
            "generated": generated_frames,
            "readable": readable_frames,
            "success_rate": _ratio(readable_frames, expected_frames, 0.0),
            "black_rate": _ratio(black_frames, readable_frames, 0.0),
            "blur_rate": _ratio(blurred_frames, readable_frames, 0.0),
        },
        "model": {
            "attempted": attempted,
            "successful": successful,
            "parse_rate": _ratio(successful, attempted, 0.0),
            "action_non_unknown_rate": _ratio(action_non_unknown, action_observable),
            "shot_size_adjacent_accuracy": _ratio(size_correct, size_observable),
            "movement_accuracy": _ratio(movement_correct, movement_observable),
            "lens_accuracy": _ratio(lens_correct, lens_observable),
            "confidence_rule_rate": _ratio(confidence_pass, confidence_total, 0.0),
            "elapsed_seconds": {
                "maximum": max(elapsed_values) if elapsed_values else None,
                "mean": sum(elapsed_values) / len(elapsed_values) if elapsed_values else None,
            },
            "minimum_available_memory_gb": min(memory_values) if memory_values else None,
        },
        "montage": {
            "labeled_relations": montage_labeled,
            "relation_accuracy": _ratio(montage_relation_correct, montage_labeled),
            "scene_change_accuracy": _ratio(montage_scene_correct, montage_labeled),
        },
        "failure_count": failure_count,
    }
    checks = {
        "hard_cut_f1": hard_cut_f1 >= THRESHOLDS["hard_cut_f1"],
        "frame_success_rate": metrics["frames"]["success_rate"] >= THRESHOLDS["frame_success_rate"],
        "black_frame_rate": metrics["frames"]["black_rate"] <= THRESHOLDS["black_frame_rate_max"],
        "blur_frame_rate": metrics["frames"]["blur_rate"] <= THRESHOLDS["blur_frame_rate_max"],
        "vlm_parse_rate": metrics["model"]["parse_rate"] >= THRESHOLDS["vlm_parse_rate"],
        "action_non_unknown_rate": metrics["model"]["action_non_unknown_rate"] is not None and metrics["model"]["action_non_unknown_rate"] >= THRESHOLDS["action_non_unknown_rate"],
        "shot_size_adjacent_accuracy": metrics["model"]["shot_size_adjacent_accuracy"] is not None and metrics["model"]["shot_size_adjacent_accuracy"] >= THRESHOLDS["shot_size_adjacent_accuracy"],
        "movement_accuracy": metrics["model"]["movement_accuracy"] is not None and metrics["model"]["movement_accuracy"] >= THRESHOLDS["movement_accuracy"],
        "lens_accuracy": metrics["model"]["lens_accuracy"] is not None and metrics["model"]["lens_accuracy"] >= THRESHOLDS["lens_accuracy"],
        "confidence_rule": metrics["model"]["confidence_rule_rate"] >= THRESHOLDS["confidence_rule_rate"],
        "memory_floor": report["environment"]["provider"] == "bailian" or (metrics["model"]["minimum_available_memory_gb"] is not None and metrics["model"]["minimum_available_memory_gb"] >= THRESHOLDS["minimum_available_memory_gb"]),
        "shot_timeout": metrics["model"]["elapsed_seconds"]["maximum"] is not None and metrics["model"]["elapsed_seconds"]["maximum"] <= settings.vlm_timeout_seconds,
        "no_failures": failure_count == 0,
    }
    report["summary"] = metrics
    report["acceptance"] = {"passed": all(checks.values()), "checks": checks, "thresholds": THRESHOLDS}


def _format_metric(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    checks = report["acceptance"]["checks"]
    lines = [
        f"# 真实素材基准报告：{report['corpus_id']}",
        "",
        f"- 运行时间：{report['created_at']}",
        f"- Provider：{report['environment']['provider']} / {report['environment']['model']}",
        f"- 样本：{len(report['clips'])} 条视频，{summary['model']['attempted']} 个人工镜头",
        f"- 总体结论：{'通过' if report['acceptance']['passed'] else '未达到全部门槛（基线已生成）'}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 | 门槛 | 状态 |",
        "| --- | ---: | ---: | :---: |",
        f"| 硬切 F1 | {_format_metric(summary['hard_cut']['f1'])} | ≥ 90% | {'✓' if checks['hard_cut_f1'] else '✗'} |",
        f"| 关键帧成功率 | {_format_metric(summary['frames']['success_rate'])} | 100% | {'✓' if checks['frame_success_rate'] else '✗'} |",
        f"| 黑帧率 | {_format_metric(summary['frames']['black_rate'])} | ≤ 5% | {'✓' if checks['black_frame_rate'] else '✗'} |",
        f"| 模糊帧率 | {_format_metric(summary['frames']['blur_rate'])} | ≤ 10% | {'✓' if checks['blur_frame_rate'] else '✗'} |",
        f"| VLM 解析成功率 | {_format_metric(summary['model']['parse_rate'])} | 100% | {'✓' if checks['vlm_parse_rate'] else '✗'} |",
        f"| 动作非未知率 | {_format_metric(summary['model']['action_non_unknown_rate'])} | ≥ 90% | {'✓' if checks['action_non_unknown_rate'] else '✗'} |",
        f"| 景别相邻准确率 | {_format_metric(summary['model']['shot_size_adjacent_accuracy'])} | ≥ 85% | {'✓' if checks['shot_size_adjacent_accuracy'] else '✗'} |",
        f"| 运动准确率 | {_format_metric(summary['model']['movement_accuracy'])} | ≥ 80% | {'✓' if checks['movement_accuracy'] else '✗'} |",
        f"| 焦段估计准确率 | {_format_metric(summary['model']['lens_accuracy'])} | ≥ 75% | {'✓' if checks['lens_accuracy'] else '✗'} |",
        f"| 置信度规则 | {_format_metric(summary['model']['confidence_rule_rate'])} | 100% | {'✓' if checks['confidence_rule'] else '✗'} |",
        f"| 跨镜关系准确率 | {_format_metric(summary['montage']['relation_accuracy'])} | 人工参考 | — |",
        f"| 场次变化准确率 | {_format_metric(summary['montage']['scene_change_accuracy'])} | 人工参考 | — |",
        "",
        "## 失败样本",
        "",
    ]
    failures = []
    for clip in report["clips"]:
        failures.extend(f"- `{clip['id']}`：{error}" for error in clip.get("errors", []))
        for shot in clip["model_metrics"]:
            if shot.get("error"):
                failures.append(f"- `{clip['id']}` SH {shot['number']:02d}：{shot['error']}")
            elif not all((shot["score"]["confidence_rule_pass"], shot["score"]["shot_size_adjacent"], shot["score"]["lens_correct"], shot["score"]["movement_correct"])):
                failures.append(f"- `{clip['id']}` SH {shot['number']:02d}：字段准确度或置信度规则未通过")
    lines.extend(failures or ["- 无"])
    evidence = report.get("perception_summary")
    if evidence:
        lines.extend(["", "## Flash 证据基线（技术指标，不代替人工准确率）", "",
                      f"- 模型请求数：{evidence['requests']}；输入/输出 Token：{evidence['prompt_tokens']}/{evidence['completion_tokens']}",
                      f"- 人民币目录价估算：{evidence['estimated_list_price_cny']}；费用统计完整：{evidence['cost_estimate_complete']}",
                      f"- 首次结构解析率：{_format_metric(evidence['first_response_parse_rate'])}",
                      f"- 纠错后双阶段成功率：{_format_metric(evidence['post_repair_stage_rate'])}",
                      f"- 关键事实引用覆盖：{_format_metric(evidence['fact_evidence_reference_coverage'])}",
                      f"- 无需补充调用：{_format_metric(evidence['without_supplement_rate'])}",
                      f"- 预算合规：{_format_metric(evidence['budget_compliance_rate'])}",
                      "- 人工动作、物体/场景、空间准确率及幻觉率：待人工评审。零样本不视为达标。"])
    lines.extend(["", "## 下一轮优先级", "", "1. 先处理影响样本数最多的失败类别。", "2. 切镜、抽帧和 VLM 分层优化，每次只改变一个变量。", "3. 使用同一 manifest 重跑并对比 JSON 报告，达到门槛后再进入生产编辑功能。", ""])
    return "\n".join(lines)


def run_benchmark(manifest_path: Path, media_dir: Path, output_dir: Path, provider_name: str, model_name: Optional[str] = None, observation_version: Optional[str] = None) -> Dict[str, Any]:
    manifest = load_manifest(manifest_path)
    clips = resolve_clips(manifest, media_dir)
    settings = replace(load_settings(), provider=provider_name, **({"cloud_model": model_name} if model_name else {}))
    if observation_version is not None:
        if observation_version not in ("v1", "v2"):
            raise ValueError("observation_version 必须为 v1 或 v2")
        settings = replace(settings, perception_v2_enabled=observation_version == "v2")
    provider = _provider(provider_name, settings)
    health = provider.health()
    if not health.ready:
        raise RuntimeError(health.detail or f"{provider_name} provider 未就绪")
    output_dir.mkdir(parents=True, exist_ok=True)
    memory = MemoryGuard(0, 0)
    project_root = Path(__file__).resolve().parents[2]
    report: Dict[str, Any] = {
        "schema_version": "storyboard-eval-report.v1",
        "corpus_id": manifest["corpus_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "provider": health.provider,
            "model": health.model,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_revision": _git_revision(project_root),
            "dependencies": {name: _dependency_version(name) for name in ("httpx", "scenedetect", "opencv-python", "Pillow")},
            "settings": {
                "frames_per_shot": settings.frames_per_shot,
                "max_frames_per_shot": settings.max_frames_per_shot,
                "frame_max_long_edge": settings.frame_max_long_edge,
                "vlm_max_context_tokens": settings.vlm_max_context_tokens,
                "vlm_max_new_tokens": settings.vlm_max_new_tokens,
                "vlm_timeout_seconds": settings.vlm_timeout_seconds,
                "perception_v2_enabled": settings.perception_v2_enabled,
            },
        },
        "clips": [],
    }
    try:
        for clip in clips:
            clip_result: Dict[str, Any] = {"id": clip["id"], "tags": clip.get("tags", []), "video_metrics": {}, "model_metrics": [], "errors": []}
            try:
                metadata = probe_video(clip["_path"])
                detected, warnings = detect_scenes(clip["_path"], metadata, settings.max_shots)
                predicted_boundaries = [shot.start_time for shot in detected[1:]]
                transition_score = score_transitions(predicted_boundaries, _transitions(clip))
                clip_result["video_metrics"] = {"duration": metadata["duration"], "detected_shots": len(detected), "warnings": warnings, "transitions": transition_score}
            except Exception as exc:
                clip_result["errors"].append(f"视频分析失败：{exc}")
                clip_result["video_metrics"] = {"duration": None, "detected_shots": 0, "warnings": [], "transitions": score_transitions([], _transitions(clip))}

            expected_frames = generated_frames = readable_frames = black_frames = blurred_frames = 0
            cloud_shots = []
            cloud_frames = {}
            for shot_spec in clip["shots"]:
                shot = DetectedShot(shot_spec["number"], float(shot_spec["start_time"]), float(shot_spec["end_time"]))
                expected_frames += min(settings.frames_per_shot, settings.max_frames_per_shot)
                shot_output = output_dir / "artifacts" / clip["id"]
                try:
                    frames = extract_frames(clip["_path"], [shot], shot_output, settings)
                    generated_frames += len(frames)
                    for frame in frames:
                        quality = _frame_quality(Path(frame.path))
                        readable_frames += int(quality["readable"])
                        black_frames += int(quality["black"])
                        blurred_frames += int(quality["blurred"])
                    before = memory.snapshot().get("available_gb")
                    started = time.monotonic()
                    extra_frames = []
                    observation = provider.analyze_shot(
                        [FrameInput(frame.id, frame.timestamp, frame.path, frame.width, frame.height) for frame in frames],
                        ShotContext(shot_id=f"{clip['id']}-shot-{shot.number:03d}", number=shot.number,
                                    start_time=shot.start_time, end_time=shot.end_time, script_text="",
                                    video_path=str(clip["_path"]), artifact_dir=str(shot_output / "evidence" / str(shot.number)),
                                    register_frame=extra_frames.append),
                    )
                    elapsed = time.monotonic() - started
                    after = memory.snapshot().get("available_gb")
                    prediction = observation.model_dump()
                    score = score_observation(shot_spec["labels"], prediction)
                    if provider_name == "bailian":
                        shot_id = f"{clip['id']}-shot-{shot.number:03d}"
                        cloud_shots.append({"id": shot_id, "number": shot.number, "start_time": shot.start_time,
                                            "end_time": shot.end_time, "observation": prediction})
                        cloud_frames[shot_id] = [FrameInput(frame.id, frame.timestamp, frame.path) for frame in frames]
                    record = getattr(observation, "perception_record", None)
                    clip_result["model_metrics"].append({
                        "number": shot.number, "elapsed_seconds": elapsed, "memory_before_gb": before,
                        "memory_after_gb": after, "prediction": prediction, "score": score,
                        "needs_review": bool(observation.analysis_warnings),
                        "unknown_field_rate": sum(score["unknown"].values()) / len(score["unknown"]),
                        "perception": record.model_dump() if record else None,
                        "evidence_artifacts": [{"id": frame.id, "timestamp": frame.timestamp, "path": frame.path,
                                                "width": frame.width, "height": frame.height}
                                               for frame in [*frames, *extra_frames]],
                    })
                except Exception as exc:
                    clip_result["model_metrics"].append({"number": shot.number, "error": str(exc)})
            clip_result["video_metrics"]["frames"] = {"expected": expected_frames, "generated": generated_frames, "readable": readable_frames, "black": black_frames, "blurred": blurred_frames}
            if provider_name == "bailian" and len(cloud_shots) == len(clip["shots"]) and len(cloud_shots) > 1:
                try:
                    montage = provider.analyze_montage(cloud_shots, cloud_frames)
                    labels = clip.get("montage_labels", [])
                    predicted = {int(item["right_shot_id"].rsplit("-", 1)[-1]): item for item in montage["relations"]}
                    scored = [
                        {"right_shot": label["right_shot"],
                         "relation_correct": predicted.get(label["right_shot"], {}).get("relation_type") == label["relation_type"],
                         "scene_change_correct": predicted.get(label["right_shot"], {}).get("scene_change") == label["scene_change"]}
                        for label in labels
                    ]
                    clip_result["montage_metrics"] = {**montage, "label_scores": scored}
                except Exception as exc:
                    clip_result["errors"].append(f"跨镜关系分析失败：{exc}")
            elif provider_name == "bailian" and len(cloud_shots) != len(clip["shots"]):
                clip_result["errors"].append("逐镜分析不完整，跨镜关系保持待复核")
            report["clips"].append(clip_result)
    finally:
        drain = getattr(provider, "drain_usage", None)
        report["model_usage"] = drain() if callable(drain) else []
        provider.unload()
    for clip in report["clips"]:
        for shot in clip["model_metrics"]:
            shot_id = f"{clip['id']}-shot-{shot['number']:03d}"
            shot["model_usage"] = [entry for entry in report["model_usage"] if entry.get("shot_id") == shot_id]
            costs = [entry.get("estimated_list_price_cny") for entry in shot["model_usage"]]
            shot["estimated_list_price_cny"] = round(sum(cost for cost in costs if cost is not None), 6) if costs else None
            shot["cost_estimate_complete"] = bool(costs) and all(cost is not None for cost in costs)
    _aggregate(report, settings)
    from evals.perception_review import technical_summary
    report["perception_summary"] = technical_summary(report)
    if provider_name == "bailian" and settings.perception_v2_enabled:
        technical = report["perception_summary"]
        checks = report["acceptance"]["checks"]
        checks.update({
            "v2_all_shots": technical["v2_shots"] == report["summary"]["model"]["attempted"] > 0,
            "v2_stages_after_repair": technical["post_repair_stage_rate"] == 1.0,
            "v2_evidence_references": technical["fact_evidence_reference_coverage"] == 1.0,
            "v2_without_supplement": technical["without_supplement_rate"] is not None and technical["without_supplement_rate"] >= 0.8,
            "v2_budget_compliance": technical["budget_compliance_rate"] == 1.0,
        })
        report["technical_acceptance"] = {"passed": all(checks.values()), "checks": dict(checks)}
        checks["human_review_complete"] = False
        report["acceptance"]["passed"] = False  # technical structure alone never passes semantic quality
        report["acceptance"]["human_quality_status"] = "pending"
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 AI 分镜真实素材质量基准")
    parser.add_argument("--manifest", type=Path, required=True, help="storyboard-eval.v1 manifest 路径")
    parser.add_argument("--media-dir", type=Path, help="外置素材目录；默认读取 STORYBOARD_EVAL_MEDIA_DIR")
    parser.add_argument("--output-dir", type=Path, default=Path("eval-results"), help="报告与临时关键帧目录")
    parser.add_argument("--provider", choices=("mock", "bailian"), default="bailian")
    parser.add_argument("--model", help="仅 bailian：固定模型 ID，不修改生产默认值")
    parser.add_argument("--observation-version", choices=("v1", "v2"), help="v1 用于先建立旧结构 Flash 基线，v2 用于证据结构评测")
    parser.add_argument("--strict", action="store_true", help="任何验收项失败时返回非零退出码")
    args = parser.parse_args(argv)
    media_dir = args.media_dir or (Path(os.environ["STORYBOARD_EVAL_MEDIA_DIR"]) if os.getenv("STORYBOARD_EVAL_MEDIA_DIR") else None)
    if media_dir is None:
        parser.error("请传入 --media-dir 或设置 STORYBOARD_EVAL_MEDIA_DIR")
    try:
        report = run_benchmark(args.manifest, media_dir, args.output_dir, args.provider, args.model, args.observation_version)
    except (ManifestError, RuntimeError, OSError) as exc:
        parser.exit(2, f"评测无法启动：{exc}\n")
    stem = f"{report['corpus_id']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    json_path = args.output_dir / f"{stem}.json"
    markdown_path = args.output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print(f"Acceptance: {'PASS' if report['acceptance']['passed'] else 'BASELINE GENERATED / THRESHOLDS NOT MET'}")
    return 1 if args.strict and not report["acceptance"]["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
