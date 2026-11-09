#!/usr/bin/env bash
# Gets what the builder's free gap fill runs on, into llm/ (job-index.yml keeps it in the Actions cache,
# keyed by this file, so it's fetched once and again only when a version below changes):
#   llm/bin/llama-server   llama.cpp's server, built from source for CPUs (every x86-64 variant, picked
#                          at run time, so a cached build works on whichever runner comes next)
#   llm/model.gguf         the open model that reads unsettled postings (lighthouse/llm_fill.py)
#   llm/embed/             the static embedding model that places job titles (lighthouse/categorize.py)
# All free and open (MIT / Apache-2.0); nothing needs a key or an account.
set -euo pipefail

LLAMA_CPP_REF="${LLAMA_CPP_REF:-v0.9.0}"
MODEL_REPO="${MODEL_REPO:-unsloth/gemma-4-E2B-it-GGUF}"
MODEL_FILE="${MODEL_FILE:-gemma-4-E2B-it-Q4_K_M.gguf}"
MODEL_REVISION="${MODEL_REVISION:-main}"
MODEL_SHA256="${MODEL_SHA256:-}"
EMBED_REPO="${EMBED_REPO:-minishlab/potion-base-8M}"
EMBED_REVISION="${EMBED_REVISION:-main}"
OUT="${OUT:-llm}"

mkdir -p "$OUT/bin"
start=$(date +%s)

if [ ! -x "$OUT/bin/llama-server" ]; then
  src=$(mktemp -d)
  git clone --quiet --filter=blob:none https://github.com/ggml-org/llama.cpp "$src"
  git -C "$src" checkout --quiet "$LLAMA_CPP_REF"
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON \
        -DBUILD_SHARED_LIBS=ON > "$src/cmake.log" || { tail -40 "$src/cmake.log"; exit 1; }
  cmake --build "$src/build" -j "$(nproc)" > "$src/build.log" 2>&1 || { tail -60 "$src/build.log"; exit 1; }
  cp -a "$src/build/bin/." "$OUT/bin/"
  echo "llama.cpp: $LLAMA_CPP_REF = $(git -C "$src" rev-parse HEAD), built in $(( $(date +%s) - start )) s"
  rm -rf "$src"
fi

python - "$MODEL_REPO" "$MODEL_FILE" "$MODEL_REVISION" "$MODEL_SHA256" "$EMBED_REPO" "$EMBED_REVISION" "$OUT" <<'PY'
import hashlib, os, shutil, sys, time
from huggingface_hub import HfApi, hf_hub_download, snapshot_download

repo, name, rev, sha, embed_repo, embed_rev, out = sys.argv[1:]
api = HfApi()
target = os.path.join(out, "model.gguf")
if not os.path.exists(target):
    t = time.time()
    info = api.model_info(repo, revision=rev, files_metadata=True)
    path = hf_hub_download(repo, name, revision=info.sha, local_dir=os.path.join(out, "dl"))
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if sha and h.hexdigest() != sha:
        sys.exit(f"{name}: sha256 {h.hexdigest()} isn't the pinned {sha}")
    shutil.move(path, target)
    shutil.rmtree(os.path.join(out, "dl"), ignore_errors=True)
    print(f"model: {repo}@{info.sha} {name} sha256={h.hexdigest()} "
          f"{os.path.getsize(target) / 1e6:.0f} MB in {time.time() - t:.0f} s")
embed = os.path.join(out, "embed")
if not os.path.isdir(embed):
    info = api.model_info(embed_repo, revision=embed_rev)
    snapshot_download(embed_repo, revision=info.sha, local_dir=embed,
                      allow_patterns=["*.json", "*.safetensors", "*.txt", "*.md"])
    print(f"embedding model: {embed_repo}@{info.sha}")
PY
echo "llm/ ready in $(( $(date +%s) - start )) s: $(du -sh "$OUT" | cut -f1)"
