from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:password@localhost:5432/blueprintqa"

    # LLM provider: NVIDIA NIM, which exposes an OpenAI-compatible API.
    nvidia_api_key: str = ""
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    # Must be vision-capable: pages are sent as images.
    llm_vision_model: str = "meta/llama-3.2-11b-vision-instruct"
    llm_max_tokens: int = 2048
    upload_dir: str = "./uploads"
    max_file_size_mb: int = 50
    max_pages_per_document: int = 10
    storage_backend: str = "local"
    azure_connection_string: str = ""
    azure_container_name: str = "blueprintqa"

    # Drawing assistant (retrieval + conversational Q&A)
    # Empty means "use llm_vision_model" for the text-only synthesis step.
    llm_text_model: str = ""
    assistant_max_index_pages: int = 150
    assistant_top_k: int = 5
    assistant_max_page_chars: int = 12000
    # Longest side, in pixels, of the image sent to the vision model per page.
    assistant_model_image_px: int = 1600
    # Cap on the base64 size of an image sent to the model; 0 = no cap. If the endpoint
    # rejects large inline images, a request is retried once under ~170 KB anyway;
    # set this (e.g. 170000) to skip the failed first attempt.
    assistant_model_image_max_bytes: int = 0
    # Longest side of the stored page image shown in the viewer and used for OCR.
    assistant_page_image_px: int = 3600
    # Pages whose embedded text layer is shorter than this are OCR'd instead.
    assistant_text_layer_min_chars: int = 80
    assistant_history_messages: int = 6
    assistant_concurrency: int = 2
    # Extra attempts after a transient API error or unparseable model output.
    assistant_llm_retries: int = 2
    # Object counting
    count_max_pages: int = 10
    # Ask the vision model for a (flagged, low-confidence) estimate when nothing in the
    # PDF's text or vector data identifies the object.
    count_vision_fallback: bool = True

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
