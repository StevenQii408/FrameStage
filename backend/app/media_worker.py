"""Private local decoder entry point, killed with its process group on cancellation."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from app.services.video_processor import process_video


def main():
    # Nested FFmpeg processes remain inside this worker's parent-owned group.
    os.environ["STORYBOARD_MEDIA_WORKER"] = "1"
    video, output, settings = sys.argv[1:]
    result = process_video(Path(video), Path(output), SimpleNamespace(**json.loads(settings)))
    print(json.dumps(asdict(result), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
