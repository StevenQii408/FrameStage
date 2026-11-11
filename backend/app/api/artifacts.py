from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/jobs/{job_id}/video")
@router.head("/jobs/{job_id}/video")
def source_video(request: Request, job_id: str):
    """Stream the saved source, including Safari Range/HEAD requests."""
    row = request.app.state.job_service.db.fetchone(
        "SELECT video_path,input_type FROM jobs WHERE id=?", (job_id,))
    if not row or row['input_type'] != 'video' or not row['video_path'] or not Path(row['video_path']).is_file():
        raise HTTPException(status_code=404, detail="原视频不存在或已移除")
    return FileResponse(row['video_path'], media_type='video/mp4',
                        content_disposition_type='inline')


@router.get("/jobs/{job_id}/frames/{frame_id}")
def frame(request: Request, job_id: str, frame_id: str):
    service = request.app.state.job_service
    row = service.db.fetchone("SELECT path FROM frames WHERE job_id = ? AND id = ?", (job_id, frame_id))
    if not row:
        # Experimental evidence is run-scoped and must not change the original
        # task's frames/result. Its IDs still open through the Artifact API.
        row = service.db.fetchone(
            "SELECT f.path FROM event_agent_frames f JOIN event_agent_runs r ON r.id=f.run_id "
            "WHERE f.job_id=? AND r.job_id=? AND f.id=?", (job_id, job_id, frame_id))
    if not row or not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="帧不存在")
    return FileResponse(row["path"], media_type="image/jpeg")
