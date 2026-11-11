"""Human style benchmark; drafts and saved-response replay never buy inference."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
from typing import Literal, Optional

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.style_direct.cli import analyze_file
from app.style_direct.models import Dimension

TARGET_IMAGES, TARGET_VIDEOS = 12, 3
MAX_WINDOW_SECONDS, MAX_FRAMES = 4.8, 4


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_hash(gold):
    return hashlib.sha256(json.dumps(gold.model_dump(), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def media_path(root, filename):
    relative = Path(filename)
    if not filename or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("素材必须为安全相对路径")
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("素材缺失或路径越界：" + filename)
    return path


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldEvidence(Strict):
    frame_number: int = Field(ge=1, le=MAX_FRAMES)
    timestamp: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    description: str = Field(min_length=2)


class GoldFeature(Strict):
    dimension: Dimension
    observable: bool
    accepted_descriptions: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    evidence: list[GoldEvidence] = Field(default_factory=list, max_length=MAX_FRAMES)


class VideoWindow(Strict):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    frame_count: int = Field(default=MAX_FRAMES, ge=2, le=MAX_FRAMES)

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < self.end_seconds - self.start_seconds <= MAX_WINDOW_SECONDS + 1e-8:
            raise ValueError("视频采样窗口须大于0且不超过4.8秒")
        return self


class GoldSample(Strict):
    id: str = Field(min_length=1)
    source: Literal["human"]
    reviewer: str = Field(min_length=1)
    reviewed_at: str = ""
    annotation_signature: str = ""
    rights: Literal["owned", "licensed"]
    rights_reference: str = Field(min_length=1)
    media_file: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: Literal["cinema", "phone-documentary", "animation", "painting", "handmade", "mixed"]
    features: list[GoldFeature] = Field(min_length=1)
    media_type: Literal["image", "video"] = "image"
    video_window: Optional[VideoWindow] = None
    naming_expectation: Literal["named", "descriptive", "unreviewed"] = "unreviewed"
    expected_recipe_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def safe_path(self):
        path = Path(self.media_file)
        if not self.media_file or path.is_absolute() or ".." in path.parts:
            raise ValueError("素材必须为安全相对路径")
        if not self.reviewer.strip() or not self.rights_reference.strip():
            raise ValueError("人工审核者和授权来源不能仅含空白")
        if (self.media_type == "video") != (self.video_window is not None):
            raise ValueError("视频必须注明有限采样窗口，图片不能带视频窗口")
        if len(self.expected_recipe_ids) != len(set(self.expected_recipe_ids)):
            raise ValueError("人工参考配方ID重复")
        if self.naming_expectation == "named" and not self.expected_recipe_ids:
            raise ValueError("可命名样本须由人工填写接受的配方ID")
        if self.naming_expectation == "descriptive" and self.expected_recipe_ids:
            raise ValueError("仅描述样本不应同时指定必须命中的名称")
        for feature in self.features:
            for evidence in feature.evidence:
                if self.media_type == "image" and (evidence.frame_number != 1 or evidence.timestamp not in (None, 0)):
                    raise ValueError("单图证据只使用第1帧，不伪造动态时间")
                if self.video_window and (evidence.timestamp is None or
                        not self.video_window.start_seconds <= evidence.timestamp <= self.video_window.end_seconds or
                        evidence.frame_number > self.video_window.frame_count):
                    raise ValueError("视频人工证据须在窗口内并保留原视频时间")
        return self


class GoldSet(Strict):
    schema_version: Literal["style-direct-gold.v3", "style-direct-gold.v4"]
    samples: list[GoldSample] = Field(min_length=1)
    corpus_id: str = "legacy-v3"
    split: Literal["development", "acceptance"] = "development"
    target_images: Optional[int] = Field(default=None, ge=0)
    target_videos: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def reviewed_and_unique(self):
        if len({sample.id for sample in self.samples}) != len(self.samples):
            raise ValueError("样本ID重复")
        if self.schema_version == "style-direct-gold.v4":
            if not self.corpus_id.strip() or self.corpus_id == "legacy-v3":
                raise ValueError("v4必须填写独立素材集名称")
            if len({s.sha256 for s in self.samples}) != len(self.samples):
                raise ValueError("同一素材SHA-256不能重复占据多个v4样本")
            for sample in self.samples:
                if not sample.reviewed_at.strip() or not sample.annotation_signature.strip() or sample.naming_expectation == "unreviewed":
                    raise ValueError("v4样本尚未完成带时间与签名的人工标注：" + sample.id)
                for feature in sample.features:
                    if feature.observable and (not feature.accepted_descriptions or not feature.evidence):
                        raise ValueError("可观察人工特征必须注明描述及帧证据：" + sample.id)
        for kind, target in (("image", self.target_images), ("video", self.target_videos)):
            if target is not None and sum(s.media_type == kind for s in self.samples) != target:
                raise ValueError("素材数量不符合标注集目标：" + kind)
        return self


def inspect_media(path, kind):
    if kind == "image":
        with Image.open(path) as image:
            if image.format not in ("PNG", "JPEG", "WEBP", "GIF", "AVIF"):
                raise ValueError("图片格式不支持")
            if image.width * image.height > 30_000_000 or getattr(image, "n_frames", 1) != 1:
                raise ValueError("图片像素过大或不是静态单帧")
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("图片超过25MB")
            metadata = {"width": image.width, "height": image.height, "format": image.format}
            image.verify()
        with Image.open(path) as decoded:
            decoded.load()  # JPEG verify alone does not validate decoded pixels.
        return metadata
    import cv2  # Existing backend dependency; nothing is installed by this tool.
    capture = cv2.VideoCapture(str(path))
    try:
        fps, count = capture.get(cv2.CAP_PROP_FPS), capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or not math.isfinite(count) or count < 2:
            raise ValueError("无法读取视频时长或帧率")
        return {"duration_seconds": count / fps, "fps": fps, "frame_count": int(count),
                "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}
    finally:
        capture.release()


def sample_video(path, window, directory):
    """Decode at most four frames; preserve absolute source times and their basis."""
    import cv2
    from app.style_direct.service import prepare_image
    capture = cv2.VideoCapture(str(path))
    assets = []
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        for index in range(window.frame_count):
            requested = window.start_seconds + (window.end_seconds - window.start_seconds) * (index + .5) / window.frame_count
            capture.set(cv2.CAP_PROP_POS_MSEC, requested * 1000)
            success, frame = capture.read()
            if not success:
                raise ValueError("视频窗口内帧无法解码")
            timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            basis = "decoder-pos-msec"
            if not math.isfinite(timestamp) or (timestamp == 0 and requested > 1 / fps):
                timestamp = max(0, capture.get(cv2.CAP_PROP_POS_FRAMES) - 1) / fps
                basis = "decoded-frame-index/fps"
            if not window.start_seconds - 1e-6 <= timestamp <= window.end_seconds + 1e-6:
                raise ValueError("解码帧时间越出有限采样窗口")
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            assets.append(prepare_image(stream.getvalue(), directory / "input", "I" + str(index + 1),
                          {"timestamp": timestamp, "requested_timestamp": requested,
                           "timestamp_basis": basis, "origin_media_type": "video"}))
    finally:
        capture.release()
    if len({a["timestamp"] for a in assets}) != len(assets):
        raise ValueError("视频采样得到重复时间，不能冒充不同证据帧")
    return assets


def analyze_video(assets, *, live=False, directory):
    from app.config import load_settings
    from app.providers.bailian import BailianVisionProvider
    from app.providers.mock import MockVisionProvider
    from app.style_direct.pipeline import StylePipeline
    settings = replace(load_settings(prefer_private_cloud=True), cloud_model="qwen3.8-flash")
    provider = BailianVisionProvider(settings) if live else MockVisionProvider(settings)
    try:
        health = provider.health()
        if not health.ready:
            raise ValueError(health.detail)
        result = StylePipeline(provider, assets, directory / "crops").run()
        usage = provider.drain_usage() if hasattr(provider, "drain_usage") else []
        result.update(usage=usage, probe_mode="live" if live else "mock",
                      estimated_list_price_cny=sum(u.get("estimated_list_price_cny") or 0 for u in usage) if usage else None,
                      cost_estimate_complete=bool(usage) and all(u.get("usage_available") and u.get("estimated_list_price_cny") is not None for u in usage))
        from app.style_direct.diagnostics import attach_usage
        attach_usage(result, usage)
        return result
    finally:
        provider.unload()


def verify_media(gold, media_dir):
    root = Path(media_dir).resolve()
    verified = []
    for sample in gold.samples:
        path = media_path(root, sample.media_file)
        if file_hash(path) != sample.sha256:
            raise ValueError("素材SHA-256不一致：" + sample.id)
        verified.append((sample, path))
    for sample, path in verified:
        metadata = inspect_media(path, sample.media_type)
        if sample.video_window and sample.video_window.end_seconds > metadata["duration_seconds"] + 1e-6:
            raise ValueError("视频采样窗口超出原视频时长：" + sample.id)
    return verified


def prepare_draft(media_dir, images=None, videos=None, *, split="development", corpus_id="style-development-12i-3v"):
    root = Path(media_dir).resolve()
    images, videos = list(images or []), list(videos or [])
    if len(images) > TARGET_IMAGES or len(videos) > TARGET_VIDEOS or len(set(images + videos)) != len(images + videos):
        raise ValueError("准备入口最多12图/3视频，文件不能重复")
    samples = []
    for kind, files, count in (("image", images, TARGET_IMAGES), ("video", videos, TARGET_VIDEOS)):
        for index in range(count):
            filename = files[index] if index < len(files) else ""
            sample = {"id": kind + "-" + str(index + 1).zfill(2), "media_type": kind,
                      "media_file": filename, "sha256": None, "source": None, "reviewer": "",
                      "reviewed_at": "", "annotation_signature": "", "rights": None,
                      "rights_reference": "", "family": None, "features": [],
                      "naming_expectation": "unreviewed", "expected_recipe_ids": [], "video_window": None}
            if filename:
                path = media_path(root, filename)
                metadata = inspect_media(path, kind)
                sample.update(sha256=file_hash(path), media_metadata=metadata)
                if kind == "video":
                    sample["video_window"] = {"start_seconds": 0, "end_seconds": min(MAX_WINDOW_SECONDS, metadata["duration_seconds"]), "frame_count": MAX_FRAMES}
            samples.append(sample)
    return {"schema_version": "style-direct-gold-draft.v1", "corpus_id": corpus_id, "split": split,
            "target_images": TARGET_IMAGES, "target_videos": TARGET_VIDEOS, "annotation_status": "pending-human",
            "note": "不生成标签/授权。人工填完后删除草稿字段media_metadata、annotation_status、note并改为style-direct-gold.v4。有限视频窗口只供人工确认的静态预览，不证明运动。",
            "samples": samples}


def independence(gold, development_gold=None):
    if gold.split != "acceptance":
        return {"status": "development-only", "verified": False}
    if development_gold is None:
        return {"status": "independence-unverified", "verified": False}
    if development_gold.split != "development" or gold.corpus_id == development_gold.corpus_id:
        raise ValueError("验收集必须与开发集名称和用途分开")
    if {s.sha256 for s in gold.samples} & {s.sha256 for s in development_gold.samples}:
        raise ValueError("独立验收集与开发集包含相同素材SHA-256")
    return {"status": "hash-disjoint", "verified": True,
            "development_manifest_sha256": manifest_hash(development_gold),
            "note": "哈希无重叠不证明不存在同源裁剪或视觉近重复，仍需人工确认。"}


def load_replays(path, gold):
    data = json.loads(Path(path).read_text(encoding="utf8"))
    rows = data.get("samples", [])
    expected = {s.id: s.sha256 for s in gold.samples}
    if len(rows) != len(expected) or len({r.get("id") for r in rows}) != len(rows):
        raise ValueError("回放样本必须完整且不能重复")
    results = {}
    for row in rows:
        digest = row.get("media_sha256") or row.get("gold", {}).get("sha256")
        if row.get("id") not in expected or digest != expected[row["id"]]:
            raise ValueError("回放与人工素材SHA-256不一致")
        prediction = row.get("prediction")
        if not isinstance(prediction, dict) or not isinstance(prediction.get("profile"), dict):
            raise ValueError("回放缺少保存的画风分析结果")
        results[row["id"]] = prediction
    return results


def run(gold, media_dir, output, live=False, *, replay=None, development_gold=None):
    if live and replay:
        raise ValueError("离线回放不能同时使用--live")
    gold = GoldSet.model_validate(gold.model_dump())
    verified = verify_media(gold, media_dir)
    separate = independence(gold, development_gold)
    replays = load_replays(replay, gold) if replay else None
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录已有产物，请使用新目录以保留报告和人工评分")
    output.mkdir(parents=True, exist_ok=True)
    prepared, frozen = {}, []
    if not replays:
        sources = output / "verified-media"
        sources.mkdir()
        for index, (sample, path) in enumerate(verified, 1):
            snapshot = sources / ("source-%03d" % index + path.suffix)
            shutil.copyfile(path, snapshot)
            if file_hash(snapshot) != sample.sha256:
                raise ValueError("预检后素材发生变化，SHA-256不一致：" + sample.id)
            inspect_media(snapshot, sample.media_type)
            frozen.append((sample, snapshot))
        for index, (sample, path) in enumerate(frozen, 1):
            if sample.media_type == "video":
                prepared[sample.id] = sample_video(path, sample.video_window, output / ("sample-%03d" % index))
    items = []
    for index, (sample, path) in enumerate(verified if replays else frozen, 1):
        directory = output / ("sample-%03d" % index)
        if replays:
            result = replays[sample.id]
        elif sample.media_type == "video":
            result = analyze_video(prepared[sample.id], live=live, directory=directory)
        else:
            result = analyze_file(path, live=live, directory=directory)
        result.setdefault("source_media_sha256", sample.sha256)
        item = {"id": sample.id, "family": sample.family, "media_type": sample.media_type,
                "gold": sample.model_dump(), "prediction": result,
                "human_review": {"facts_accurate": None, "representative": None, "content_leakage": None,
                                 "unsupported_expansion": None, "prompt_usable": None, "comments": ""}}
        items.append(item)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "review.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf8")
        from app.style_direct.diagnostics import legacy_diagnostics, markdown_report
        (directory / "diagnostics.md").write_text(markdown_report(result.get("diagnostics") or legacy_diagnostics()), encoding="utf8")
    from evals.style_direct_review import technical_summary, review_draft
    mode = "replay" if replays else "live" if live else "mock"
    original_calls = sum(item["prediction"].get("budget", {}).get("model_requests", 0) for item in items)
    priced = [item["prediction"].get("estimated_list_price_cny") for item in items]
    historical_cost = sum(p for p in priced if p is not None) if any(p is not None for p in priced) else None
    summary = {"schema_version": "style-direct-eval.v4" if gold.schema_version.endswith("v4") else "style-direct-eval.v3",
               "mode": mode, "split": gold.split, "corpus_id": gold.corpus_id,
               "manifest_sha256": manifest_hash(gold), "sample_count": len(items), "independence": separate,
               "human_review_complete": False, "quality_scores": None,
               "model_requests": 0 if replays else original_calls, "new_paid_requests": original_calls if live else 0,
               "original_model_requests": original_calls,
               "estimated_list_price_cny": historical_cost if live else 0,
               "original_estimated_list_price_cny": historical_cost,
               "cost_estimate_complete": all(item["prediction"].get("cost_estimate_complete", False) for item in items) if live else True,
               "original_cost_estimate_complete": all(item["prediction"].get("cost_estimate_complete", False) for item in items),
               "note": "空白/Mock不计算真实准确率；回放0新调用，历史usage缺失不等于历史免费。静态抽帧不证明运动或帧率。"}
    report = {**summary, "samples": items, "technical": technical_summary(items)}
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    (output / "human-review.json").write_text(json.dumps(review_draft(report, file_hash(report_path)), ensure_ascii=False, indent=2), encoding="utf8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    (output / "summary.md").write_text(
        "# 画风基准运行报告\n\n模式：" + mode + "；样本：" + str(len(items)) +
        "；新增付费请求：" + str(summary['new_paid_requests']) +
        "。\n\n人工评审未完成：不输出真实准确率。\n\n" +
        "逐样本诊断见 sample-NNN/diagnostics.md；填写 human-review.json 后再汇总。\n" +
        "名称不一致、缺少命名及耗时技术统计保存在 report.json，不当作视觉准确率。\n", encoding="utf8")
    (output / "README.md").write_text("# 画风人工评测\n\n填写 human-review.json 的签名和逐样本评分后使用 evals.style_direct_review 汇总。\n空白/Mock不算真实准确率；回放不产生新请求。视频只分析有限窗口的静态帧。\n", encoding="utf8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="授权人工画风基准；默认Mock，--live才产生API费用")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true", help="准备12图/3视频空槽位或文件清单，不填写真值")
    action.add_argument("--validate", action="store_true", help="只校验人工清单、整集素材与开发/验收关系")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay", type=Path, help="保存report.json或样本ID/hash/prediction离线清单")
    parser.add_argument("--development-manifest", type=Path)
    parser.add_argument("--images", nargs="*", default=[])
    parser.add_argument("--videos", nargs="*", default=[])
    parser.add_argument("--split", choices=("development", "acceptance"), default="development")
    parser.add_argument("--corpus-id", default="style-development-12i-3v")
    args = parser.parse_args(argv)
    if (args.prepare or args.validate) and (args.live or args.replay):
        parser.error("准备/校验不会调用模型，不能附带--live或--replay")
    if args.prepare:
        if not args.output:
            parser.error("--prepare需要--output")
        result = prepare_draft(args.media_dir, args.images, args.videos, split=args.split, corpus_id=args.corpus_id)
        if args.output.exists():
            parser.error("草稿输出已存在，请使用新路径以保留人工标注")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    else:
        if not args.manifest:
            parser.error("运行/校验需要--manifest")
        gold = GoldSet.model_validate_json(args.manifest.read_text(encoding="utf8"))
        development = GoldSet.model_validate_json(args.development_manifest.read_text(encoding="utf8")) if args.development_manifest else None
        if args.validate:
            verify_media(gold, args.media_dir)
            result = {"status": "validated", "sample_count": len(gold.samples), "split": gold.split,
                      "manifest_sha256": manifest_hash(gold), "new_paid_requests": 0, "independence": independence(gold, development)}
        else:
            if not args.output:
                parser.error("运行需要--output")
            result = run(gold, args.media_dir, args.output, args.live, replay=args.replay, development_gold=development)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
