from __future__ import annotations

from huggingface_hub import snapshot_download

from src.env_config import load_project_env, require_env


def main() -> None:
    load_project_env()

    model_ids = [
        require_env("LOCAL_MODEL_ID"),
        require_env("LOCAL_CLASSIFIER_MODEL_ID"),
    ]

    for model_id in dict.fromkeys(model_ids):
        print(f"Preloading Hugging Face model: {model_id}", flush=True)
        snapshot_download(repo_id=model_id)
        print(f"Cached Hugging Face model: {model_id}", flush=True)


if __name__ == "__main__":
    main()
