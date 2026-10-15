"""Measured synthetic AV timing; no user media, models or project database."""
import subprocess
from uuid import uuid4
import pytest
from app.domain.models import ProjectFormat, RationalFps, VideoScene, VideoSpec, VideoVisual
from app.presentation_repair import measure_render_output
from app.runtime import resolve_local_executable


@pytest.mark.parametrize("rate,frames",[(RationalFps(numerator=30,denominator=1),69),
    (RationalFps(numerator=30000,denominator=1001),69)])
def test_shared_real_av_probe_and_full_decode(tmp_path,rate,frames):
    output=tmp_path/"synthetic.mp4"
    spec=VideoSpec(project_id=uuid4(),format=ProjectFormat.VERTICAL,width=1080,height=1920,fps=rate,
        scenes=[VideoScene(scene_id="synthetic",start_frame=0,duration_frames=frames,
            visual=VideoVisual(source_kind="typography",authorization_reference="synthetic"),
            subtitle_treatment="none",graphic_treatment="none")])
    duration=frames*rate.denominator/rate.numerator
    command=[resolve_local_executable("ffmpeg"),"-v","error","-y","-f","lavfi","-i",
        f"color=c=0xf3efe5:s=1080x1920:r={rate.numerator}/{rate.denominator}","-f","lavfi","-i",
        "anullsrc=r=24000:cl=mono","-t",str(duration),"-frames:v",str(frames),"-c:v","libx264","-preset","ultrafast",
        "-pix_fmt","yuv420p","-c:a","aac",str(output)]
    result=subprocess.run(command,shell=False,capture_output=True,timeout=45)
    assert result.returncode==0,result.stderr.decode(errors="replace")[-1000:]
    evidence=measure_render_output(output,spec)
    assert evidence["state"]=="verified" and evidence["decode"]=="pass"
    assert evidence["video_frames"]==frames and evidence["has_audio"]
    assert evidence["scope"]=="technical_only_final_u_product_required"
