"""Review selected card collections with Azure OpenAI and a persistent image cache."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PROMPT = """Inspect this Hungarian quiz-card image. Return JSON with: is_card, orientation (upright, rotated, mixed, unknown), category, answer, confidence, and a short reason. Do not infer text that is not visible."""


def image_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_key(path: Path, prompt: str, deployment: str) -> str:
    material = f"{deployment}\n{prompt}\n{image_hash(path)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def load_cache(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(path: Path, cache: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def review_images(
    image_paths: list[Path],
    cache_path: Path,
    prompt: str,
    endpoint: str,
    deployment: str,
) -> list[dict[str, object]]:
    cache = load_cache(cache_path)
    results: list[dict[str, object]] = []
    client = None
    for image_path in image_paths:
        key = cache_key(image_path, prompt, deployment)
        cached = cache.get(key)
        if isinstance(cached, dict):
            results.append(cached)
            print(f"cache hit: {image_path}", file=sys.stderr)
            continue

        if client is None:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://cognitiveservices.azure.com/.default",
            )
            client = AzureOpenAI(
                azure_endpoint=endpoint,
                azure_ad_token_provider=token_provider,
                api_version="2025-01-01-preview",
            )

        encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
        completion = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "developer", "content": "You review image evidence conservatively."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"},
                        },
                    ],
                },
            ],
            max_completion_tokens=2000,
            stream=False,
        )
        record = {
            "image": str(image_path),
            "image_sha256": image_hash(image_path),
            "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
            "response": json.loads(completion.to_json()),
        }
        cache[key] = record
        save_cache(cache_path, cache)
        results.append(record)
        print(f"reviewed: {image_path}", file=sys.stderr)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", action="append", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("review/llm_cache.json"))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--endpoint", default=os.getenv("ENDPOINT_URL", "https://ae-oa-d-we-004.openai.azure.com/"))
    parser.add_argument("--deployment", default=os.getenv("DEPLOYMENT_NAME", "gpt-5.6-luna"))
    args = parser.parse_args()
    results = review_images(args.image, args.cache, args.prompt, args.endpoint, args.deployment)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()