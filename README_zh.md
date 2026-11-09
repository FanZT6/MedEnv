# MedEnv: Scaling Multimodal Virtual Medical Environments for Long-Horizon Diagnosis

**MICCAI 2026 接收论文。**

MedEnv 是面向多轮诊断的多模态虚拟医疗环境。医生智能体通过问诊、申请检查、检索医学知识、查看胸片和视觉定位收集证据，最终提交诊断。本仓库提供数据处理框架、benchmark 推理与评测代码。

[English](README.md) · [完整推理流程](docs/RUNNING.md) · [配置与代码修改示例](docs/CONFIGURATION.md) · [数据格式](data/README.md)

## 公开范围与 MIMIC 数据限制

我们公开的是**对 MIMIC 数据进行处理的代码框架和处理过程**，包括数据库导入、诊断时间窗口提取、OSCE 病例构建、EHR 格式转换和 ICD 分层抽样。具体步骤、输入要求和运行命令见 [`data_process/README.md`](data_process/README.md)。其中 CXR 预处理仅使用报告文本，不打包或复制图像，也不承诺重建与历史 5,405 例 benchmark 完全一致的数据集。

**MIMIC 是受限访问的数据集，本次代码公开不包含其原始数据或患者级衍生数据。** 原始表、临床记录、影像报告及图像、患者/住院/检查标识符、处理后的 OSCE/EHR 病例、病例抽样清单和逐例模型交互轨迹均应保存在私有环境，并遵守适用的 PhysioNet 访问条件及数据使用协议。去标识化、转成 JSON 或去掉本机路径，不代表这些记录可以公开。使用者须自行取得输入所需的 [MIMIC-IV](https://physionet.org/content/mimiciv/)、[MIMIC-IV-Note](https://physionet.org/content/mimic-iv-note/)、[MIMIC-IV-ED](https://physionet.org/content/mimic-iv-ed/) 和 [MIMIC-CXR](https://physionet.org/content/mimic-cxr/) 访问权限。Apache-2.0 仅适用于代码，不授予数据再分发权利。

真实输入、数据库和处理产物应存放在仓库之外。本地 `data/bench/`、`data/retrieval/` 和 `outputs/` 已被 Git 忽略，但 **`.gitignore` 不能阻止手工压缩、目录上传或文件复制带出这些内容**；发布时只应包含经过检查的源码。可公开的 `data/synthetic/` 是完全虚构的软件测试病例，其图片是生成的测试图案。

关于删除：不再需要运行记录时可以删除 `outputs/`，评测会重新创建输出目录。建议保留 `data/README.md` 和 `data/synthetic/`，它们用于文档、快速检查和离线测试；单独迁移或移除私有的 `data/bench/` 与本地 `data/retrieval/`。删除后，真实评测需将 `MEDENV_BENCH_ROOT` 指向仓库外的授权数据，检索服务需配置外部 `RETRIEVAL_INDEX` / `RETRIEVAL_CORPUS`。检查范围及结果见[本地数据公开检查记录](docs/DATA_PRIVACY_AUDIT_zh.md)。

## 推理流程

1. 加载病例，向医生提供人口学信息和主诉。
2. 医生选择问诊或工具调用，环境返回相应证据。患者与检查模拟器优先使用病例 EHR，记录不足时查询关联的疾病知识。
3. 重复交互，直到医生提交诊断或达到轮数限制。
4. Judge 根据参考诊断评分，保存指标和完整交互轨迹。

可用工具包括 `ask_patient`、`request_exam`、`retrieve`、`diagnosis`；有图子集还提供 `cxr` 和 `cxr_grounding`。

## 快速开始

推荐 Python 3.11，PyTorch 按硬件环境安装。

```bash
git clone https://github.com/FanZT6/MedEnv.git
cd MedEnv
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env

# 用合成病例检查全部四个子集，无需启动模型服务。
bash scripts/run_all.sh --case_dir data/synthetic --dry_run
```

在 `.env` 中设置数据路径、模型路径、tokenizer、API 地址和 GPU：

```bash
MEDENV_BENCH_ROOT=/path/to/bench
DOCTOR_MODEL_PATH=/path/to/doctor-model
TOKENIZER_PATH=/path/to/doctor-model
PATIENT_MODEL_PATH=/path/to/patient-model
JUDGE_MODEL_PATH=/path/to/judge-model
DOCTOR_GPUS=0
PATIENT_GPUS=1
JUDGE_GPUS=2
```

按[运行指南](docs/RUNNING.md)安装并分别启动 Doctor、Patient、Judge、检索和 Grounding 服务后，即可推理：

```bash
# 有 CXR 子集：文本模型通过报告获取胸片信息。
bash scripts/run_bench.sh --cxr_mode reports --max_cases 10 --n_parallel_agents 1

# 无 CXR 子集。
bash scripts/run_bench.sh --no_cxr --max_cases 10 --n_parallel_agents 1

# 指定子集，运行全部病例，每例重复 3 次。
bash scripts/run_bench.sh --subdir with_img/hosp_only --max_cases 0 --repeat_k 3

# 运行全部四个子集。
bash scripts/run_all.sh --n_parallel_agents 1
```

使用图像输入时，将 Doctor 换成支持视觉的模型及其 tokenizer，并设置 `--cxr_mode vision`。两种 CXR 模式均会提供报告信息，视觉模式额外传入图像。

默认结果路径为 `outputs/<run>/<subset>/`，包含运行配置 `config.json`、评测指标 `metrics.json` 和 `trajectories/` 下的逐例轨迹。

## 修改设置

| 要修改什么 | 修改位置 |
|---|---|
| 模型、API 地址、GPU、数据路径 | [`.env.example`](.env.example)：复制为 `.env` 后编辑 |
| 样本数、轮数、重复次数、采样温度、并发数 | 命令行参数；默认值在 [`medenv/cli.py`](medenv/cli.py) 的 `build_parser()` |
| 医生提示词 | [`rllm/agents/system_prompts.py`](rllm/agents/system_prompts.py) |
| 患者回答与检查结果的生成规则 | [`dialog_tool.py`](rllm/tools/med_tool/dialog_tool.py)、[`exam_result_tool.py`](rllm/tools/med_tool/exam_result_tool.py) |
| 启用的工具 | [`medenv/cli.py`](medenv/cli.py) 的 `run_configuration()` |
| Judge 提示词、评分逻辑 | [`med_diagnosis_reward.py`](rllm/rewards/med_diagnosis_reward.py) |
| 数据字段与医生的初始主诉 | [`medenv/data.py`](medenv/data.py) |

例如，调整推理预算和采样参数：

```bash
bash scripts/run_bench.sh \
  --max_cases 100 --repeat_k 3 --max_steps 20 \
  --temperature 0.2 --top_p 0.9 --n_parallel_agents 4
```

更多提示词、工具、检索和评分的修改片段见[配置指南](docs/CONFIGURATION.md)。

## 数据与许可

Benchmark 共 5,405 个病例，分布在 `with_img/ed_hosp`、`with_img/hosp_only`、`without_img/ed_hosp` 和 `without_img/hosp_only` 四个子集。推理所需的数据结构、访问要求和合成样例见[数据说明](data/README.md)。

代码采用 [Apache-2.0](LICENSE)，归属说明见 [NOTICE](NOTICE)。数据和模型遵循各自的访问条件与许可。
