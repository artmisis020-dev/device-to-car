#!/bin/bash
# Тестове середовище Air-IO на Spark — ОКРЕМО від робочого venv sirena-vision.
# Відтворює те, що вже розгорнуто в /opt/sirena-vision/learnedio.
set -euo pipefail
W=${AIRIO_WORKDIR:-/opt/sirena-vision/learnedio}
mkdir -p "$W"/{data,models}
cd "$W"
[ -d Air-IO ] || git clone -q --depth 1 https://github.com/Air-IO/Air-IO.git
if [ ! -d data/Blackbird ]; then
  curl -sL -o /tmp/bb.zip https://github.com/Air-IO/Air-IO/releases/download/datasets/Blackbird.zip
  unzip -q /tmp/bb.zip -d data && rm /tmp/bb.zip
fi
if [ ! -d models/AirIO_Blackbird ]; then
  curl -sL -o /tmp/m.zip https://github.com/Air-IO/Air-IO/releases/download/AirIO/AirIO_Blackbird.zip
  mkdir -p models/AirIO_Blackbird && unzip -q /tmp/m.zip -d models/AirIO_Blackbird && rm /tmp/m.zip
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# torch — той самий індекс CUDA 13, що й vision_module/requirements.txt
.venv/bin/pip install -q --extra-index-url https://download.pytorch.org/whl/cu130 \
    torch pypose==0.6.8 pyhocon scipy tqdm matplotlib numpy wandb
.venv/bin/python -c "import torch, pypose; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
echo "Готово: $W"
