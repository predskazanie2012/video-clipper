# Limitations and integration requirements

Real cutting and center cropping produced a playable vertical MP4. Full AI selection, face tracking, music generation and account-connected publishing require separate configured runs.

Install FFmpeg and ffprobe on PATH. The local requirements cover the interface and core editing modules. Transcription and other model features need additional dependencies from `requirements.txt`; see [Optional models](OPTIONAL-MODELS.md) before enabling face tracking or music generation.

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits.
