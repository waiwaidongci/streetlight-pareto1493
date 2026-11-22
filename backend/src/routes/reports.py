from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from urllib.parse import quote

from src.config.database import connection
from src.controllers.auth import current
from src.models.identity import User
from src.schemas.requests import ReportDownloadRequest
from src.services.audit import record
from src.services.reports import publish_report

router = APIRouter(prefix='/street-reports')


@router.post('', status_code=201)
def download(body: ReportDownloadRequest, db: Session = Depends(connection),
             actor: User = Depends(current)):
    try:
        # 数据集行锁 + 发布表唯一约束共同保证并发按实际提交顺序串行判定，
        # 同幂等键的重试不会重复发布，直接返回原报表。
        release, created = publish_report(db, body, actor)
    except LookupError as error:
        db.rollback()
        raise HTTPException(404, str(error)) from None
    if created:
        record(db, actor.username, 'report.released', release.id,
               dataset_id=release.dataset_id, metrics=release.metrics)
        db.commit()
        filename = quote(f'街道报修表-{release.id}.csv')
        headers = {'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"}
        return Response(content=release.report['csv'], media_type='text/csv; charset=utf-8',
                        headers=headers, status_code=201)
    # 幂等重试：不重复发布，回滚只读事务并返回原报表。
    db.rollback()
    return Response(content=release.report['csv'], media_type='text/csv; charset=utf-8',
                    status_code=200)
