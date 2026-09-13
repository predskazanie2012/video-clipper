# Video Clipper

Video Clipper brings transcription, clip selection, vertical framing and subtitles into one local editing workflow. Review candidate moments, cut them into short videos, and prepare translated captions and metadata from the same interface.

## Features

- Select useful segments from a transcript with an LLM-assisted workflow.
- Cut clips with FFmpeg and compose vertical output with center or optional face-aware framing (see OPTIONAL-MODELS.md).
- Generate subtitles, translations and video metadata.
- Track processing jobs and review output before any publishing step.

## How it works

The web interface coordinates focused processing modules. Local media processing is separated from transcription, translation, metadata and optional publishing integrations.

**Stack:** Python · FastAPI · FFmpeg · Whisper · OpenAI · MediaPipe

## Getting started

Use Python 3.12 and a separate virtual environment. Run the following commands from this repository's root in Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

Install FFmpeg and ffprobe on PATH. The local requirements cover the interface and core editing modules. Transcription and other model features need additional dependencies from `requirements.txt`; see [Optional models](OPTIONAL-MODELS.md) before enabling face tracking or music generation.

If you need provider credentials, copy `.env.example` to `.env` and configure only the services you use. Keep `.env` local.

### Start the application

Copy `.env.example` to `.env` and fill only the services you use. Open http://127.0.0.1:8088. Install FFmpeg separately and put it on PATH. Configure only the AI providers you need in .env.

```powershell
Copy-Item config.example.yaml config.yaml
python app.py
```

## Example workflow

Load a short recording, choose segment boundaries, and export a vertical clip with center cropping.

## Testing and limitations

Real cutting and center cropping produced a playable vertical MP4. Full AI selection, face tracking, music generation and account-connected publishing require separate configured runs.

See [Verification](VERIFICATION.md) for the recorded checks and [Limitations](LIMITATIONS.md) for integration requirements.

## Configuration and security

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits. Configure your own provider credentials when a feature requires them; credentials and personal data are not included. See [Security](SECURITY.md) for local configuration and reporting guidance.
