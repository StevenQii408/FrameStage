"""CPU-only image evidence helpers shared by cloud analysis. No local inference."""
from pathlib import Path
from typing import List

from app.providers.base import FrameInput
from app.providers.vision_prompts import VisionPromptBuilder
from app.providers.vision_responses import VisionResponseParser


class VisionAnalysisTools(VisionPromptBuilder, VisionResponseParser):
    @staticmethod
    def _contact_sheets(frames: List[FrameInput], output_dir: Path) -> List[str]:
        from PIL import Image, ImageDraw  # noqa: PLC0415

        output_dir.mkdir(parents=True, exist_ok=True)
        results: List[str] = []
        for sheet_index in range(0, len(frames), 6):
            batch = frames[sheet_index : sheet_index + 6]
            canvas = Image.new("RGB", (960, 400), "#111318")
            draw = ImageDraw.Draw(canvas)
            for local_index, frame in enumerate(batch):
                with Image.open(frame.path) as source:
                    image = source.convert("RGB")
                    image.thumbnail((300, 160), Image.Resampling.LANCZOS)
                    column, row = local_index % 3, local_index // 3
                    x, y = column * 320 + 10, row * 200 + 28
                    canvas.paste(image, (x + (300 - image.width) // 2, y + (160 - image.height) // 2))
                    draw.text((x, 8 + row * 200), f"{frame.id} | {frame.timestamp:.3f}s", fill="white")
            output = output_dir / f"overview-{sheet_index // 6 + 1:02d}.jpg"
            canvas.save(output, "JPEG", quality=84)
            results.append(str(output))
        return results

    @staticmethod
    def _sharpest_image(image_paths: List[str]) -> str:
        try:
            import cv2  # noqa: PLC0415

            scored = []
            for path in image_paths:
                image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
                score = float(cv2.Laplacian(image, cv2.CV_64F).var()) if image is not None else -1.0
                scored.append((score, path))
            return max(scored)[1]
        except Exception:
            return image_paths[len(image_paths) // 2]
