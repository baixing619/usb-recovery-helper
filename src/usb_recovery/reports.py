"""CreateNew static Chinese reports; no formulas, scripts or remote resources."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import html
import io
import json
from pathlib import Path
import uuid

from .engine.exfat import _output_tree
from .engine.image import no_reparse

COUNT_KEYS = ('structure_checked', 'repaired_candidate', 'partial', 'unverified', 'historical', 'temporary')
LABELS = dict(structure_checked='结构检查通过', repaired_candidate='修复候选', partial='残缺',
              unverified='未确认', historical='历史候选', temporary='临时或系统文件')
SCOPE = ('结构检查不代表内容或业务完整。未进行原生 WPS/Excel 打开验收、公式重算、宏执行或业务核对。'
         '历史条目及深搜候选的原名、版本、时间与内容归属可能未知；未读范围不能当作真实零数据。')


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False).encode('utf-8')


def category(record):
    validation = record.get('validation_status', record.get('status', 'unverified'))
    if validation == 'temporary':
        return 'temporary'
    if str(record.get('origin', '')).startswith('historical'):
        return 'historical'
    return validation if validation in COUNT_KEYS else 'unverified'


def counts_for(files):
    counts = {key: 0 for key in COUNT_KEYS}
    for record in files:
        if not record.get('duplicate_of'):
            counts[category(record)] += 1
    return counts


def _csv_cell(value):
    text = str(value if value is not None else '')
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def write_reports(job_dir, result):
    """Reports are versioned in new directories, so resumed runs keep old reports."""
    root = Path(job_dir).absolute()
    no_reparse(root)
    if not root.is_dir():
        raise ValueError('报告位置必须为已有普通任务文件夹')
    with _output_tree(root, []) as (_root, parent):
        report_dir = parent / ('reports_' + uuid.uuid4().hex[:12])
        report_dir.mkdir(exist_ok=False)
    paths = dict(report_path=str(report_dir / 'report.html'), csv_path=str(report_dir / 'files.csv'),
                 json_path=str(report_dir / 'result.json'))
    data = {**result, **paths, 'report_created_at': datetime.now(timezone.utc).isoformat(), 'validation_scope': SCOPE}
    files = data.get('files', [])
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('原始路径或候选标识', '输出路径', '分类', '文件验证状态', '来源', '字节数', 'SHA256', '重复关联', '警告'))
    rows = []
    for record in files:
        cells = (record.get('original_path', record.get('id', '')), record.get('output_path', record.get('path', '')),
                 LABELS[category(record)], record.get('validation_status', record.get('status', 'unverified')),
                 record.get('origin', ''), record.get('bytes', record.get('size', '')), record.get('sha256', ''),
                 record.get('duplicate_of', ''), '；'.join(map(str, record.get('warnings', []))))
        writer.writerow([_csv_cell(value) for value in cells])
        rows.append('<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in cells) + '</tr>')
    counts = data.get('counts') or counts_for(files)
    state = '已停止，部分结果保留' if data.get('cancelled') else ('本次处理结束，文件仍需核对' if data.get('complete') else '部分处理结果，尚未完成全部约定范围')
    warning_list = ''.join('<li>' + html.escape(str(value)) + '</li>' for value in data.get('warnings', []))
    count_list = '；'.join(f'{LABELS[key]}：{int(counts.get(key, 0))}' for key in COUNT_KEYS)
    document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">'
                '<title>U盘恢复结果核对报告</title><style>body{font:15px/1.65 "Microsoft YaHei",sans-serif;margin:28px;color:#222}'
                'table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #bbb;padding:7px;vertical-align:top;overflow-wrap:anywhere}'
                'th{background:#eee}p.note{padding:12px;background:#fff4d8}code{overflow-wrap:anywhere}</style>'
                '<h1>U盘恢复结果核对报告</h1><h2>' + html.escape(state) + '</h2><p class="note">' + html.escape(SCOPE) + '</p>'
                '<p>镜像读取完整度：' + html.escape({'unknown': '未知（未提供读取记录）', 'complete': '所选视图全部为已读取范围', 'partial': '存在未读或无法确认范围'}.get(data.get('image_completeness'), '见任务记录')) + '</p>'
                '<p>' + html.escape(count_list) + '。重复字节内容仅记录别名，不重复增加唯一文件数。</p>'
                '<p><a href="files.csv">文件清单 CSV</a> · <a href="result.json">完整证据 JSON</a></p>'
                '<h2>提示与范围</h2><ul>' + warning_list + '</ul><h2>文件及候选</h2>'
                '<table><thead><tr>' + ''.join('<th>' + title + '</th>' for title in
                ('原路径/标识', '输出路径', '分类', '验证状态', '来源', '字节数', 'SHA256', '重复关联', '警告')) +
                '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></html>')
    with _output_tree(report_dir, []):
        for path, content in ((paths['report_path'], document.encode('utf-8')),
                              (paths['csv_path'], stream.getvalue().encode('utf-8-sig')),
                              (paths['json_path'], json_bytes(data))):
            no_reparse(Path(path))
            with Path(path).open('xb') as output:
                output.write(content)
    return paths
