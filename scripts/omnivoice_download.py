"""Resume the authorized local OmniVoice model download.

This helper intentionally relies on Hugging Face's normal local cache and does
not read, print, or persist credentials.  It is an operational helper for the
non-commercial evaluation environment, not a product runtime dependency.
"""

from huggingface_hub import snapshot_download


def main() -> None:
    print(
        "OmniVoice download started; resuming shared Hugging Face cache",
        flush=True,
    )
    cache_path = snapshot_download(
        repo_id="k2-fsa/OmniVoice",
        repo_type="model",
        resume_download=True,
    )
    print(f"OmniVoice download completed: {cache_path}", flush=True)


if __name__ == "__main__":
    main()
