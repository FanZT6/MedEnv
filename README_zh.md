<div align="center">

# MedEnv

### Scaling Multimodal Virtual Medical Environments for Long-Horizon Diagnosis

**MICCAI 2026**

[论文](https://papers.miccai.org/miccai-2026/paper/5084_paper.pdf) · [English](README.md) · [推理指南](docs/RUNNING.md) · [配置说明](docs/CONFIGURATION.md)

**真实 EHR 支撑 · 多模态 · 交互式 · 可扩展**

</div>

## 目录

- [概述](#概述)
  - [环境与交互](#环境与交互)
  - [智能体训练与结果](#智能体训练与结果)
- [开始使用](#开始使用)
  - [安装](#安装)
  - [数据准备](#数据准备)
  - [快速运行](#快速运行)
- [仓库结构](#仓库结构)
- [公开范围与 MIMIC 数据](#公开范围与-mimic-数据)
- [引用](#引用)
- [许可](#许可)

## 概述

**MedEnv** 将真实临床记录转化为面向长程诊断的交互式环境。医生智能体通过主动问诊、申请检查、检索指南和查看胸片逐步获取证据，最终给出诊断。患者与检查模拟器结合患者自身的 EHR 证据和疾病级知识，支持一致的多轮交互。论文进一步利用该环境训练诊断智能体 **Diagnosis-R1**，通过基于置信度的过程监督学习有价值的证据获取策略。

### 环境与交互

<p align="center">
  <img src="https://github.com/user-attachments/assets/a53a72ce-aa5f-4a99-ab8c-03c7614bd01b" width="680" alt="MedEnv 环境：医生智能体连接患者模拟器、检查模拟器、指南检索与视觉工具。">
</p>

| 组件 | 作用 |
|---|---|
| 患者模拟器 | 回答病史、症状和个人感受相关问题。 |
| 检查模拟器 | 返回体格检查、实验室检查和影像证据。 |
| 检索与视觉工具 | 支持指南检索、胸片查看和视觉定位。 |
| 证据路由 | 优先查询病例 EHR，再查询疾病知识；两者均无法回答时使用兜底回复。 |

![数据构建与多模拟器交互](https://github.com/user-attachments/assets/042c5728-1ccf-4ddd-b2b8-5daff3e4beca)

*从 EHR 构建角色专属证据，并通过工具调用开展多轮交互。所提供图示包含早期统计；海报报告的规模为 **496K+ 条轨迹**和**约 4.9K 条疾病级知识条目**。*

- **数据来源：** MIMIC-IV、MIMIC-IV-Note，以及可配对的 MIMIC-CXR 检查。
- **病例构建：** 整理诊断阶段证据，移除早期记录中的明确诊断表述，保留诊断标签用于评测。
- **评测指标：** 最终诊断准确率、有效交互轮次和平均交互长度；交互轨迹可用于分析重复提问、过早结束诊断等问题。

### 智能体训练与结果

论文使用约 3K 条专家轨迹对 Qwen3-VL-8B-Instruct 进行冷启动训练，再通过 GRPO 结合结果奖励与密集过程奖励；过程奖励依据模型对参考诊断的置信度增益计算。每例进行八次随机采样，过滤全部正确或全部错误的病例。

**海报报告的诊断准确率（%）：**

| 模型 | 无图像 | 有图像 |
|---|---:|---:|
| Qwen3-VL-8B | 10.28 | 12.16 |
| MedGemma-27B | 27.75 | 28.95 |
| Gemini-3-Flash | 34.15 | 33.97 |
| GPT 5 | 34.84 | 35.86 |
| Qwen3.5-397B-A17B | 35.99 | 37.56 |
| **Diagnosis-R1** | **41.95** | **41.15** |

**训练消融：** 基础模型 → SFT → 仅结果奖励的 RL → Diagnosis-R1，无图像准确率依次为 **10.28 → 27.54 → 31.51 → 41.95%**。扩展训练轨迹的收益在约 50K 条附近逐渐饱和；图像输入并未在所有模型上带来一致提升。

## 开始使用

### 安装

推荐 **Python 3.11**，并根据硬件安装合适的 PyTorch 版本。

```bash
git clone https://github.com/FanZT6/MedEnv.git
cd MedEnv
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

本地运行检索和 Grounding 服务时，使用 `python -m pip install -e '.[retrieval,grounding]'` 安装可选依赖。模型服务使用[独立的 vLLM 环境](docs/RUNNING.md#3-start-the-model-services)。

### 数据准备

1. 获取所需 MIMIC 访问授权，按照[数据处理流程](data_process/README.md)准备病例。
2. 在 `.env` 中设置 `MEDENV_BENCH_ROOT=/path/to/bench`。
3. 对照[输入格式](data/README.md#input-structure)，验证病例：

```bash
python scripts/validate_data.py --bench-root /path/to/bench
```

历史评测 benchmark 共 **5,405 个病例**：

| 子集 | 病例数 |
|---|---:|
| `with_img/ed_hosp` | 401 |
| `with_img/hosp_only` | 17 |
| `without_img/ed_hosp` | 1,517 |
| `without_img/hosp_only` | 3,470 |

仓库提供 **4 个虚构病例**用于接口检查，不包含真实 benchmark 数据；公开的 CXR 处理流程仅处理报告文本，不能精确重建历史含图像 benchmark。

### 快速运行

**无需模型服务，检查数据流程：**

```bash
bash scripts/run_all.sh --case_dir data/synthetic --dry_run
```

**运行评测：** 在 `.env` 中配置模型路径、tokenizer 和 API 地址，再按照[模型服务配置](docs/RUNNING.md#3-start-the-model-services)和[检索 / Grounding 配置](docs/RUNNING.md#4-start-retrieval-and-grounding)启动服务。

```bash
# 胸片报告基线：with_img/ed_hosp 中的 10 个病例。
bash scripts/run_bench.sh --cxr_mode reports --max_cases 10 --n_parallel_agents 1

# 无胸片子集：without_img/hosp_only。
bash scripts/run_bench.sh --no_cxr --max_cases 10 --n_parallel_agents 1

# 运行全部四个子集的所有病例。
bash scripts/run_all.sh --n_parallel_agents 1
```

| 设置 | 用法 |
|---|---|
| 多模态推理 | 使用支持视觉的 Doctor 及匹配的 tokenizer，设置 `--cxr_mode vision`。两种 CXR 模式均提供报告信息，视觉模式额外提供图像。 |
| 样本预算 | `--max_cases 0` 选择整个子集；`--repeat_k` 控制重复运行次数。 |
| 结果保存 | `outputs/<run>/<subset>/`：`config.json`、`metrics.json` 和 `trajectories/`。 |
| 自定义配置 | [模型、提示词、工具与评分](docs/CONFIGURATION.md) |

## 仓库结构

```text
medenv/        Benchmark 命令行、病例加载与指标计算
rllm/          医生智能体、环境、工具与评分
services/      检索与视觉定位服务
scripts/       服务启动、评测与结果可视化
data_process/  MIMIC 数据处理代码与文档
data/          输入格式与虚构测试病例
docs/          推理指南、配置说明与图示
tests/         离线测试
```

## 公开范围与 MIMIC 数据

| 公开内容 | 不作为 benchmark 数据分发的内容 |
|---|---|
| 处理代码：数据库导入、诊断窗口提取、OSCE 构建、EHR 转换与 ICD 分层抽样 | MIMIC 原始表、临床记录、报告与图像 |
| 推理 / 评测代码和虚构软件测试样例 | 患者 / 住院 / 检查标识符、处理后的病例、抽样清单和逐例交互轨迹 |

- **访问授权：** 按输入需要自行取得 [MIMIC-IV](https://physionet.org/content/mimiciv/)、[MIMIC-IV-Note](https://physionet.org/content/mimic-iv-note/)、[MIMIC-IV-ED](https://physionet.org/content/mimic-iv-ed/) 和 [MIMIC-CXR](https://physionet.org/content/mimic-cxr/) 的授权；使用 JPG 图像时还需遵守 [MIMIC-CXR-JPG](https://physionet.org/content/mimic-cxr-jpg/) 的相应要求。
- **数据使用：** MIMIC 衍生记录和轨迹仍受适用访问条件与数据使用协议约束。去标识化或转换成 JSON 不授予再分发权利；Apache-2.0 仅适用于代码。
- **本地文件：** 真实输入、数据库与处理产物应保存在仓库外。Git 忽略本地 benchmark、检索资产和输出目录，但**不会**过滤手工压缩或上传。保留 `data/README.md` 和 `data/synthetic/`；不再需要运行记录时可删除 `outputs/`。
- **详细说明：** [数据处理流程](data_process/README.md) · [数据指南](data/README.md) · [发布检查记录](docs/DATA_PRIVACY_AUDIT_zh.md)。

## 引用

```bibtex
@inproceedings{FanZhi_MedEnv_MICCAI2026,
  title     = {MedEnv: Scaling Multimodal Virtual Medical Environments for Long-Horizon Diagnosis},
  author    = {Fan, Zhiting and Ren, Xuxiang and Wang, Yuan and Chen, Ruizhe and
               Meng, Zijie and Hu, Keli and Liu, Jiaxiang and Wu, Jian and
               Zhai, Weiqi and Xu, Hongxia and Liu, Zuozhu},
  booktitle = {Medical Image Computing and Computer Assisted Intervention -- MICCAI 2026},
  year      = {2026}
}
```

## 许可

代码采用 [Apache-2.0](LICENSE)，归属说明见 [NOTICE](NOTICE)。数据集与模型权重遵循各自的访问条件和许可。
