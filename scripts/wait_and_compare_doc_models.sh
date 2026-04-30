#!/bin/bash
set -e

echo "Waiting for models to download..."
echo

# Wait for all 3 models to be available
while true; do
    MODELS_READY=$(ollama list | grep -c "mistral\|llama2\|phi" || true)

    if [ "$MODELS_READY" -ge 3 ]; then
        echo "✓ All models ready!"
        break
    fi

    MISTRAL=$(ollama list | grep -c "mistral" || echo 0)
    LLAMA=$(ollama list | grep -c "llama2" || echo 0)
    PHI=$(ollama list | grep -c "phi" || echo 0)

    echo "  mistral: $MISTRAL  llama2: $LLAMA  phi: $PHI"
    sleep 10
done

echo
echo "Starting test..."
echo
uv run python scripts/compare_doc_models.py
