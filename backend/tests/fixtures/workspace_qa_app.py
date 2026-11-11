"""Local QA only. Never imported by the project or any production provider."""
import io
import os
import tempfile
from pathlib import Path
import threading
import subprocess
from dataclasses import replace

from PIL import Image
if os.environ.get('STORYBOARD_PROVIDER') != 'mock':
    raise RuntimeError('QA fixture requires STORYBOARD_PROVIDER=mock')
qa_data=Path(os.environ.get('STORYBOARD_DATA_DIR','')).resolve()
temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
if not any(qa_data.is_relative_to(root) and qa_data != root for root in temporary_roots):
    raise RuntimeError('QA fixture requires an explicit data directory inside the system temporary directory')
from app.main import app
from app.generation.providers import DisabledGenerationProvider, GeneratedAsset, ProviderResult

QA_FAILURES = {
    'test-fail-capacity': ('provider_capacity_exhausted', True),
    'test-fail-rate': ('rate_limited', True),
    'test-fail-balance': ('provider_balance_insufficient', False),
    'test-fail-content': ('content_rejected', False),
    'test-fail-auth': ('provider_authentication_failed', False),
    'test-fail-timeout': ('provider_timeout', True),
    'test-fail-unknown': ('qa_unknown_vendor_error', False),
}


class LocalTestProvider:
    name = 'local-qa-only'
    configured = True

    def __init__(self, failure=None):
        self.progress = {}
        self.requests = {}
        self.lock = threading.Lock()
        self.failure = failure

    def capabilities(self):
        values = dict(available=True, aspect_ratios=['16:9'], resolutions=['720p'],
                      supports_reference=True, max_reference_images=8,
                      min_duration_seconds=1, max_duration_seconds=12,
                      models=[dict(id='qa-image', label='本地 QA 测试模型', aspect_ratios=['16:9'], resolutions=['720p'])])
        return dict(configured=True, provider=self.name, reason='', image=values,
                    video={**values, 'models': [dict(id='qa-video', label='本地 QA 视频模型', aspect_ratios=['16:9'], resolutions=['720p'])]})

    def submit(self, request, *, idempotency_key):
        with self.lock:
            self.progress[idempotency_key] = 0
            self.requests[idempotency_key] = request
        return idempotency_key

    def poll(self, task_id):
        with self.lock:
            value = self.progress[task_id] + 20
            self.progress[task_id] = value
        if self.failure and value >= 40:
            code, retryable = self.failure
            return ProviderResult('failed', error_code=code, retryable=retryable,
                                  message='QA_UPSTREAM_DETAIL_MUST_NOT_BE_PUBLIC')
        if value < 100:
            return ProviderResult('running', progress=value)
        request = self.requests[task_id]
        if request['kind'] == 'video':
            path = qa_data / (task_id + '.qa.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                f"testsrc2=size=1280x720:rate=5:duration={request['settings']['duration_seconds']}",
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)],
                check=True, capture_output=True, timeout=15)
            return ProviderResult('succeeded', assets=[GeneratedAsset(path.read_bytes(), 'video/mp4')])
        output = io.BytesIO()
        Image.new('RGB', (1280, 720), (45, 95, 110)).save(output, 'PNG')
        return ProviderResult('succeeded', progress=100, assets=[GeneratedAsset(output.getvalue(), 'image/png')])

    def cancel(self, task_id):
        pass


@app.post('/__qa/provider/{mode}')
def set_provider(mode: str):
    enabled = mode == 'test' or mode in QA_FAILURES
    app.state.generation_service.provider = LocalTestProvider(QA_FAILURES.get(mode)) if enabled else DisabledGenerationProvider()
    if enabled:
        # Only this explicit, temporary, mock-only app accelerates cloud waiting.
        app.state.generation_service.config = replace(app.state.generation_service.config,
            poll_interval_seconds=0.2, image_submit_rpm=600, video_submit_rpm=600)
    return {'mode': mode}
