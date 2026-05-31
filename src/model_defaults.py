# Shared constructor defaults. CLI runs still read these values from `.env`;
# these constants keep direct Python use and tests aligned with the template.
DEFAULT_LOCAL_MODEL_ID = "Qwen/Qwen3-4B"
DEFAULT_OLLAMA_MODEL = "qwen3:4b"
DEFAULT_AIASS_MODEL = "swiss-ai/Apertus-8B-Instruct-2509"
DEFAULT_AIASS_BASE_URL = "https://inference-rcp.epfl.ch/v1"
