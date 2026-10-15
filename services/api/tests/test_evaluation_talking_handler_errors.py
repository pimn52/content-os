"""Public owned processing errors are terminal and credential-free."""
import pytest
from app.jobs.evaluation_talking_qa_handler import EvaluationTalkingQaHandler
from app.jobs.evaluation_talking_preview_handler import EvaluationTalkingPreviewHandler
from app.jobs.runner import JobExecutionError
from app.execution_admission import UseAdmissionError


@pytest.mark.parametrize("handler_class",[EvaluationTalkingQaHandler,EvaluationTalkingPreviewHandler])
@pytest.mark.parametrize("error",[UseAdmissionError("evaluation_talking_operation_cancelled"),
    UseAdmissionError("private/path?operator_key=SECRET"),ValueError("private/path?operator_key=SECRET")])
def test_public_errors_never_retry_or_expose_private_details(tmp_path,monkeypatch,handler_class,error):
    options = dict(probe=None)
    if handler_class is EvaluationTalkingPreviewHandler: options["ffmpeg_command"] = "never-run"
    handler = handler_class(None,tmp_path,**options)
    def reject(_job): raise error
    monkeypatch.setattr(handler,"require_dispatch",reject)
    with pytest.raises(JobExecutionError) as caught: handler(None)
    assert caught.value.retryable is False
    assert "SECRET" not in caught.value.message and "private" not in caught.value.message
    assert "SECRET" not in caught.value.code
