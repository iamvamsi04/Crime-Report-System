# Crime-Report-System

2026-09-18 21:22:55,244 INFO httpx - HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents "HTTP/1.1 200 OK"
2026-09-18 21:22:56,942 ERROR app.api - document_upload_failed filename=2014_economy.txt.txt
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\api.py", line 91, in upload_document
    return UploadResponse(
        document=_document_response(
            document
        )
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\pydantic\main.py", line 263, in __init__
    validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
pydantic_core._pydantic_core.ValidationError: 4 validation errors for UploadResponse
document_id
  Field required [type=missing, input_value={'document': DocumentResp...15:52:40.875484+00:00')}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
filename
  Field required [type=missing, input_value={'document': DocumentResp...15:52:40.875484+00:00')}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
file_type
  Field required [type=missing, input_value={'document': DocumentResp...15:52:40.875484+00:00')}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
status
  Field required [type=missing, input_value={'document': DocumentResp...15:52:40.875484+00:00')}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
INFO:     127.0.0.1:65399 - "POST /documents/upload HTTP/1.1" 500 Internal Server Error
INFO:     127.0.0.1:65402 - "GET /documents HTTP/1.1" 200 OK
