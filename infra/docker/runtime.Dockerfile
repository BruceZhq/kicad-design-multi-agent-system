# RatsNest agent runtime — all intelligence lives here.
# Build context must be the directory that contains BOTH `RatsNest/` and the
# vendored `kicad-happy-main/` checkout:
#
#   docker build -f RatsNest/infra/docker/runtime.Dockerfile -t ratsnest-runtime .
#
# The image mirrors the on-disk layout so every path default just works:
#   /repo/RatsNest/agent-runtime   (this package)
#   /repo/RatsNest/benchmarks      (corpus + seeder)
#   /repo/kicad-happy-main         (vendored evaluation engine, unforked)
FROM python:3.11-slim

WORKDIR /repo/RatsNest/agent-runtime

COPY kicad-happy-main/ /repo/kicad-happy-main/
COPY RatsNest/agent-runtime/ /repo/RatsNest/agent-runtime/
COPY RatsNest/benchmarks/ /repo/RatsNest/benchmarks/

RUN pip install --no-cache-dir "pydantic>=2.5" "pyyaml>=6.0" "httpx>=0.27" "pytest>=8.0" "kafka-python>=2.0"

# kicad-cli is not in this image (KiCad is ~2GB); ERC is feature-gated off.
ENV RATSNEST_RUNS_DIR=/repo/RatsNest/runs

ENTRYPOINT ["python", "-m", "ratsnest"]
CMD ["--help"]
