# MedEnv：OSCE 医学智能体评测仓库

本仓库按 MedGym 的 `README_TEST_OSCE.md` 运行流程整理，包含 bench 数据加载、医生/患者交互、检查请求、诊断评分、检索服务及 CXR 工具。运行时已独立封装，不再依赖原来的 rllm 路径。

## 本地数据与公开仓库

本地 `data/bench/` 保存全部 **5,405 个病例、883 张图像**，包含四个子集：

| 子集 | 病例数 | 原运行说明是否覆盖 |
|---|---:|---|
| `with_img/ed_hosp` | 401 | 默认有图模式 |
| `with_img/hosp_only` | 17 | 新增显式选择支持 |
| `without_img/hosp_only` | 3,470 | `--no_cxr` |
| `without_img/ed_hosp` | 1,517 | 新增显式选择支持 |

`data/retrieval/` 保存现有检索语料、E5/FAISS 索引、文档映射和用于重建索引的原始语料。复制时逐文件核对 SHA-256，本地校验清单位于 `data/private/`。

真实病例、影像、检索语料、模型与实验输出默认不进入 Git。MIMIC 衍生记录仍受数据访问条款约束，不能因为代码开源就直接上传公开 GitHub。公开版本包含四个完全合成病例和数据接入脚本。详见 [数据说明](docs/DATA.md)。

## 安装与离线检查

推荐 Python 3.11；PyTorch 按实际 CPU/CUDA 环境安装。

```bash
cd /path/to/MedEnv
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env

python -m medenv --case_dir data/synthetic --dry_run
python -m unittest discover -s tests -v
python scripts/validate_data.py --bench-root data/bench
```

新机器上有已授权的预处理 bench 时，可完整导入：

```bash
python scripts/import_data.py --bench-source /path/to/osce_data/bench
# 可选：同时导入原项目 guidelines 目录中的检索资产
python scripts/import_data.py --retrieval-source /path/to/guidelines
```

## 按原说明启动三个模型服务

vLLM 建议单独建环境安装，避免与评测环境的 PyTorch 依赖冲突；原环境记录的版本为 `vllm==0.11.0`。修改 `.env` 中的模型路径、tokenizer 路径、GPU 分配后，在三个终端分别执行：

```bash
bash scripts/start_vllm.sh doctor
bash scripts/start_vllm.sh patient
bash scripts/start_vllm.sh judge
```

默认端口为 `30000/30001/30002`，默认 GPU 分别为 `0/1/2`。每个脚本只启动一个前台服务；不会清理机器上已有服务。GPU 显存是否足够取决于模型、上下文长度与硬件，可配置多卡 TP。脚本会检查配置中的三组 GPU 是否重叠。

原工具集还需要检索服务；有图模式下的 `cxr_grounding` 需要 Grounding DINO 服务。其安装、启动与健康检查见 [完整运行指南](docs/RUNNING.md)。

## 跑 bench

```bash
# 有 CXR 的人群，Qwen3-8B 文本报告模式；默认前 10 例
bash scripts/run_bench.sh --cxr_mode reports

# 无 CXR；默认 without_img/hosp_only
bash scripts/run_bench.sh --no_cxr

# 显式指定其他子集，0 表示运行该子集全部病例
bash scripts/run_bench.sh --subdir with_img/hosp_only --max_cases 0

# 全部 4 个子集、全部病例；每个子集独立保存结果
bash scripts/run_all.sh --n_parallel_agents 1
```

使用 `--cxr_mode vision` 时，Doctor 必须换成支持图像输入的模型及对应 tokenizer。原说明的 Qwen3-8B 是文本模型：本仓库将其行为明确标记为报告模式。视觉模式通过 Chat Completions 传递图像，同时保留原工具提供的报告文本，不能将它称为纯图像实验。

`--disable_retrieval` 和 `--disable_grounding` 可用于消融实验；会改变工具配置。直接运行 Python 入口时需先导出 `.env`；shell 脚本会自动加载。

每次运行产生：

- `config.json`：参数、工具集和样本数，API key 已脱敏。
- `metrics.json`：实际环境判分、准确率、经验 pass@k、逐病例结果。
- `trajectories/`：逐病例交互轨迹。可用 `python scripts/trajectory_to_html.py <trajectory.json> --output outputs/trajectory.html` 转为 HTML。

## 发布 GitHub

```bash
git add .
python scripts/check_release.py --archive dist/MedEnv-public.zip
git diff --cached --stat
# 检查后自行提交并连接目标仓库
git commit -m "Initial MedEnv benchmark release"
git remote add origin <your-github-repository-url>
git push -u origin main
```

`dist/MedEnv-public.zip` 为可分享的源码包，排除了真实数据与本地运行产物。不要将整个本地目录直接压缩上传；请使用发布脚本。代码沿用原目录的 Apache-2.0 许可证；数据和模型许可独立，见 [NOTICE](NOTICE)。

迁移中的图像路径、诊断判分、检查上下文等修正见 [变更记录](docs/CHANGES.md)，验证范围见 [验证记录](docs/VALIDATION.md)。
