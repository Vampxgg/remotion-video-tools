---
name: cre_image_implementation_plan
overview: Implement the `cre_image` interface for generating images using Gemini 2.5 Flash Image and Gemini 3 Pro Image models, following the style of `cre_video.py` and integrating with Google Cloud Storage.
todos:
  - id: create-image-module
    content: Create api/cre_image.py with logger, constants, and HTTP client setup
    status: completed
  - id: implement-auth-utils
    content: Implement auth and GCS upload helper functions in cre_image.py
    status: completed
    dependencies:
      - create-image-module
  - id: define-models
    content: Define Pydantic models for Image Generation Request/Response
    status: completed
    dependencies:
      - create-image-module
  - id: implement-endpoint
    content: Implement generate_image endpoint calling generateContent and uploading to GCS
    status: completed
    dependencies:
      - implement-auth-utils
      - define-models
  - id: register-router
    content: Register the new image router in main.py
    status: completed
    dependencies:
      - implement-endpoint
---

## Product Overview

Implement a new image generation module `api/cre_image.py` that integrates with Google Vertex AI to generate images using specific Gemini models. The generated images will be uploaded to Google Cloud Storage (GCS) and served via public URLs.

## Core Features

- **Image Generation API**: A REST endpoint to generate images from text prompts.
- **Model Support**: Support for `gemini-2.5-flash-image` and `gemini-3-pro-image-preview`.
- **GCS Integration**: Automatically upload generated images (from Base64 response) to a specified GCS bucket (`x-pilot-storage`).
- **Public Access**: Return publicly accessible URLs for the generated images.
- **Consistency**: Follow the architectural style of `api/cre_video.py` (Async `httpx`, `pydantic` validation, `google.auth` for ADC).

## Tech Stack

- **Framework**: FastAPI
- **HTTP Client**: `httpx` (Async)
- **Authentication**: `google.auth` (Application Default Credentials)
- **Cloud Services**: 
    - Google Vertex AI (Model Garden/Gemini API)
    - Google Cloud Storage (GCS) (via JSON API or `google-cloud-storage` if available, preferred REST for consistency)

## Tech Architecture

### System Architecture

- **Router**: `api/cre_image.py` handles the `/generate_image` endpoint.
- **Service Layer**: 
    - Authenticates using Google ADC.
    - Calls Vertex AI `generateContent` API.
    - Decodes Base64 image response.
    - Uploads image to GCS bucket `x-pilot-storage`.
    - Returns public URL.
- **Integration**: Registered in `main.py`.

### Key Code Structures

**ImageGenerationPayload**:

```python
class ImageModelID(str, Enum):
    GEMINI_2_5_FLASH = "gemini-2.5-flash-image"
    GEMINI_3_PRO_PREVIEW = "gemini-3-pro-image-preview"

class GenerateImagePayload(BaseModel):
    prompt: str
    model_id: ImageModelID
    aspect_ratio: Optional[str] = "1:1"
    number_of_images: int = 1
    # ... other parameters
```

**GCS Upload Logic**:
Since `generateContent` typically returns Base64 data for images, the system will include a helper to upload this data to GCS via the XML/JSON API using the same `httpx` client and Auth token.