"""街道报修表下载：从台账/工单批次构建灯组日单量，做披露控制并生成报表。"""
import csv
import io
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.settings import ARTIFACTS
from src.domain.analysis import _date
from src.domain.disclosure import K_THRESHOLD, MASK, plan_release
from src.models.jobs import ReportRelease
from src.models.source import Dataset
from src.utils.exports import safe_cell

METRIC_DAILY = '灯组日单量'
METRIC_GROUP_PERIOD = '灯组期间合计'
METRIC_DISTRICT_DAY = '片区日合计'
METRICS = (METRIC_DAILY, METRIC_GROUP_PERIOD, METRIC_DISTRICT_DAY)


def _grid(rows, start, end):
    """从原始台账/工单行构建灯组、片区、日期轴与真实单量网格。"""
    from datetime import date, timedelta

    sites = {}
    counts = defaultdict(int)
    incident_days = set()
    for row in rows:
        if row.get('record_type') == '灯组台账':
            sites[row['site_id']] = row['district']
        elif row.get('record_type') == '暗区工单':
            report_date = row['report_date']
            if start and report_date < start:
                continue
            if end and report_date > end:
                continue
            counts[(row['site_id'], report_date)] += 1
            incident_days.add(report_date)
    if not sites:
        raise ValueError('数据批次缺少灯组台账，无法生成报修表。')
    group_ids = sorted(sites)
    group_index = {site_id: g for g, site_id in enumerate(group_ids)}
    district_of_group = [sites[site_id] for site_id in group_ids]
    # 日期轴优先取显式起止区间（含零报修日），否则取实际出现过的报修日期。
    if start and end:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        selected_days = [(s + timedelta(days=k)).isoformat()
                         for k in range((e - s).days + 1)]
    else:
        selected_days = sorted(incident_days)
    day_index = {d: i for i, d in enumerate(selected_days)}
    true_cells = {}
    for (site_id, day), value in counts.items():
        if site_id in group_index and day in day_index:
            true_cells[(group_index[site_id], day_index[day])] = value
    return group_ids, district_of_group, selected_days, day_index, true_cells


def _requested_metrics(true_cells, district_of_group, selected_days, metrics, group_ids):
    groups_by_district = defaultdict(list)
    for g, district in enumerate(district_of_group):
        groups_by_district[district].append(g)
    requested_dd, requested_gp = [], []
    if METRIC_DISTRICT_DAY in metrics:
        for district, gset in groups_by_district.items():
            for d, day in enumerate(selected_days):
                requested_dd.append({'day': d, 'groups': sorted(gset),
                                     'value': sum(true_cells.get((g, d), 0) for g in gset),
                                     'district': district, 'day_label': day})
    if METRIC_GROUP_PERIOD in metrics:
        for g in range(len(group_ids)):
            requested_gp.append({'group': g, 'days': list(range(len(selected_days))),
                                 'value': sum(true_cells.get((g, d), 0) for d in range(len(selected_days)))})
    return requested_dd, requested_gp


def _render_csv(group_ids, district_of_group, selected_days, metrics, true_cells,
                keep_cells, kept_dd, kept_gp, requested_dd, requested_gp, start, end):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow([safe_cell('街道报修表（已做统计披露控制）')])
    writer.writerow([safe_cell(f'报修起日 {start or "不限"} · 报修止日 {end or "不限"} · 少于 {K_THRESHOLD} 单的灯组日单量以 {MASK} 遮住')])
    writer.writerow([])

    if METRIC_DAILY in metrics:
        writer.writerow([safe_cell(METRIC_DAILY)])
        header = ['灯组编号', '片区'] + [safe_cell(day) for day in selected_days]
        writer.writerow(header)
        for g, site_id in enumerate(group_ids):
            line = [safe_cell(site_id), safe_cell(district_of_group[g])]
            for d in range(len(selected_days)):
                if (g, d) in keep_cells:
                    line.append(safe_cell(true_cells.get((g, d), 0)))
                else:
                    line.append(MASK)
            writer.writerow(line)
        writer.writerow([])

    if METRIC_DISTRICT_DAY in metrics:
        writer.writerow([safe_cell(METRIC_DISTRICT_DAY)])
        writer.writerow(['片区', '报修日期', '合计'])
        for item, kept in zip(requested_dd, kept_dd):
            writer.writerow([safe_cell(item['district']), safe_cell(item['day_label']),
                             safe_cell(item['value']) if kept else MASK])
        writer.writerow([])

    if METRIC_GROUP_PERIOD in metrics:
        writer.writerow([safe_cell(METRIC_GROUP_PERIOD)])
        writer.writerow(['灯组编号', '片区', '期间合计'])
        for item, kept in zip(requested_gp, kept_gp):
            g = item['group']
            writer.writerow([safe_cell(group_ids[g]), safe_cell(district_of_group[g]),
                             safe_cell(item['value']) if kept else MASK])
    return '﻿' + buffer.getvalue()


def publish_report(db: Session, body, actor):
    dataset = db.get(Dataset, body.dataset_id, with_for_update=True)
    if not dataset:
        raise LookupError('找不到指定的数据批次')
    metrics = list(dict.fromkeys(body.metrics))
    unknown = set(metrics) - set(METRICS)
    if unknown:
        raise ValueError(f'不支持的统计项：{sorted(unknown)}')
    start = _date(body.start_date, '报修起日') if body.start_date else None
    end = _date(body.end_date, '报修止日') if body.end_date else None
    if start and end and start > end:
        raise ValueError('报修起日须早于或等于报修止日。')

    # 幂等：同批次同键的重试直接拿回原报表，不重复发布。
    existing = db.scalar(select(ReportRelease).where(
        ReportRelease.dataset_id == body.dataset_id,
        ReportRelease.idempotency_key == body.idempotency_key).with_for_update())
    if existing:
        return existing, False

    group_ids, district_of_group, selected_days, day_index, true_cells = _grid(
        dataset.rows, start, end)
    requested_dd, requested_gp = _requested_metrics(
        true_cells, district_of_group, selected_days, metrics, group_ids)

    # 历次发布按创建顺序累计；每条记录保存的是当时的累计快照，因此只取最新一条作为基线。
    history_cells, history_margins = {}, []
    latest = db.scalars(select(ReportRelease).where(
        ReportRelease.dataset_id == body.dataset_id).order_by(ReportRelease.id.desc())).first()
    if latest:
        history_cells = {tuple(map(int, key.split(','))): value
                         for key, value in latest.published_cells.items()}
        history_margins = list(latest.published_margins)

    keep_cells, kept_dd, kept_gp, fixed_cells = plan_release(
        true_cells, district_of_group, selected_days, requested_dd, requested_gp,
        {'cells': history_cells, 'margins': history_margins})

    csv_text = _render_csv(group_ids, district_of_group, selected_days, metrics, true_cells,
                           keep_cells, kept_dd, kept_gp, requested_dd, requested_gp, start, end)

    # 累计本次实际发布的约束，供后续下载使用。
    published_cells = {f'{g},{d}': fixed_cells[(g, d)] for (g, d) in keep_cells}
    published_margins = list(history_margins)
    for item, kept in zip(requested_dd, kept_dd):
        if kept:
            published_margins.append({'kind': 'dd',
                                      'cells': [(g, item['day']) for g in item['groups']],
                                      'value': item['value']})
    for item, kept in zip(requested_gp, kept_gp):
        if kept:
            published_margins.append({'kind': 'gp',
                                      'cells': [(item['group'], d) for d in item['days']],
                                      'value': item['value']})

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    export_name = f'streetlight-report-{body.dataset_id}-{body.idempotency_key}.csv'
    safe_name = export_name.replace('/', '_')
    staging = ARTIFACTS / f'{safe_name}.tmp'
    staging.write_text(csv_text, encoding='utf-8')
    staging.replace(ARTIFACTS / safe_name)

    report = {'group_ids': group_ids, 'district_of_group': district_of_group,
              'selected_days': selected_days, 'metrics': metrics,
              'kept_daily': sorted(list(keep_cells)),
              'kept_district_day': [bool(k) for k in kept_dd],
              'kept_group_period': [bool(k) for k in kept_gp],
              'csv': csv_text}
    release = ReportRelease(dataset_id=body.dataset_id, idempotency_key=body.idempotency_key,
                            start_date=start, end_date=end, metrics=metrics,
                            published_cells=published_cells, published_margins=published_margins,
                            report=report, export_file=safe_name, author=actor.username)
    db.add(release)
    db.flush()
    return release, True
