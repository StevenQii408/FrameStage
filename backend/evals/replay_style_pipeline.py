"""Re-run local validation/rendering over saved responses, with no cloud provider."""
import argparse
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from app.media_paths import MediaPaths
from app.style_direct.pipeline import StylePipeline


class SavedResponseProvider:
    def __init__(self, result):
        self.responses = iter(result.get('raw_responses', []))
        self.model = result.get('profile', {}).get('provenance', {}).get('model', 'legacy-unknown')
        self.consumed = 0

    def health(self):
        return SimpleNamespace(model=self.model)

    def analyze_style(self, prompt, frames, *, phase, schema, **options):
        try:
            row = next(self.responses)
        except StopIteration:
            raise ValueError('没有对应的历史响应；离线回放不会请求云端') from None
        if row.get('phase') != phase or row.get('images') != [f.id for f in frames]:
            raise ValueError('请求阶段或证据帧与历史不一致；停止回放，不请求云端')
        self.consumed += 1
        if 'response' not in row:
            raise RuntimeError(row.get('error') or '历史请求未保存响应')
        return row['response']


def replay_saved(database, analysis_id, output_dir):
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError('数据库不存在')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT status,source_json,result_json FROM style_analyses WHERE id=?', (analysis_id,)).fetchone()
    if not row or not row[2]:
        raise ValueError('未找到已保存的画风响应')
    source, old = json.loads(row[1]), json.loads(row[2])
    media = MediaPaths(database.parent)
    source['assets'] = [media.record(asset, reading=True) for asset in source['assets']]
    provider = SavedResponseProvider(old)
    result = StylePipeline(provider, source['assets'], Path(output_dir) / 'crops', scope=source['scope']).run()
    result['assets'] = [{k: v for k, v in a.items() if k not in ('path', 'original_path')} for a in result['assets']]
    result['replay'] = {'analysis_id': analysis_id, 'database_access': 'read-only',
        'old_status': row[0], 'old_description_count': len(old.get('profile', {}).get('descriptions', [])),
        'saved_responses_consumed': provider.consumed, 'new_paid_requests': 0,
        'note': '仅验证工程链路，不代表重新推理或人工画风准确率验收'}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读回放保存响应，不调用云端、不修改历史任务')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--analysis-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('输出文件已存在，请使用新路径')
    result = replay_saved(args.database, args.analysis_id, args.output.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'replay': result['replay'], 'quality_state': result['quality_state'],
                      'rendered_prompt': result['profile']['rendered_prompt']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
