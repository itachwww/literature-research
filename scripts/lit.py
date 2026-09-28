#!/usr/bin/env python
"""Literature research CLI for the literature-research Codex skill.

The script intentionally keeps external dependencies optional. It uses stdlib
HTTP/SQLite/CSV/JSON by default, PyYAML if available for profiles, PyMuPDF if
available for PDF text extraction, and httpx/cryptography only for WebVPN.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError
import http.cookiejar


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path.home() / ".literature-research" / "projects"
DEFAULT_RUNTIME_CONFIG = SKILL_ROOT / "assets" / "local_runtime.yaml"
DEFAULT_PDF_FETCH_CONCURRENCY = 4
VERSION = "0.1.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

SEARCH_DEPTH_DEFAULTS = {
    "quick": {
        "max_queries": 80,
        "max_records": 3000,
        "max_per_query": 25,
        "source_max_pages": 1,
        "split_years": False,
        "year_chunk_size": 0,
        "query_strategy": "balanced_matrix",
    },
    "balanced": {
        "max_queries": 300,
        "max_records": 10000,
        "max_per_query": 75,
        "source_max_pages": 3,
        "split_years": False,
        "year_chunk_size": 0,
        "query_strategy": "balanced_matrix",
    },
    "deep": {
        "max_queries": 1000,
        "max_records": 50000,
        "max_per_query": 250,
        "source_max_pages": 10,
        "split_years": True,
        "year_chunk_size": 1,
        "query_strategy": "exhaustive_pairwise",
    },
    "exhaustive": {
        "max_queries": 0,
        "max_records": 0,
        "max_per_query": 1000,
        "source_max_pages": 50,
        "split_years": True,
        "year_chunk_size": 1,
        "query_strategy": "exhaustive_pairwise",
    },
}


@dataclass
class SearchPlan:
    depth: str
    query_strategy: str
    max_queries: int
    max_records: int
    max_per_query: int
    source_max_pages: int
    split_years: bool
    year_chunk_size: int
    full_pairwise_count: int
    generated_query_count: int = 0
    truncated_queries: bool = False


@dataclass
class SourceSearchResult:
    papers: List[Dict[str, Any]]
    total_available: Optional[int] = None
    pages_fetched: int = 0
    truncated: bool = False


METHOD_PATTERNS = [
    r"\bdensity functional theory\b",
    r"\bDFT\b",
    r"\bmolecular dynamics\b",
    r"\bMD simulation",
    r"\btheoretical simulation",
    r"\bdata[- ]driven\b",
    r"\bkinetic modeling\b",
    r"\brecursive kinetic model(?:ing)?\b",
    r"\bcomputational (?:study|screening|model|simulation)",
    r"\bmicrokinetic",
    r"\bmachine learning\b",
    r"\bmaching learning\b",
    r"\bmachine[- ]learned\b",
    r"\bdeep learning\b",
    r"\bgraph neural network",
    r"\bGNN\b",
    r"\brandom forest\b",
    r"\bXGBoost\b",
    r"\bQSAR\b",
    r"\bhigh-throughput screening\b",
    r"\bfirst-principles\b",
    r"\bab initio\b",
]

DOMAIN_PATTERNS = [
    r"environmental catalysis",
    r"environmental remediation",
    r"water remediation",
    r"water purification",
    r"water decontamination",
    r"water treatment",
    r"air pollution",
    r"air pollution control",
    r"advanced oxidation",
    r"chemical oxidation",
    r"photocatalytic .*degradation",
    r"photocatalysts? for environmental",
    r"electrocatal.*environmental",
    r"electrocatal.*remediation",
    r"persulfate",
    r"peroxymonosulfate",
    r"fenton",
    r"pollutant degradation",
    r"catalytic degradation",
    r"degradation of .*pollut",
    r"organic pollutants?",
    r"organic contaminants?",
    r"contaminant degradation",
    r"pollutant abatement",
    r"antibiotic degradation",
    r"tetracycline degradation",
    r"sulfamethoxazole degradation",
    r"bisphenol A",
    r"disinfection",
    r"wastewater",
    r"contaminant",
    r"\bNOx\b",
    r"nitrogen oxides?",
    r"VOC degradation",
    r"CO2 reduction",
]

ENVIRONMENTAL_CORE_PATTERNS = [
    r"environmental catalysis",
    r"environmental remediation",
    r"water remediation",
    r"water purification",
    r"water harvesting and treatment",
    r"water decontamination",
    r"water treatment",
    r"wastewater",
    r"air pollution",
    r"air pollution control",
    r"advanced oxidation",
    r"photocatalytic .*degradation",
    r"persulfate",
    r"peroxymonosulfate",
    r"fenton",
    r"pollutant",
    r"contaminant",
    r"antibiotic degradation",
    r"tetracycline degradation",
    r"bisphenol A",
    r"disinfection",
    r"\bNOx\b",
    r"nitrogen oxides?",
    r"\bSCR\b",
    r"selective catalytic reduction",
    r"\b(?:nitrate|nitrite)[- ](?:to[- ]ammonia|reduction|electroreduction)\b",
    r"\b(?:nitrate|nitrite) (?:reduction|electroreduction)\b",
]

ENERGY_ONLY_EXCLUSION_PATTERNS = [
    r"\bhydrogen (?:evolution|production|generation|liquefaction)\b",
    r"\bhydrogen adsorption",
    r"\bH\s*2 production\b",
    r"\bHER\b",
    r"\boxygen evolution reaction\b",
    r"\bOER\b",
    r"\burea (?:synthesis|production)\b",
    r"\bsynthesis of urea\b",
    r"\belectrocatalytic urea\b",
    r"\bCO\s*2 reduction\b",
    r"\bCO2RR\b",
    r"\bcarbon dioxide reduction\b",
    r"\bCO\s*2-to-\w+",
]

CATALYSIS_CONTEXT_PATTERNS = [
    r"\b\w*catalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\bcatalytic\b",
    r"\b\w*photocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\bphotocatalytic\b",
    r"\b\w*electrocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\belectrocatalytic\b",
    r"\bphoto[- ]?fenton\b",
    r"\bfenton(?:-like)?\b",
    r"\badvanced oxidation process(?:es)?\b",
    r"\bAOPs?\b",
    r"\bpersulfate activation\b",
    r"\bperoxymonosulfate activation\b",
    r"\bperoxydisulfate activation\b",
    r"\bperacetic acid activation\b",
    r"\bperoxone\b",
    r"\bozonation\b",
    r"\bcatalytic wet peroxidation\b",
    r"\bPMS activation\b",
    r"\bPDS activation\b",
    r"\bactivate(?:s|d|ing)? (?:persulfate|peroxymonosulfate|peroxydisulfate|peracetic acid|PAA|PMS|PDS|H2O2|hydrogen peroxide)\b",
    r"\bactivation of (?:persulfate|peroxymonosulfate|peroxydisulfate|peracetic acid|PAA|PMS|PDS|H2O2|hydrogen peroxide)\b",
    r"\b(?:radical|non[- ]radical) pathway\b",
    r"\b(?:radical|non[- ]radical) mechanism\b",
    r"\bactive sites?\b",
    r"\bcatalytic (?:activity|performance|degradation|oxidation|reduction|ozonation)\b",
    r"\bphotocatalytic degradation\b",
    r"\belectrocatalytic (?:reduction|oxidation|degradation)\b",
    r"\bcatalytic degradation\b",
    r"\bcatalytic ozonation\b",
    r"\bSCR\b",
    r"\bselective catalytic reduction\b",
]

GENERIC_ENV_ML_EXCLUSION_PATTERNS = [
    r"\bair (?:pollution|quality) (?:prediction|forecasting|forecast|index|monitoring)\b",
    r"\bPM\s*2\.?5 (?:prediction|forecasting|forecast)\b",
    r"\bAQI (?:prediction|forecasting|forecast|classification)\b",
    r"\bwater quality (?:prediction|forecasting|classification|assessment)\b",
    r"\bwastewater treatment plants? (?:prediction|forecasting|performance)\b",
    r"\bmunicipal sewage sludge\b",
    r"\btraffic(?:-|\s)?related air pollution\b",
    r"\bmeteorological influences?\b",
    r"\basthmatic attacks?\b",
    r"\blung cancer classification\b",
    r"\bheart failure diagnosis\b",
]

BIBLIOGRAPHIC_EXCLUSION_PATTERNS = [
    r"^\s*correction\b",
    r"^\s*corrigendum\b",
    r"^\s*erratum\b",
    r"^\s*retraction\b",
    r"^\s*editorial\b",
    r"^\s*comment(?:ary)?\b",
    r"\bsupplementary information\b",
    r"\bsupporting information\b",
    r"\bsupplementary materials?\b",
]

EXPERIMENT_PATTERNS = [
    r"\bsynthesi[sz]ed\b",
    r"\bprepared\b",
    r"\bcharacteri[sz]ation\b",
    r"\bXRD\b",
    r"\bSEM\b",
    r"\bTEM\b",
    r"\bXPS\b",
    r"\bFTIR\b",
    r"\bRaman\b",
    r"\bdegradation experiments?\b",
    r"\bexperimental results?\b",
]

DIRECTION_RULES = [
    ("ml_prediction", [r"machine learning", r"deep learning", r"graph neural", r"\bGNN\b", r"random forest", r"XGBoost", r"QSAR", r"data[- ]driven"]),
    ("catalyst_screening", [r"high-throughput", r"screening", r"descriptor", r"materials genome", r"computational screening"]),
    ("molecular_simulation", [r"molecular dynamics", r"\bMD simulation", r"adsorption", r"interface", r"solvent"]),
    ("reaction_pathway", [r"microkinetic", r"reaction pathway", r"free radical", r"non-radical", r"energy barrier", r"transition state"]),
    ("mechanism_dft", [r"\bDFT\b", r"density functional theory", r"first-principles", r"ab initio", r"active site"]),
    ("hybrid_theory_experiment", [r"experimental", r"synthesi", r"characteri"]),
]

DIRECTION_NOTES = {
    "theory_simulation_mechanism": {
        "innovation": "以DFT、第一性原理计算、分子模拟、微观动力学建模和反应路径分析为主线，识别活性位点、吸附构型、电荷转移、自由基/非自由基路径及速控步骤。",
        "value": "适合机制牵引型课题设计，可在大规模实验投入前形成可检验假设、机制图谱和催化剂设计规则。",
        "difficulty": "中到高。需要可靠的结构模型、收敛性检查、中间体/过渡态验证，并谨慎连接真实环境反应条件。",
        "novelty": "中到高；用于未解决的环境催化机制、新型单原子/缺陷/界面位点时创新性更强。",
        "potential": "高；若配合靶向实验验证或同族催化剂对比，出成果潜力更强。",
    },
    "ml_data_driven_screening": {
        "innovation": "以机器学习、QSAR/QSPR、图神经网络、知识抽取和高通量计算描述符为核心，预测催化剂、反应性能、降解效率或可设计空间。",
        "value": "非常适合快速形成课题方向，可将文献数据或计算数据转化为候选材料排序、关键描述符和可实验验证的假设。",
        "difficulty": "中到高。主要风险来自数据质量、数据泄漏、描述符可迁移性和验证可信度。",
        "novelty": "高；当数据集、描述符或模型解释专门面向环境催化问题，而非泛催化任务时更突出。",
        "potential": "很高；如果预测能收敛到少量有说服力的候选催化剂或过程窗口，发表潜力强。",
    },
    "mechanism_dft": {
        "innovation": "用量子化学证据解析活性位点、吸附构型、电荷转移和限速步骤。",
        "value": "可在合成前指导催化剂设计，并解释自由基/非自由基反应路径。",
        "difficulty": "中到高。需要可靠模型、收敛性检查，并与环境化学条件建立严谨联系。",
        "novelty": "中到高；若关联新催化剂结构或争议机制，创新性更强。",
        "potential": "高；适合机制论文和理论指导实验合作。",
    },
    "reaction_pathway": {
        "innovation": "系统描绘污染物降解路径、中间体生成、活化能垒和动力学瓶颈。",
        "value": "能把催化剂设计与选择性、毒性削减和路径调控联系起来。",
        "difficulty": "高。需要较强化学直觉，并验证多个中间体和关键步骤。",
        "novelty": "高；若能厘清争议路径或非自由基过程，创新性突出。",
        "potential": "高；少量关键实验验证即可显著增强说服力。",
    },
    "catalyst_screening": {
        "innovation": "利用描述符和高通量计算，在实验前对催化剂家族进行筛选和排序。",
        "value": "对课题设计价值高，可直接产出候选材料清单和设计规则。",
        "difficulty": "高。需要自动化建模流程、一致的结构处理和稳健的描述符选择。",
        "novelty": "高；探索研究不足的材料空间时尤其明显。",
        "potential": "很高；若结合一到两个实验验证案例，成果潜力强。",
    },
    "ml_prediction": {
        "innovation": "从文献数据或计算数据中学习结构-性能关系。",
        "value": "可加速催化剂优化，并识别隐含关键描述符。",
        "difficulty": "中到高。可靠性主要受数据质量和数据泄漏控制影响。",
        "novelty": "高；若结合可解释模型或新数据集，创新性更强。",
        "potential": "高；数据覆盖充分且预测得到验证时，产出潜力好。",
    },
    "molecular_simulation": {
        "innovation": "模拟静态DFT难以覆盖的吸附、溶剂、界面和传质效应。",
        "value": "适合研究真实水/气界面以及污染物-催化剂相互作用。",
        "difficulty": "高。力场质量和采样充分性可能成为限制。",
        "novelty": "中到高；面向界面环境催化问题时更有价值。",
        "potential": "中到高；与DFT或机器学习结合时更强。",
    },
    "hybrid_theory_experiment": {
        "innovation": "以理论模拟或机器学习作为研究逻辑主线，实验作为靶向验证。",
        "value": "通常是应用型环境催化方向中较容易形成完整论文故事线的路径。",
        "difficulty": "中等。需要计算能力，也需要最小但关键的实验验证。",
        "novelty": "中到高，取决于理论深度和实验验证的针对性。",
        "potential": "很高，尤其适合实际课题推进。",
    },
    "other": {
        "innovation": "混合型计算或数据驱动环境催化研究。",
        "value": "需要进一步人工复核后细化方向。",
        "difficulty": "视具体问题而定。",
        "novelty": "视具体问题而定。",
        "potential": "视具体问题而定。",
    },
}

DIRECTION_LABELS = {
    "theory_simulation_mechanism": "理论模拟机制解析",
    "ml_data_driven_screening": "机器学习与数据驱动筛选",
    "mechanism_dft": "DFT活性位点与机制解析",
    "reaction_pathway": "反应路径与污染物转化机制",
    "catalyst_screening": "高通量催化剂筛选",
    "ml_prediction": "机器学习性能预测",
    "molecular_simulation": "分子模拟与界面过程",
    "hybrid_theory_experiment": "理论/机器学习主导的实验验证",
    "other": "其他计算或数据驱动方向",
}

STATUS_LABELS = {
    "included": "最终纳入",
    "pending_pdf": "待获取PDF",
    "pending_fulltext": "待全文复核",
    "peripheral": "方法非主导/外围",
    "excluded": "已排除",
    "candidate": "候选",
}


def normalize_journal_name(value: Optional[str]) -> str:
    if not value:
        return ""
    text = clean_html_text(value).lower()
    text = text.replace("&amp;", "and").replace("&", "and")
    text = re.sub(r"\bes\s*&?\s*t\b", "environmental science and technology", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def profile_priority_journals(profile: Dict[str, Any]) -> List[str]:
    values = profile.get("priority_journals") or profile.get("tracked_journals") or []
    return [normalize_journal_name(str(v)) for v in values if normalize_journal_name(str(v))]


def profile_q1_journals(profile: Dict[str, Any]) -> List[str]:
    values = profile.get("q1_journals") or profile.get("priority_journals") or profile.get("tracked_journals") or []
    return [normalize_journal_name(str(v)) for v in values if normalize_journal_name(str(v))]


def profile_high_level_journals(profile: Dict[str, Any]) -> List[str]:
    values = (
        profile.get("high_level_journals")
        or profile.get("top_journals")
        or profile.get("priority_journals")
        or profile.get("tracked_journals")
        or []
    )
    output = []
    seen = set()
    for value in values:
        norm = normalize_journal_name(str(value))
        if norm and norm not in seen:
            seen.add(norm)
            output.append(norm)
    return output


def priority_journal_match(journal: Optional[str], profile: Dict[str, Any]) -> str:
    norm = normalize_journal_name(journal)
    if not norm:
        return ""
    targets = profile_priority_journals(profile)
    for target in targets:
        if norm == target:
            return target
    for target in targets:
        if len(target.split()) == 1 or len(target) <= 5:
            continue
        if re.search(rf"(^|\s){re.escape(target)}($|\s)", norm):
            return target
        if len(norm.split()) >= 3 and target.startswith(norm):
            return target
    return ""


def journal_list_match(journal: Optional[str], targets: Sequence[str]) -> str:
    norm = normalize_journal_name(journal)
    if not norm:
        return ""
    for target in targets:
        if norm == target:
            return target
    for target in targets:
        if len(target.split()) == 1 or len(target) <= 5:
            continue
        if re.search(rf"(^|\s){re.escape(target)}($|\s)", norm):
            return target
        if len(norm.split()) >= 3 and target.startswith(norm):
            return target
    return ""


def q1_journal_match(journal: Optional[str], profile: Dict[str, Any]) -> str:
    return journal_list_match(journal, profile_q1_journals(profile))


def high_level_journal_match(journal: Optional[str], profile: Dict[str, Any]) -> str:
    norm = normalize_journal_name(journal)
    if not norm:
        return ""
    for target in profile_high_level_journals(profile):
        if norm == target:
            return target
    return ""


def top_journal_match_for_profile(journal: Optional[str], profile: Dict[str, Any]) -> str:
    if profile_bool(profile, "strict_top_journal_whitelist", False):
        return high_level_journal_match(journal, profile)
    return journal_list_match(journal, profile_high_level_journals(profile))


def profile_excluded_dois(profile: Dict[str, Any]) -> set[str]:
    values = profile.get("exclude_dois") or profile.get("manual_exclude_dois") or []
    if isinstance(values, str):
        values = [values]
    return {normalize_doi(str(v)) for v in values if normalize_doi(str(v))}


def journal_scope_label(journal: Optional[str], profile: Dict[str, Any]) -> str:
    priority = priority_journal_match(journal, profile)
    q1 = q1_journal_match(journal, profile)
    if priority and q1:
        return "优先期刊；Q1白名单"
    if q1:
        return "Q1白名单；非优先期刊"
    return "非Q1或未匹配期刊"


MDPI_JOURNALS = {
    "ai",
    "catalysts",
    "metals",
    "molecules",
    "microorganisms",
    "materials",
    "nanomaterials",
    "processes",
    "quantum beam science",
    "sustainability",
    "toxics",
    "water",
    "international journal of molecular sciences",
}


def is_mdpi_record(doi: Optional[str], journal: Optional[str]) -> bool:
    nd = normalize_doi(doi)
    if nd.startswith("10.3390/"):
        return True
    return normalize_journal_name(journal) in MDPI_JOURNALS


def representative_limit(profile: Dict[str, Any], default: int = 20) -> int:
    try:
        return int(profile.get("representatives_per_direction") or default)
    except Exception:
        return default


def profile_bool(profile: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = profile.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def profile_int(profile: Dict[str, Any], key: str, default: int) -> int:
    value = profile.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def profile_str(profile: Dict[str, Any], key: str, default: str = "") -> str:
    value = profile.get(key, default)
    if value is None:
        return default
    return str(value).strip() or default


def profile_year_bounds(profile: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    year_from = parse_year(str(profile.get("date_from") or "")) if profile.get("date_from") else None
    year_to = parse_year(str(profile.get("date_to") or "")) if profile.get("date_to") else None
    return year_from, year_to


def log(message: str) -> None:
    text = f"[lit] {message}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        safe = text.encode(getattr(sys.stdout, "encoding", None) or "utf-8", errors="replace").decode(getattr(sys.stdout, "encoding", None) or "utf-8", errors="replace")
        print(safe, flush=True)


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def read_mapping_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"mapping file must contain a YAML mapping: {path}")
        return data
    except ImportError:
        return parse_simple_yaml(text)


def runtime_config_path() -> Optional[Path]:
    paths: List[Path] = []
    env_path = os.environ.get("LIT_RESEARCH_RUNTIME_CONFIG")
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.append(DEFAULT_RUNTIME_CONFIG)
    for path in paths:
        if path.exists():
            return path
    return None


def load_runtime_defaults() -> Dict[str, Any]:
    path = runtime_config_path()
    if path:
        try:
            return read_mapping_file(path)
        except Exception as exc:
            log(f"runtime config ignored ({path}): {exc}")
    return {}


def load_env_file(path: Optional[str]) -> None:
    if not path:
        runtime = load_runtime_defaults()
        path = os.environ.get("LIT_RESEARCH_ENV_FILE") or str(runtime.get("default_env_file") or "")
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists() and not Path(str(path)).is_absolute():
        env_path = (Path.cwd() / str(path)).resolve()
    if not env_path.exists():
        if os.environ.get("LIT_RESEARCH_ENV_FILE"):
            raise FileNotFoundError(f"env file not found: {env_path}")
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


RUNTIME_PROFILE_DEFAULT_KEYS = {
    "data_root",
    "pdf_fetch_policy",
    "pdf_methods",
    "institutional_access_mode",
    "webvpn_browser_path",
    "webvpn_user_data_dir",
    "webvpn_login_timeout",
    "webvpn_manual_wait_timeout",
    "webvpn_max_consecutive_failures",
    "require_pdf_for_inclusion",
    "representatives_include_candidates",
}


def merge_runtime_defaults(profile: Dict[str, Any]) -> Dict[str, Any]:
    runtime = load_runtime_defaults()
    if not runtime:
        return profile
    merged = dict(profile)
    for key in RUNTIME_PROFILE_DEFAULT_KEYS:
        if key not in runtime:
            continue
        value = merged.get(key)
        if value is None or value == "":
            merged[key] = runtime[key]
    active_runtime_path = runtime_config_path()
    if active_runtime_path:
        merged["_runtime_config_path"] = str(active_runtime_path.resolve())
    return merged


def load_profile(path: str) -> Dict[str, Any]:
    profile_path = Path(path)
    data = read_mapping_file(profile_path)
    if not isinstance(data, dict):
        raise ValueError("profile must be a mapping")
    data = merge_runtime_defaults(data)
    data["_profile_path"] = str(profile_path.resolve())
    return data


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Small YAML subset parser for the bundled profile template."""
    root: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "":
                root[key] = []
                current_key = key
            else:
                root[key] = coerce_value(value)
                current_key = None
        elif current_key and line.strip().startswith("- "):
            root.setdefault(current_key, []).append(coerce_value(line.strip()[2:]))
    return root


def coerce_value(value: str) -> Any:
    value = value.strip()
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def project_name(profile: Dict[str, Any], override: Optional[str] = None) -> str:
    name = override or str(profile.get("project") or "literature-project")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower()
    return safe or "literature-project"


def project_dir(profile: Dict[str, Any], override_name: Optional[str] = None) -> Path:
    root = profile.get("data_root") or os.environ.get("LIT_RESEARCH_DATA_DIR") or str(DEFAULT_DATA_ROOT)
    return Path(root).expanduser().resolve() / project_name(profile, override_name)


def db_path(profile: Dict[str, Any], override_name: Optional[str] = None) -> Path:
    return project_dir(profile, override_name) / "library.sqlite"


def ensure_project(profile: Dict[str, Any], override_name: Optional[str] = None) -> Path:
    pdir = project_dir(profile, override_name)
    for child in ["pdfs", "exports", "logs"]:
        (pdir / child).mkdir(parents=True, exist_ok=True)
    init_db(pdir / "library.sqlite")
    return pdir


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,
                doi TEXT UNIQUE,
                title TEXT,
                abstract TEXT,
                authors TEXT,
                year INTEGER,
                publication_date TEXT,
                journal TEXT,
                citation_count INTEGER DEFAULT 0,
                is_oa INTEGER DEFAULT 0,
                pdf_url TEXT,
                landing_url TEXT,
                sources TEXT,
                source_ids TEXT,
                search_queries TEXT,
                status TEXT DEFAULT 'candidate',
                direction TEXT,
                method_primary INTEGER DEFAULT 0,
                method_evidence TEXT,
                relevance_score REAL DEFAULT 0,
                pdf_path TEXT,
                pdf_method TEXT,
                pdf_status TEXT,
                pdf_pages INTEGER DEFAULT 0,
                full_text TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
            CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
            CREATE INDEX IF NOT EXISTS idx_papers_direction ON papers(direction);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT,
                details TEXT,
                created_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_run(conn: sqlite3.Connection, command: str, details: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO runs(command, details, created_at) VALUES(?,?,?)",
        (command, json.dumps(details, ensure_ascii=False), utc_now()),
    )
    conn.commit()


def normalize_doi(doi: Optional[str]) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = doi.strip().rstrip(".,;)")
    return doi.lower()


def paper_id(doi: Optional[str], title: str = "") -> str:
    nd = normalize_doi(doi)
    if nd:
        return nd.replace("/", "_").replace(":", "_")
    base = re.sub(r"\s+", " ", title or "").strip().lower()
    return "title_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return cleaned[:160] or "paper"


def json_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, list) else [loaded]
        except Exception:
            return [value]
    return [value]


def http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 60, accept: str = "*/*") -> Tuple[int, Dict[str, str], bytes, str]:
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(params, doseq=True)}"
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            final_url = resp.geturl()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), headers, body, final_url
    except HTTPError as e:
        body = e.read()
        headers = {k.lower(): v for k, v in e.headers.items()}
        return int(e.code), headers, body, url
    except URLError as e:
        raise RuntimeError(f"request failed for {url}: {e}") from e


def decode_json(body: bytes) -> Dict[str, Any]:
    return json.loads(body.decode("utf-8", errors="replace"))


def clean_html_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def text_from_body(body: bytes, headers: Dict[str, str]) -> str:
    ctype = headers.get("content-type", "")
    match = re.search(r"charset=([^;]+)", ctype, flags=re.I)
    encoding = match.group(1) if match else "utf-8"
    return body.decode(encoding, errors="replace")


def make_search_plan(profile: Dict[str, Any], args: argparse.Namespace) -> SearchPlan:
    depth = (getattr(args, "depth", None) or profile_str(profile, "search_depth", "balanced")).lower()
    if depth not in SEARCH_DEPTH_DEFAULTS:
        log(f"unknown search depth '{depth}', using balanced")
        depth = "balanced"
    defaults = SEARCH_DEPTH_DEFAULTS[depth]
    search_terms = [str(x) for x in profile.get("search_terms", [])]
    method_terms = [str(x) for x in profile.get("method_terms", [])]
    full_pairwise_count = len(search_terms) * len(method_terms)
    query_strategy = (
        getattr(args, "query_strategy", None)
        or profile_str(profile, "query_strategy", str(defaults["query_strategy"]))
    ).lower()
    max_queries_arg = getattr(args, "max_queries", None)
    max_records_arg = getattr(args, "max", None)
    max_per_query_arg = getattr(args, "max_per_query", None)
    source_max_pages_arg = getattr(args, "source_max_pages", None)
    split_years_arg = getattr(args, "split_years", None)
    no_split_years_arg = getattr(args, "no_split_years", None)
    year_chunk_size_arg = getattr(args, "year_chunk_size", None)
    split_years_default = profile_bool(profile, "search_split_years", bool(defaults["split_years"]))
    if split_years_arg:
        split_years = True
    elif no_split_years_arg:
        split_years = False
    else:
        split_years = split_years_default
    return SearchPlan(
        depth=depth,
        query_strategy=query_strategy,
        max_queries=(
            int(max_queries_arg)
            if max_queries_arg is not None
            else profile_int(profile, "search_max_queries", int(defaults["max_queries"]))
        ),
        max_records=(
            int(max_records_arg)
            if max_records_arg is not None
            else profile_int(profile, "search_max_records", int(defaults["max_records"]))
        ),
        max_per_query=(
            int(max_per_query_arg)
            if max_per_query_arg is not None
            else profile_int(profile, "search_max_per_query", int(defaults["max_per_query"]))
        ),
        source_max_pages=(
            int(source_max_pages_arg)
            if source_max_pages_arg is not None
            else profile_int(profile, "search_source_max_pages", int(defaults["source_max_pages"]))
        ),
        split_years=split_years,
        year_chunk_size=(
            int(year_chunk_size_arg)
            if year_chunk_size_arg is not None
            else profile_int(profile, "search_year_chunk_size", int(defaults["year_chunk_size"]))
        ),
        full_pairwise_count=full_pairwise_count,
    )


def year_chunks(year_from: Optional[int], year_to: Optional[int], split_years: bool, chunk_size: int) -> List[Tuple[Optional[int], Optional[int]]]:
    if not split_years or not year_from or not year_to or year_to < year_from:
        return [(year_from, year_to)]
    chunk_size = max(1, chunk_size or 1)
    chunks = []
    start = year_from
    while start <= year_to:
        end = min(year_to, start + chunk_size - 1)
        chunks.append((start, end))
        start = end + 1
    return chunks


def ranked_method_terms(profile: Dict[str, Any]) -> List[str]:
    method_terms = [str(x) for x in profile.get("method_terms", [])]
    priority_method_patterns = [
        r"^DFT$",
        r"density functional theory",
        r"machine learning",
        r"data[- ]driven",
        r"kinetic modeling",
        r"QSAR",
        r"high-throughput",
        r"molecular dynamics",
        r"deep learning",
        r"XGBoost",
        r"first-principles",
    ]

    def method_rank(term: str) -> int:
        for idx, pattern in enumerate(priority_method_patterns):
            if re.search(pattern, term, flags=re.I):
                return idx
        return len(priority_method_patterns)

    return sorted(method_terms, key=lambda value: (method_rank(value), method_terms.index(value)))


def generate_queries(profile: Dict[str, Any], max_queries: int = 80, strategy: str = "balanced_matrix") -> List[str]:
    search_terms = [str(x) for x in profile.get("search_terms", [])]
    ranked_methods = ranked_method_terms(profile)
    custom_queries = [str(x) for x in (profile.get("custom_queries") or profile.get("seed_queries") or [])]
    queries: List[str] = []
    seen: set[str] = set()

    def add_query(topic: str, method: str = "") -> bool:
        query = f"{topic} {method}".strip()
        if not query or query in seen:
            return False
        seen.add(query)
        queries.append(query)
        return max_queries > 0 and len(queries) >= max_queries

    for query in custom_queries:
        if add_query(query):
            return queries

    if search_terms and not ranked_methods:
        for s in search_terms:
            if add_query(s):
                return queries
    elif ranked_methods and not search_terms:
        for m in ranked_methods:
            if add_query(m):
                return queries

    elif strategy in {"exhaustive", "exhaustive_pairwise", "full"} or max_queries <= 0:
        for s in search_terms:
            for m in ranked_methods:
                if add_query(s, m):
                    return queries
    elif strategy in {"priority", "priority_methods"}:
        for m in ranked_methods:
            for s in search_terms:
                if add_query(s, m):
                    return queries
    else:
        # Balanced matrix order samples both topic and method dimensions before
        # densifying any one method family. This is safer for unfamiliar topics
        # when the query budget is smaller than the full Cartesian product.
        topic_count = len(search_terms)
        method_count = len(ranked_methods)
        if topic_count and method_count:
            for layer in range(topic_count * method_count):
                topic_idx = layer % topic_count
                offset = layer // topic_count
                method_idx = (topic_idx + offset) % method_count
                if add_query(search_terms[topic_idx], ranked_methods[method_idx]):
                    return queries

    if profile_bool(profile, "include_topic_only_queries", False):
        for s in search_terms:
            if add_query(s):
                return queries
    if not queries:
        topic = str(profile.get("topic") or "")
        if topic:
            queries.append(topic)
    return queries


def parse_year(date_text: Optional[str]) -> Optional[int]:
    if not date_text:
        return None
    match = re.search(r"(19|20)\d{2}", str(date_text))
    return int(match.group(0)) if match else None


def upsert_paper(conn: sqlite3.Connection, paper: Dict[str, Any], source: str, query: str) -> None:
    doi = normalize_doi(paper.get("doi"))
    title = (paper.get("title") or "").strip()
    if not title and not doi:
        return
    pid = paper_id(doi, title)
    existing = conn.execute("SELECT * FROM papers WHERE id=? OR (doi IS NOT NULL AND doi=?)", (pid, doi or None)).fetchone()
    now = utc_now()
    source_ids = paper.get("source_ids") or {}
    if isinstance(source_ids, str):
        source_ids = {source: source_ids}
    if existing:
        sources = sorted(set(json_list(existing["sources"]) + [source]))
        queries = sorted(set(json_list(existing["search_queries"]) + [query]))
        sid = dict(json.loads(existing["source_ids"] or "{}"))
        sid.update(source_ids)
        citation = max(int(existing["citation_count"] or 0), int(paper.get("citation_count") or 0))
        fields = {
            "title": title or existing["title"],
            "abstract": paper.get("abstract") or existing["abstract"],
            "authors": json.dumps(paper.get("authors") or json_list(existing["authors"]), ensure_ascii=False),
            "year": paper.get("year") or existing["year"],
            "publication_date": paper.get("publication_date") or existing["publication_date"],
            "journal": paper.get("journal") or existing["journal"],
            "citation_count": citation,
            "is_oa": 1 if paper.get("is_oa") or existing["is_oa"] else 0,
            "pdf_url": paper.get("pdf_url") or existing["pdf_url"],
            "landing_url": paper.get("landing_url") or existing["landing_url"],
            "sources": json.dumps(sources, ensure_ascii=False),
            "source_ids": json.dumps(sid, ensure_ascii=False),
            "search_queries": json.dumps(queries, ensure_ascii=False),
            "updated_at": now,
        }
        conn.execute(
            """
            UPDATE papers SET title=:title, abstract=:abstract, authors=:authors, year=:year,
                publication_date=:publication_date, journal=:journal, citation_count=:citation_count,
                is_oa=:is_oa, pdf_url=:pdf_url, landing_url=:landing_url, sources=:sources,
                source_ids=:source_ids, search_queries=:search_queries, updated_at=:updated_at
            WHERE id=:id
            """,
            {**fields, "id": existing["id"]},
        )
    else:
        conn.execute(
            """
            INSERT INTO papers(
                id, doi, title, abstract, authors, year, publication_date, journal,
                citation_count, is_oa, pdf_url, landing_url, sources, source_ids,
                search_queries, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pid,
                doi or None,
                title,
                paper.get("abstract"),
                json.dumps(paper.get("authors") or [], ensure_ascii=False),
                paper.get("year") or parse_year(paper.get("publication_date")),
                paper.get("publication_date"),
                paper.get("journal"),
                int(paper.get("citation_count") or 0),
                1 if paper.get("is_oa") else 0,
                paper.get("pdf_url"),
                paper.get("landing_url"),
                json.dumps([source], ensure_ascii=False),
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps([query], ensure_ascii=False),
                "candidate",
                now,
                now,
            ),
        )


def search_openalex(query: str, year_from: Optional[int], year_to: Optional[int], limit: int, mailto: Optional[str], max_pages: int = 1) -> SourceSearchResult:
    filters = ["type:article"]
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    target = max(1, limit)
    max_pages = max(1, max_pages)
    papers = []
    total_available: Optional[int] = None
    cursor = "*"
    pages = 0
    while len(papers) < target and pages < max_pages:
        params = {
            "search": query,
            "filter": ",".join(filters),
            "per_page": min(200, max(1, target - len(papers))),
            "cursor": cursor,
            "select": "id,doi,title,authorships,abstract_inverted_index,publication_date,primary_location,best_oa_location,locations,open_access,cited_by_count",
        }
        if mailto:
            params["mailto"] = mailto
        status = 0
        body = b""
        for attempt in range(3):
            status, headers, body, _ = http_get("https://api.openalex.org/works", params=params, timeout=45, accept="application/json")
            if status != 429:
                break
            wait = 5 * (attempt + 1)
            log(f"OpenAlex rate limited; waiting {wait}s")
            time.sleep(wait)
        if status != 200:
            log(f"OpenAlex returned {status} for query: {query[:80]}")
            break
        data = decode_json(body)
        meta = data.get("meta") or {}
        if total_available is None and isinstance(meta.get("count"), int):
            total_available = int(meta["count"])
        results = data.get("results", [])
        pages += 1
        if not results:
            break
        before = len(papers)
        for work in results:
            paper = parse_openalex_work(work)
            if paper:
                papers.append(paper)
        cursor = meta.get("next_cursor") or ""
        if not cursor or len(papers) == before:
            break
    truncated = False
    if total_available is not None and len(papers) < total_available:
        truncated = True
    if len(papers) >= target:
        truncated = True
    if pages >= max_pages and (total_available is None or len(papers) < total_available):
        truncated = True
    return SourceSearchResult(papers=papers[:target], total_available=total_available, pages_fetched=pages, truncated=truncated)


def resolve_openalex_source(journal: str, mailto: Optional[str] = None) -> Tuple[Optional[str], List[str], str]:
    params = {
        "search": journal,
        "per_page": 10,
        "select": "id,display_name,issn_l,issn,works_count",
    }
    if mailto:
        params["mailto"] = mailto
    status, headers, body, _ = http_get("https://api.openalex.org/sources", params=params, timeout=45, accept="application/json")
    if status != 200:
        log(f"OpenAlex source lookup returned {status} for journal: {journal}")
        return None, [], ""
    data = decode_json(body)
    target_norm = normalize_journal_name(journal)
    candidates = data.get("results") or []
    best = None
    for item in candidates:
        if normalize_journal_name(item.get("display_name")) == target_norm:
            best = item
            break
    if best is None:
        for item in candidates:
            norm = normalize_journal_name(item.get("display_name"))
            if target_norm in norm or norm in target_norm:
                best = item
                break
    if best is None and candidates:
        best = candidates[0]
    if not best:
        return None, [], ""
    issns = []
    if best.get("issn_l"):
        issns.append(str(best["issn_l"]))
    for value in best.get("issn") or []:
        if value and value not in issns:
            issns.append(str(value))
    return best.get("id"), issns, best.get("display_name") or journal


def search_openalex_source(source_id: str, year_from: Optional[int], year_to: Optional[int], limit: int, mailto: Optional[str], max_pages: int = 50) -> SourceSearchResult:
    filters = ["type:article", f"primary_location.source.id:{source_id}"]
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    return search_openalex_with_filters(filters, year_from, year_to, limit, mailto, max_pages)


def search_openalex_with_filters(filters: List[str], year_from: Optional[int], year_to: Optional[int], limit: int, mailto: Optional[str], max_pages: int = 50) -> SourceSearchResult:
    target = max(1, limit)
    max_pages = max(1, max_pages)
    papers = []
    total_available: Optional[int] = None
    cursor = "*"
    pages = 0
    while len(papers) < target and pages < max_pages:
        params = {
            "filter": ",".join(filters),
            "per_page": min(200, max(1, target - len(papers))),
            "cursor": cursor,
            "select": "id,doi,title,authorships,abstract_inverted_index,publication_date,primary_location,best_oa_location,locations,open_access,cited_by_count",
        }
        if mailto:
            params["mailto"] = mailto
        status = 0
        body = b""
        for attempt in range(3):
            status, headers, body, _ = http_get("https://api.openalex.org/works", params=params, timeout=45, accept="application/json")
            if status != 429:
                break
            wait = 5 * (attempt + 1)
            log(f"OpenAlex rate limited; waiting {wait}s")
            time.sleep(wait)
        if status != 200:
            log(f"OpenAlex returned {status} for filters: {','.join(filters)[:120]}")
            break
        data = decode_json(body)
        meta = data.get("meta") or {}
        if total_available is None and isinstance(meta.get("count"), int):
            total_available = int(meta["count"])
        results = data.get("results", [])
        pages += 1
        if not results:
            break
        before = len(papers)
        for work in results:
            paper = parse_openalex_work(work)
            if paper:
                papers.append(paper)
        cursor = meta.get("next_cursor") or ""
        if not cursor or len(papers) == before:
            break
    truncated = False
    if total_available is not None and len(papers) < total_available:
        truncated = True
    if len(papers) >= target:
        truncated = True
    if pages >= max_pages and (total_available is None or len(papers) < total_available):
        truncated = True
    return SourceSearchResult(papers=papers[:target], total_available=total_available, pages_fetched=pages, truncated=truncated)


def parse_openalex_work(work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = clean_html_text(work.get("title"))
    if not title:
        return None
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    oa = work.get("open_access") or {}
    content_urls = work.get("content_urls") or {}
    best_oa = work.get("best_oa_location") or {}
    pdf_url = content_urls.get("pdf") or best_oa.get("pdf_url") or oa.get("oa_url")
    for loc in [best_oa] + list(work.get("locations") or []):
        if not isinstance(loc, dict):
            continue
        if loc.get("pdf_url"):
            pdf_url = pdf_url or loc.get("pdf_url")
        if not pdf_url and loc.get("is_oa") and loc.get("landing_page_url"):
            pdf_url = loc.get("landing_page_url")
    doi = normalize_doi(work.get("doi"))
    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "abstract": reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
        "publication_date": work.get("publication_date"),
        "year": parse_year(work.get("publication_date")),
        "journal": source.get("display_name"),
        "citation_count": work.get("cited_by_count") or 0,
        "is_oa": bool(oa.get("is_oa")),
        "pdf_url": pdf_url,
        "landing_url": primary.get("landing_page_url"),
        "source_ids": {"openalex": work.get("id")},
    }


def reconstruct_openalex_abstract(index: Optional[Dict[str, List[int]]]) -> Optional[str]:
    if not index:
        return None
    try:
        max_pos = max(pos for positions in index.values() for pos in positions)
        words = [""] * (max_pos + 1)
        for word, positions in index.items():
            for pos in positions:
                words[pos] = word
        return " ".join(words).strip()
    except Exception:
        return None


def search_crossref(query: str, year_from: Optional[int], year_to: Optional[int], limit: int, mailto: Optional[str], max_pages: int = 1) -> SourceSearchResult:
    filters = ["type:journal-article"]
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    target = max(1, limit)
    max_pages = max(1, max_pages)
    papers = []
    total_available: Optional[int] = None
    cursor = "*"
    pages = 0
    while len(papers) < target and pages < max_pages:
        params = {
            "query.bibliographic": query,
            "filter": ",".join(filters),
            "rows": min(100, max(1, target - len(papers))),
            "cursor": cursor,
            "select": "DOI,title,author,published-print,published-online,container-title,is-referenced-by-count,link,URL,abstract",
        }
        if mailto:
            params["mailto"] = mailto
        status, headers, body, _ = http_get("https://api.crossref.org/works", params=params, timeout=45, accept="application/json")
        if status != 200:
            log(f"Crossref returned {status} for query: {query[:80]}")
            break
        data = decode_json(body)
        message = data.get("message") or {}
        if total_available is None and isinstance(message.get("total-results"), int):
            total_available = int(message["total-results"])
        items = message.get("items", [])
        pages += 1
        if not items:
            break
        before = len(papers)
        for item in items:
            paper = parse_crossref_item(item)
            if paper:
                papers.append(paper)
        cursor = message.get("next-cursor") or ""
        if not cursor or len(papers) == before:
            break
    truncated = False
    if total_available is not None and len(papers) < total_available:
        truncated = True
    if len(papers) >= target:
        truncated = True
    if pages >= max_pages and (total_available is None or len(papers) < total_available):
        truncated = True
    return SourceSearchResult(papers=papers[:target], total_available=total_available, pages_fetched=pages, truncated=truncated)


def search_crossref_issn(issn: str, year_from: Optional[int], year_to: Optional[int], limit: int, mailto: Optional[str], max_pages: int = 50) -> SourceSearchResult:
    filters = ["type:journal-article", f"issn:{issn}"]
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    target = max(1, limit)
    max_pages = max(1, max_pages)
    papers = []
    total_available: Optional[int] = None
    cursor = "*"
    pages = 0
    while len(papers) < target and pages < max_pages:
        params = {
            "filter": ",".join(filters),
            "rows": min(100, max(1, target - len(papers))),
            "cursor": cursor,
            "select": "DOI,title,author,published-print,published-online,container-title,is-referenced-by-count,link,URL,abstract",
        }
        if mailto:
            params["mailto"] = mailto
        status, headers, body, _ = http_get("https://api.crossref.org/works", params=params, timeout=45, accept="application/json")
        if status != 200:
            log(f"Crossref returned {status} for ISSN: {issn}")
            break
        data = decode_json(body)
        message = data.get("message") or {}
        if total_available is None and isinstance(message.get("total-results"), int):
            total_available = int(message["total-results"])
        items = message.get("items", [])
        pages += 1
        if not items:
            break
        before = len(papers)
        for item in items:
            paper = parse_crossref_item(item)
            if paper:
                papers.append(paper)
        cursor = message.get("next-cursor") or ""
        if not cursor or len(papers) == before:
            break
    truncated = False
    if total_available is not None and len(papers) < total_available:
        truncated = True
    if len(papers) >= target:
        truncated = True
    if pages >= max_pages and (total_available is None or len(papers) < total_available):
        truncated = True
    return SourceSearchResult(papers=papers[:target], total_available=total_available, pages_fetched=pages, truncated=truncated)


def parse_crossref_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    titles = item.get("title") or []
    title = clean_html_text(titles[0] if titles else "")
    if not title:
        return None
    authors = []
    for a in item.get("author") or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
        if name:
            authors.append(name)
    date_parts = None
    for key in ["published-online", "published-print", "published"]:
        if item.get(key, {}).get("date-parts"):
            date_parts = item[key]["date-parts"][0]
            break
    pub_date = None
    if date_parts:
        y = date_parts[0]
        m = date_parts[1] if len(date_parts) > 1 else 1
        d = date_parts[2] if len(date_parts) > 2 else 1
        pub_date = f"{y:04d}-{m:02d}-{d:02d}"
    journal = None
    if item.get("container-title"):
        journal = item["container-title"][0]
    pdf_url = None
    for link in item.get("link") or []:
        ctype = (link.get("content-type") or "").lower()
        if "pdf" in ctype or str(link.get("URL", "")).lower().endswith(".pdf"):
            pdf_url = link.get("URL")
            break
    abstract = item.get("abstract")
    if abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
    doi = normalize_doi(item.get("DOI"))
    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "abstract": html.unescape(abstract or "").strip() or None,
        "publication_date": pub_date,
        "year": parse_year(pub_date),
        "journal": journal,
        "citation_count": item.get("is-referenced-by-count") or 0,
        "pdf_url": pdf_url,
        "landing_url": item.get("URL"),
        "source_ids": {"crossref": doi or item.get("URL")},
    }


def search_wos(query: str, year_from: Optional[int], year_to: Optional[int], limit: int, max_pages: int = 1) -> SourceSearchResult:
    api_key = os.environ.get("WOS_API_KEY")
    if not api_key:
        return SourceSearchResult(papers=[])
    wos_query = build_wos_query(query, year_from, year_to)
    url = "https://api.clarivate.com/apis/wos-starter/v1/documents"
    target = max(1, limit)
    max_pages = max(1, max_pages)
    papers = []
    total_available: Optional[int] = None
    pages = 0
    for page in range(1, max_pages + 1):
        if len(papers) >= target:
            break
        params = {"q": wos_query, "db": "WOS", "limit": min(50, max(1, target - len(papers))), "page": page}
        req_url = f"{url}?{urlencode(params)}"
        req = Request(req_url, headers={"X-ApiKey": api_key, "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with build_opener().open(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except HTTPError as e:
            log(f"WOS returned {e.code}; check WOS_API_KEY/plan")
            break
        except Exception as e:
            log(f"WOS request failed: {e}")
            break
        metadata = data.get("metadata") or {}
        for key in ("total", "totalRecords", "recordsFound", "totalResults"):
            if total_available is None and isinstance(metadata.get(key), int):
                total_available = int(metadata[key])
        hits = data.get("hits", [])
        pages += 1
        if not hits:
            break
        before = len(papers)
        for hit in hits:
            paper = parse_wos_hit(hit)
            if paper:
                papers.append(paper)
        if len(hits) < params["limit"] or len(papers) == before:
            break
    truncated = False
    if total_available is not None and len(papers) < total_available:
        truncated = True
    if len(papers) >= target:
        truncated = True
    if pages >= max_pages and (total_available is None or len(papers) < total_available):
        truncated = True
    return SourceSearchResult(papers=papers[:target], total_available=total_available, pages_fetched=pages, truncated=truncated)


def build_wos_query(query: str, year_from: Optional[int], year_to: Optional[int]) -> str:
    words = [w for w in re.split(r"\s+", query.replace('"', " ").replace("'", " ")) if len(w) > 2]
    stop = {"the", "and", "for", "with", "from", "using", "based", "study", "high"}
    terms = [w for w in words if w.lower() not in stop][:8]
    ts = " AND ".join(terms) if terms else query
    parts = [f"TS=({ts})"]
    if year_from and year_to:
        parts.append(f"PY=({year_from}-{year_to})")
    elif year_from:
        parts.append(f"PY=({year_from}-2099)")
    return " AND ".join(parts)


def parse_wos_hit(hit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = clean_html_text(hit.get("title"))
    if not title:
        return None
    authors = []
    for a in (hit.get("names") or {}).get("authors") or []:
        if isinstance(a, dict):
            name = a.get("displayName") or a.get("wosStandard")
            if name:
                authors.append(name)
    source = hit.get("source") or {}
    pub_year = source.get("publishYear")
    pub_date = f"{pub_year}-01-01" if pub_year else None
    doi = normalize_doi((hit.get("identifiers") or {}).get("doi"))
    citations = 0
    for c in hit.get("citations") or []:
        if c.get("db") == "WOS":
            citations = c.get("count") or 0
    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "abstract": hit.get("abstract") or "",
        "publication_date": pub_date,
        "year": parse_year(pub_date),
        "journal": source.get("sourceTitle"),
        "citation_count": citations,
        "source_ids": {"wos": hit.get("uid")},
    }


def command_init(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile, args.name)
    profile_copy = pdir / "profile.yaml"
    if Path(args.profile).resolve() != profile_copy.resolve():
        shutil.copyfile(args.profile, profile_copy)
    conn = connect(pdir / "library.sqlite")
    try:
        record_run(conn, "init", {"profile": args.profile, "project": pdir.name})
    finally:
        conn.close()
    log(f"initialized project at {pdir}")


def write_query_coverage(pdir: Path, rows: List[Dict[str, Any]]) -> None:
    fields = [
        "query_index",
        "chunk_index",
        "query",
        "year_from",
        "year_to",
        "source",
        "requested_limit",
        "fetched_records",
        "total_available",
        "pages_fetched",
        "truncated",
    ]
    write_csv(pdir / "exports" / "query_coverage_report.csv", rows, fields)


def write_search_plan_summary(pdir: Path, summary: Dict[str, Any]) -> None:
    path = pdir / "exports" / "search_plan_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def select_journals_for_scope(profile: Dict[str, Any], scope: str) -> List[str]:
    if scope == "high_level":
        journals = profile_high_level_journals(profile)
    elif scope == "q1":
        journals = profile_q1_journals(profile)
    elif scope == "priority":
        journals = profile_priority_journals(profile)
    else:
        journals = [normalize_journal_name(str(v)) for v in profile.get("tracked_journals", []) if normalize_journal_name(str(v))]
    seen = set()
    output = []
    for journal in journals:
        if journal and journal not in seen:
            seen.add(journal)
            output.append(journal)
    return output


def command_search(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    year_from = args.year_from or parse_year(profile.get("date_from"))
    year_to = args.year_to or parse_year(profile.get("date_to")) or dt.date.today().year
    plan = make_search_plan(profile, args)
    queries = generate_queries(profile, max_queries=plan.max_queries, strategy=plan.query_strategy)
    chunks = year_chunks(year_from, year_to, plan.split_years, plan.year_chunk_size)
    plan.generated_query_count = len(queries)
    plan.truncated_queries = bool(plan.max_queries > 0 and plan.full_pairwise_count and len(queries) < plan.full_pairwise_count)
    if plan.truncated_queries:
        log(
            f"query plan is truncated: generated {len(queries)}/{plan.full_pairwise_count} pairwise topic-method queries; "
            "use --depth deep/exhaustive or --max-queries 0 for fuller coverage"
        )
    log(
        f"search plan: depth={plan.depth}, strategy={plan.query_strategy}, queries={len(queries)}, "
        f"year_chunks={len(chunks)}, split_years={plan.split_years}, "
        f"max_records={'unlimited' if plan.max_records <= 0 else plan.max_records}, "
        f"max_per_query={plan.max_per_query}, source_max_pages={plan.source_max_pages}"
    )
    total = 0
    completed_tasks = 0
    expected_tasks = len(queries) * len(chunks) * len(sources)
    global_cap_reached = False
    coverage_rows: List[Dict[str, Any]] = []
    try:
        for i, query in enumerate(queries, 1):
            if plan.max_records > 0 and total >= plan.max_records:
                log(f"global search record budget reached: {total}/{plan.max_records}")
                global_cap_reached = True
                break
            log(f"search {i}/{len(queries)}: {query}")
            for chunk_index, (chunk_from, chunk_to) in enumerate(chunks, 1):
                if plan.max_records > 0 and total >= plan.max_records:
                    global_cap_reached = True
                    break
                year_label = f"{chunk_from or 'open'}-{chunk_to or 'open'}"
                if len(chunks) > 1:
                    log(f"  year chunk {chunk_index}/{len(chunks)}: {year_label}")
                for source in sources:
                    if plan.max_records > 0 and total >= plan.max_records:
                        global_cap_reached = True
                        break
                    try:
                        remaining = plan.max_records - total if plan.max_records > 0 else plan.max_per_query
                        requested = max(1, min(plan.max_per_query, remaining))
                        if source == "openalex":
                            result = search_openalex(query, chunk_from, chunk_to, requested, os.environ.get("OPENALEX_EMAIL"), plan.source_max_pages)
                        elif source == "crossref":
                            result = search_crossref(query, chunk_from, chunk_to, requested, os.environ.get("OPENALEX_EMAIL"), plan.source_max_pages)
                        elif source == "wos":
                            result = search_wos(query, chunk_from, chunk_to, requested, plan.source_max_pages)
                        else:
                            log(f"unknown source skipped: {source}")
                            continue
                        papers = result.papers
                        completed_tasks += 1
                        for paper in papers:
                            upsert_paper(conn, paper, source, query)
                        conn.commit()
                        total += len(papers)
                        coverage_rows.append(
                            {
                                "query_index": i,
                                "chunk_index": chunk_index,
                                "query": query,
                                "year_from": chunk_from or "",
                                "year_to": chunk_to or "",
                                "source": source,
                                "requested_limit": requested,
                                "fetched_records": len(papers),
                                "total_available": result.total_available if result.total_available is not None else "",
                                "pages_fetched": result.pages_fetched,
                                "truncated": "yes" if result.truncated else "no",
                            }
                        )
                        cap_note = " truncated" if result.truncated else ""
                        total_note = f"/{result.total_available}" if result.total_available is not None else ""
                        log(f"    {source}: {len(papers)}{total_note} records, pages={result.pages_fetched}{cap_note}")
                        time.sleep(args.pause)
                    except Exception as e:
                        log(f"    {source} failed: {e}")
        write_query_coverage(pdir, coverage_rows)
        write_search_plan_summary(
            pdir,
            {
                "created_at": utc_now(),
                "sources": sources,
                "depth": plan.depth,
                "query_strategy": plan.query_strategy,
                "queries": len(queries),
                "full_pairwise_queries": plan.full_pairwise_count,
                "query_plan_truncated": plan.truncated_queries,
                "year_chunks": len(chunks),
                "split_years": plan.split_years,
                "year_chunk_size": plan.year_chunk_size,
                "max_records": plan.max_records,
                "max_per_query": plan.max_per_query,
                "source_max_pages": plan.source_max_pages,
                "expected_source_tasks": expected_tasks,
                "completed_source_tasks": completed_tasks,
                "global_cap_reached": global_cap_reached,
                "raw_records": total,
            },
        )
        record_run(
            conn,
            "search",
            {
                "sources": sources,
                "depth": plan.depth,
                "query_strategy": plan.query_strategy,
                "queries": len(queries),
                "year_chunks": len(chunks),
                "split_years": plan.split_years,
                "year_chunk_size": plan.year_chunk_size,
                "full_pairwise_queries": plan.full_pairwise_count,
                "query_plan_truncated": plan.truncated_queries,
                "max_records": plan.max_records,
                "max_per_query": plan.max_per_query,
                "source_max_pages": plan.source_max_pages,
                "raw_records": total,
            },
        )
    finally:
        conn.close()
    log(f"search complete; raw records processed: {total}")


def command_search_journals(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    year_from = args.year_from or parse_year(profile.get("date_from"))
    year_to = args.year_to or parse_year(profile.get("date_to")) or dt.date.today().year
    years = list(range(year_from or year_to, (year_to or year_from) + 1)) if year_from and year_to else [year_to or year_from or dt.date.today().year]
    journals = select_journals_for_scope(profile, args.journal_scope)
    if args.journals:
        journals = [normalize_journal_name(j) for j in args.journals.split(";") if normalize_journal_name(j)]
    limit = args.max_per_journal_year or profile_int(profile, "journal_sweep_max_per_year", 10000)
    max_pages = args.source_max_pages or profile_int(profile, "journal_sweep_source_max_pages", 100)
    mailto = os.environ.get("OPENALEX_EMAIL")
    total = 0
    coverage_rows: List[Dict[str, Any]] = []
    source_cache: Dict[str, Tuple[Optional[str], List[str], str]] = {}
    expected_tasks = 0
    completed_tasks = 0
    try:
        log(f"journal sweep: journals={len(journals)}, years={len(years)}, sources={','.join(sources)}, limit={limit}, pages={max_pages}")
        for journal_index, journal in enumerate(journals, 1):
            source_id = None
            issns: List[str] = []
            display_name = journal
            if "openalex" in sources or "crossref" in sources:
                source_id, issns, display_name = source_cache.get(journal) or resolve_openalex_source(journal, mailto)
                source_cache[journal] = (source_id, issns, display_name)
                if source_id:
                    log(f"journal {journal_index}/{len(journals)}: {display_name} ({source_id})")
                else:
                    log(f"journal {journal_index}/{len(journals)}: {journal} (OpenAlex source unresolved)")
            for year in years:
                query_label = f"journal-sweep: {display_name} {year}"
                for source in sources:
                    if source == "openalex" and not source_id:
                        continue
                    if source == "crossref" and not issns:
                        continue
                    expected_tasks += 1
                    try:
                        if source == "openalex":
                            result = search_openalex_source(source_id or "", year, year, limit, mailto, max_pages)
                        elif source == "crossref":
                            result = search_crossref_issn(issns[0], year, year, limit, mailto, max_pages)
                        else:
                            log(f"journal sweep source not supported yet: {source}")
                            continue
                        completed_tasks += 1
                        for paper in result.papers:
                            upsert_paper(conn, paper, f"{source}_journal_sweep", query_label)
                        conn.commit()
                        total += len(result.papers)
                        coverage_rows.append(
                            {
                                "query_index": journal_index,
                                "chunk_index": year,
                                "query": query_label,
                                "year_from": year,
                                "year_to": year,
                                "source": f"{source}_journal_sweep",
                                "requested_limit": limit,
                                "fetched_records": len(result.papers),
                                "total_available": result.total_available if result.total_available is not None else "",
                                "pages_fetched": result.pages_fetched,
                                "truncated": "yes" if result.truncated else "no",
                            }
                        )
                        cap_note = " truncated" if result.truncated else ""
                        total_note = f"/{result.total_available}" if result.total_available is not None else ""
                        log(f"  {year} {source}: {len(result.papers)}{total_note} records, pages={result.pages_fetched}{cap_note}")
                        time.sleep(args.pause)
                    except Exception as e:
                        log(f"  {year} {source} failed for {display_name}: {e}")
        write_query_coverage(pdir, coverage_rows)
        write_search_plan_summary(
            pdir,
            {
                "created_at": utc_now(),
                "mode": "journal_sweep",
                "journal_scope": args.journal_scope,
                "journals": len(journals),
                "years": years,
                "sources": sources,
                "max_per_journal_year": limit,
                "source_max_pages": max_pages,
                "expected_source_tasks": expected_tasks,
                "completed_source_tasks": completed_tasks,
                "global_cap_reached": False,
                "query_plan_truncated": False,
                "raw_records": total,
            },
        )
        record_run(conn, "search-journals", {"journal_scope": args.journal_scope, "journals": len(journals), "years": years, "sources": sources, "raw_records": total})
    finally:
        conn.close()
    log(f"journal sweep complete; raw records processed: {total}")


def apply_proxy(url: str, proxy_url: Optional[str], proxy_type: str) -> str:
    if not proxy_url:
        return url
    if proxy_type == "prefix":
        return proxy_url + quote(url, safe="")
    if proxy_type == "suffix":
        parsed = urlparse(url)
        host = (parsed.netloc or "").replace(".", "-")
        return f"{parsed.scheme}://{host}{proxy_url}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
    return url


def is_pdf_bytes(body: bytes, headers: Dict[str, str]) -> bool:
    ctype = headers.get("content-type", "").lower()
    return body[:4] == b"%PDF" or "application/pdf" in ctype or "pdf" in ctype


def write_pdf_if_valid(path: Path, body: bytes, headers: Dict[str, str]) -> bool:
    if not is_pdf_bytes(body, headers):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path.exists() and path.stat().st_size > 1000


def pdf_download_timeout() -> int:
    raw = os.environ.get("LIT_PDF_TIMEOUT", "20").strip()
    try:
        return max(5, min(120, int(float(raw))))
    except ValueError:
        return 20


def webvpn_timeout() -> int:
    raw = os.environ.get("LIT_WEBVPN_TIMEOUT", "90").strip()
    try:
        return max(10, min(180, int(float(raw))))
    except ValueError:
        return 90


def webvpn_verify_tls() -> bool:
    """Verify TLS by default; allow an explicit opt-out for legacy proxies."""
    raw = os.environ.get("LIT_WEBVPN_VERIFY_TLS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def download_pdf_url(url: str, out_path: Path) -> bool:
    timeout = pdf_download_timeout()
    try:
        status, headers, body, _ = http_get(url, timeout=timeout, accept="application/pdf,text/html,*/*")
        if status == 200 and write_pdf_if_valid(out_path, body, headers):
            return True
    except Exception:
        pass
    # Some publisher CDNs block browser-like user agents but allow a plain
    # urllib client for direct PDF endpoints.
    for headers_in in (
        {"Accept": "application/pdf,*/*"},
        {"Accept": "application/pdf,*/*", "User-Agent": "Python-urllib/3.11"},
    ):
        try:
            req = Request(url, headers=headers_in)
            with build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar())).open(req, timeout=timeout) as resp:
                body = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                if int(resp.status) == 200 and write_pdf_if_valid(out_path, body, headers):
                    return True
        except Exception:
            continue
    try:
        from curl_cffi import requests as curl_requests  # type: ignore

        for impersonate in ("chrome124", "safari17_0"):
            try:
                resp = curl_requests.get(
                    url,
                    impersonate=impersonate,
                    timeout=timeout,
                    headers={"accept": "application/pdf,text/html,*/*"},
                )
                headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                if int(resp.status_code) == 200 and write_pdf_if_valid(out_path, bytes(resp.content), headers):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def extract_pmcid(text: str) -> str:
    match = re.search(r"\bPMC\d+\b", text or "", flags=re.I)
    return match.group(0).upper() if match else ""


def download_pmc_oa_pdf(pmcid: str, out_path: Path) -> bool:
    pmcid = extract_pmcid(pmcid)
    if not pmcid:
        return False
    try:
        status, headers, body, _ = http_get(
            "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
            params={"id": pmcid},
            timeout=45,
            accept="application/xml,text/xml,*/*",
        )
        if status != 200:
            return False
        xml_text = text_from_body(body, headers)
        package_urls = re.findall(r'href=["\']([^"\']+?\.tar\.gz)["\']', xml_text, flags=re.I)
    except Exception:
        return False
    for package_url in package_urls:
        package_candidates = [package_url]
        if package_url.startswith("ftp://ftp.ncbi.nlm.nih.gov/"):
            https_url = "https://ftp.ncbi.nlm.nih.gov/" + package_url[len("ftp://ftp.ncbi.nlm.nih.gov/") :]
            package_candidates.append(https_url)
            package_candidates.append(https_url.replace("/pub/pmc/oa_package/", "/pub/pmc/deprecated/oa_package/"))
        seen_packages = set()
        for resolved_package_url in package_candidates:
            if resolved_package_url in seen_packages:
                continue
            seen_packages.add(resolved_package_url)
            try:
                status, headers, body, _ = http_get(resolved_package_url, timeout=120, accept="application/gzip,*/*")
                if status != 200 or len(body) < 1000:
                    continue
                with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
                    pdf_members = [
                        m for m in archive.getmembers()
                        if m.isfile() and m.name.lower().endswith(".pdf") and (m.size or 0) > 1000
                    ]
                    pdf_members.sort(key=lambda m: m.size or 0, reverse=True)
                    for member in pdf_members:
                        extracted = archive.extractfile(member)
                        if not extracted:
                            continue
                        pdf_body = extracted.read()
                        if write_pdf_if_valid(out_path, pdf_body, {"content-type": "application/pdf"}):
                            return True
            except Exception:
                continue
    return False


def europe_pmc_locations(doi: str) -> Tuple[List[str], List[str]]:
    nd = normalize_doi(doi)
    if not nd:
        return [], []
    try:
        status, headers, body, _ = http_get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f'DOI:"{nd}"', "format": "json", "pageSize": "3"},
            timeout=45,
            accept="application/json,*/*",
        )
        if status != 200:
            return [], []
        data = decode_json(body)
    except Exception:
        return [], []
    pmcids: List[str] = []
    pdf_urls: List[str] = []
    for result in (data.get("resultList") or {}).get("result") or []:
        pmcid = extract_pmcid(result.get("pmcid") or result.get("pmcidId") or "")
        if pmcid and pmcid not in pmcids:
            pmcids.append(pmcid)
        url_list = ((result.get("fullTextUrlList") or {}).get("fullTextUrl") or [])
        for item in url_list:
            url = item.get("url")
            style = (item.get("documentStyle") or "").lower()
            if url and ("pdf" in style or ".pdf" in url.lower()) and url not in pdf_urls:
                pdf_urls.append(url)
    return pmcids, pdf_urls


def normalize_hal_file_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.netloc.endswith(".hal.science") or parsed.netloc == "hal.science":
        return "https://hal.archives-ouvertes.fr" + parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return url


def hal_pdf_urls(doi: str) -> List[str]:
    nd = normalize_doi(doi)
    if not nd:
        return []
    urls: List[str] = []
    queries = [f'doiId_s:"{nd}"', nd]
    for query in queries:
        try:
            status, headers, body, _ = http_get(
                "https://api.archives-ouvertes.fr/search/",
                params={"q": query, "fl": "halId_s,doiId_s,files_s,title_s", "rows": "5"},
                timeout=45,
                accept="application/json,*/*",
            )
            if status != 200:
                continue
            data = decode_json(body)
        except Exception:
            continue
        for doc in (data.get("response") or {}).get("docs") or []:
            doc_doi = normalize_doi(doc.get("doiId_s"))
            if doc_doi and doc_doi != nd:
                continue
            for file_url in doc.get("files_s") or []:
                if ".pdf" not in str(file_url).lower():
                    continue
                for candidate in (normalize_hal_file_url(str(file_url)), str(file_url)):
                    if candidate and candidate not in urls:
                        urls.append(candidate)
        if urls:
            break
    return urls


def semantic_scholar_pdf_urls(doi: str) -> List[str]:
    nd = normalize_doi(doi)
    if not nd:
        return []
    try:
        status, headers, body, _ = http_get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(nd, safe='')}",
            params={"fields": "title,openAccessPdf,externalIds"},
            timeout=35,
            accept="application/json,*/*",
        )
        if status != 200:
            return []
        data = decode_json(body)
    except Exception:
        return []
    pdf = data.get("openAccessPdf") or {}
    url = pdf.get("url")
    if not url:
        return []
    return [url]


def crossref_pdf_urls(doi: str) -> List[str]:
    nd = normalize_doi(doi)
    if not nd:
        return []
    mailto = os.environ.get("OPENALEX_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    params = {"select": "DOI,link,URL"}
    if mailto:
        params["mailto"] = mailto
    try:
        status, headers, body, _ = http_get(
            f"https://api.crossref.org/works/{quote(nd, safe='')}",
            params=params,
            timeout=35,
            accept="application/json,*/*",
        )
        if status != 200:
            return []
        data = decode_json(body)
    except Exception:
        return []
    urls: List[str] = []
    for link in ((data.get("message") or {}).get("link") or []):
        ctype = (link.get("content-type") or "").lower()
        url = str(link.get("URL") or "")
        intended = (link.get("intended-application") or "").lower()
        if url and ("pdf" in ctype or ".pdf" in url.lower() or intended in {"text-mining", "similarity-checking"}):
            if url not in urls:
                urls.append(url)
    return urls


def unpaywall_pdf_urls(doi: str) -> List[str]:
    data = unpaywall_metadata(doi)
    urls = []
    for loc in [data.get("best_oa_location")] + list(data.get("oa_locations") or []):
        if not loc:
            continue
        for key in ["url_for_pdf", "url"]:
            val = loc.get(key)
            if val and val not in urls:
                urls.append(val)
    return urls


def unpaywall_metadata(doi: str) -> Dict[str, Any]:
    email = (
        os.environ.get("UNPAYWALL_EMAIL")
        or os.environ.get("OPENALEX_EMAIL")
        or os.environ.get("EMAIL_USERNAME")
        or "noreply@openai.com"
    )
    url = f"https://api.unpaywall.org/v2/{quote(doi)}"
    try:
        status, headers, body, _ = http_get(url, params={"email": email}, timeout=30, accept="application/json")
        if status != 200:
            return {"status_code": status}
        return decode_json(body)
    except Exception:
        return {}


def elsevier_pii_variants(pii: str) -> List[str]:
    raw = re.sub(r"[^A-Za-z0-9]", "", pii or "").upper()
    if not raw:
        return []
    variants = [raw]
    match = re.match(r"^S(\d{4})(\d{4})(\d{2})(\d+)([A-Z0-9])$", raw)
    if match:
        issn_a, issn_b, year, article, check = match.groups()
        variants.append(f"S{issn_a}-{issn_b}({year}){article}-{check}")
    deduped: List[str] = []
    for item in variants:
        if item not in deduped:
            deduped.append(item)
    return deduped


def publisher_pdf_candidates(doi: str) -> List[str]:
    nd = normalize_doi(doi)
    if not nd:
        return []
    urls: List[str] = []

    def add(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    if nd.startswith("10.1021/"):
        add(f"https://pubs.acs.org/doi/pdf/{nd}")
        add(f"https://pubs.acs.org/doi/epdf/{nd}")
        add(f"https://pubs.acs.org/doi/pdfdirect/{nd}?download=true")
    if nd.startswith("10.1002/"):
        add(f"https://onlinelibrary.wiley.com/doi/pdf/{nd}")
        add(f"https://onlinelibrary.wiley.com/doi/epdf/{nd}")
        add(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{nd}?download=true")
        add(f"https://chemistry-europe.onlinelibrary.wiley.com/doi/pdf/{nd}")
        add(f"https://chemistry-europe.onlinelibrary.wiley.com/doi/epdf/{nd}")
        add(f"https://advanced.onlinelibrary.wiley.com/doi/pdf/{nd}")
        add(f"https://advanced.onlinelibrary.wiley.com/doi/epdf/{nd}")
    if nd.startswith("10.1080/"):
        add(f"https://www.tandfonline.com/doi/pdf/{nd}?download=true")
        add(f"https://www.tandfonline.com/doi/epdf/{nd}?needAccess=true")
        add(f"https://www.tandfonline.com/doi/full/{nd}")
    if nd.startswith("10.1007/"):
        add(f"https://link.springer.com/content/pdf/{nd}.pdf")
        add(f"https://link.springer.com/article/{nd}")
    if nd.startswith("10.1109/"):
        ieee_match = re.search(r"\.(\d+)$", nd)
        if ieee_match:
            article_number = ieee_match.group(1)
            add(f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={article_number}")
            add(f"https://ieeexplore.ieee.org/document/{article_number}")
    if nd.startswith("10.1038/"):
        article_id = nd.rsplit("/", 1)[-1]
        add(f"https://www.nature.com/articles/{article_id}.pdf")
    if nd.startswith("10.1126/"):
        add(f"https://www.science.org/doi/pdf/{nd}?download=true")
        add(f"https://www.science.org/doi/pdf/{nd}")
    if nd.startswith("10.1039/"):
        article_id = nd.rsplit("/", 1)[-1].lower()
        journal_code = article_id[2:4] if len(article_id) >= 4 else ""
        year_digit = article_id[1:2]
        if article_id.startswith("d") and year_digit.isdigit() and journal_code:
            add(f"https://pubs.rsc.org/en/content/articlepdf/202{year_digit}/{journal_code}/{article_id}")
        add(f"https://pubs.rsc.org/en/content/articlepdf/{article_id}")
    if nd.startswith("10.1016/"):
        try:
            status, headers, body, final_url = http_get(f"https://doi.org/{nd}", timeout=45, accept="text/html,*/*")
            text = text_from_body(body, headers) if status == 200 else ""
            candidates = [final_url, text]
            for source in candidates:
                for pii in re.findall(r"(?:retrieve/pii/|/pii/)([A-Z0-9]+)", source or "", flags=re.I):
                    add(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?download=true")
                    add(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdf")
                    add(f"https://linkinghub.elsevier.com/retrieve/pii/{pii}/pdfft?download=true")
                    for pii_variant in elsevier_pii_variants(pii):
                        add(f"https://www.cell.com/action/showPdf?pii={quote(pii_variant, safe='')}")
        except Exception:
            pass
    if nd.startswith("10.1088/"):
        add(f"https://iopscience.iop.org/article/{nd}/pdf")
    if nd.startswith("10.3390/"):
        m = re.match(r"10\.3390/([a-z]+)(\d{2})(\d{2})(\d+)$", nd, flags=re.I)
        mdpi_journals = {
            "catal": "catalysts",
            "toxics": "toxics",
            "water": "water",
            "environments": "environments",
            "sustainability": "sustainability",
            "nanomaterials": "nanomaterials",
            "materials": "materials",
            "molecules": "molecules",
            "processes": "processes",
            "chemengineering": "chemengineering",
        }
        if m:
            code, volume, issue, article = m.groups()
            journal_slug = mdpi_journals.get(code.lower(), code.lower())
            file_id = f"{journal_slug}-{int(volume):02d}-{int(article):05d}.pdf"
            add(f"https://pub.mdpi-res.com/{journal_slug}/{journal_slug}-{int(volume):02d}-{int(article):05d}/article_deploy/{file_id}")
            add(f"https://mdpi-res.com/d_attachment/{journal_slug}/{journal_slug}-{int(volume):02d}-{int(article):05d}/article_deploy/{file_id}")
            add(f"https://www.mdpi.com/article/10.3390/{code}{volume}{issue}{article}/pdf")
    return urls


def is_supplementary_pdf_url(url: str) -> bool:
    lower = html.unescape(str(url or "")).lower()
    return any(
        marker in lower
        for marker in (
            "supporting information",
            "supplementary information",
            "supplementary material",
            "supplementary materials",
            "supporting-info",
            "/doi/suppl/",
            "/suppl_file/",
            "suppl_file",
            "_si_",
            "-si-",
            "_supp",
            "suppinfo",
            "supportinfo",
        )
    )


def extract_pdf_links(page_url: str) -> List[str]:
    try:
        status, headers, body, final_url = http_get(page_url, timeout=60, accept="text/html,application/pdf,*/*")
        if status == 200 and is_pdf_bytes(body, headers):
            return [final_url]
        if status != 200:
            return []
        text = text_from_body(body, headers)
    except Exception:
        return []
    links: List[str] = []
    patterns = [
        r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
        r'href=["\']([^"\']*pdf[^"\']*)["\']',
        r'data-pdf-url=["\']([^"\']+)["\']',
        r'"pdfUrl"\s*:\s*"([^"]+)"',
        r'citation_pdf_url["\s]+content=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for match in re.findall(pat, text, flags=re.I):
            full = urljoin(final_url, html.unescape(match))
            if is_supplementary_pdf_url(full):
                continue
            if full not in links:
                links.append(full)
    return links


def webvpn_fetch_pdf(url: str, out_path: Path) -> bool:
    vpn_url = os.environ.get("WEBVPN_URL")
    username = os.environ.get("WEBVPN_USERNAME")
    password = os.environ.get("WEBVPN_PASSWORD")
    if not (vpn_url and username and password):
        return False
    try:
        import binascii
        import httpx  # type: ignore
        from cryptography.hazmat.backends import default_backend  # type: ignore
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore
    except Exception:
        return False

    key = b"wrdvpnisthebest!"
    iv = b"wrdvpnisthebest!"
    marker_hex = binascii.hexlify(key).decode("ascii")

    def enc_host(host: str) -> str:
        cipher = Cipher(algorithms.AES(key), modes.CFB(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(host.encode("utf-8")) + encryptor.finalize()
        return marker_hex + binascii.hexlify(encrypted).decode("ascii")

    def enc_url(target: str) -> str:
        parsed = urlparse(target)
        if not parsed.hostname:
            return target
        host = parsed.hostname
        if parsed.port and not (parsed.scheme == "https" and parsed.port == 443) and not (parsed.scheme == "http" and parsed.port == 80):
            host = f"{host}:{parsed.port}"
        path = parsed.path or ""
        if parsed.query:
            path += f"?{parsed.query}"
        return f"{vpn_url.rstrip('/')}/{parsed.scheme}/{enc_host(host)}{path}"

    try:
        with httpx.Client(
            timeout=webvpn_timeout(),
            follow_redirects=True,
            verify=webvpn_verify_tls(),
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = client.get(vpn_url)
            final_url = str(resp.url)
            match = re.match(r"(https?://[^/]+/cas)", final_url)
            cas_base = (os.environ.get("WEBVPN_CAS_URL") or (match.group(1) if match else "")).rstrip("/")
            if not cas_base:
                return False
            tgt = client.post(
                f"{cas_base}/v1/tickets",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if tgt.status_code not in (200, 201):
                return False
            m = re.search(r'action="([^"]+)"', tgt.text)
            tgt_url = m.group(1) if m else f"{cas_base}/v1/tickets/{tgt.text.strip()}"
            service = f"{vpn_url.rstrip('/')}/login?cas_login=true"
            st = client.post(tgt_url, data={"service": service}).text.strip()
            if not st.startswith("ST-"):
                return False
            client.get(f"{service}&ticket={st}")
            pdf_resp = client.get(enc_url(url))
            body = pdf_resp.content
            headers = {k.lower(): v for k, v in pdf_resp.headers.items()}
            return pdf_resp.status_code == 200 and write_pdf_if_valid(out_path, body, headers)
    except Exception:
        return False


def verify_pdf(path: Path) -> Tuple[bool, int, str]:
    if not path.exists() or path.stat().st_size < 1000:
        return False, 0, ""
    try:
        if path.read_bytes()[:4] != b"%PDF":
            return False, 0, ""
    except Exception:
        return False, 0, ""
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        pages = len(doc)
        text = ""
        for i in range(min(2, pages)):
            text += doc[i].get_text("text")
        doc.close()
        preview = text[:2000]
        normalized_preview = re.sub(r"\s+", " ", preview).strip().lower()
        if re.search(r"^s[- ]?\d*\s*supporting information\b", normalized_preview):
            return False, pages, preview
        if re.search(r"^s[- ]?\d*\s*supplementary information\b", normalized_preview):
            return False, pages, preview
        if re.search(r"^s[- ]?\d*\s*supplementary materials?\b", normalized_preview):
            return False, pages, preview
        if re.search(r"^supporting information\b", normalized_preview):
            return False, pages, preview
        if re.search(r"^supplementary information\b", normalized_preview):
            return False, pages, preview
        if re.search(r"^supplementary materials?\b", normalized_preview):
            return False, pages, preview
        if "supplementary materials for" in normalized_preview[:500]:
            return False, pages, preview
        if "supporting information" in normalized_preview[:300] and not re.search(r"\babstract\b|\bintroduction\b", normalized_preview[:1200]):
            return False, pages, preview
        if "supplementary information" in normalized_preview[:300] and not re.search(r"\babstract\b|\bintroduction\b", normalized_preview[:1200]):
            return False, pages, preview
        if "supplementary materials" in normalized_preview[:300] and not re.search(r"\babstract\b|\bintroduction\b", normalized_preview[:1200]):
            return False, pages, preview
        return pages > 0, pages, text[:2000]
    except Exception:
        return True, 0, ""


def fetch_one_pdf(row: sqlite3.Row, pdf_dir: Path, methods: Sequence[str]) -> Tuple[Optional[str], Optional[str], str, int]:
    doi = row["doi"] or ""
    filename = sanitize_filename(row["id"]) + ".pdf"
    out_path = pdf_dir / filename
    if out_path.exists():
        ok, pages, _ = verify_pdf(out_path)
        if ok:
            return str(out_path), "existing", "ok", pages
    pmcids = []
    pmcid = extract_pmcid(" ".join(str(row[k] or "") for k in ("pdf_url", "landing_url", "source_ids", "sources")))
    if pmcid:
        pmcids.append(pmcid)
    europe_pmc_urls: List[str] = []
    if doi and "unpaywall" in methods:
        found_pmcids, europe_pmc_urls = europe_pmc_locations(doi)
        for found_pmcid in found_pmcids:
            if found_pmcid not in pmcids:
                pmcids.append(found_pmcid)
    for known_pmcid in pmcids:
        if download_pmc_oa_pdf(known_pmcid, out_path):
            ok, pages, _ = verify_pdf(out_path)
            if ok:
                return str(out_path), "pmc_oa", "ok", pages
            out_path.unlink(missing_ok=True)
    candidate_urls: List[Tuple[str, str]] = []
    if row["pdf_url"]:
        candidate_urls.append(("direct", row["pdf_url"]))
    if "unpaywall" in methods and doi:
        candidate_urls.extend(("unpaywall", u) for u in unpaywall_pdf_urls(doi))
        candidate_urls.extend(("europe_pmc", u) for u in europe_pmc_urls)
        candidate_urls.extend(("hal", u) for u in hal_pdf_urls(doi))
        candidate_urls.extend(("semantic_scholar", u) for u in semantic_scholar_pdf_urls(doi))
        candidate_urls.extend(("crossref", u) for u in crossref_pdf_urls(doi))
    if "publisher" in methods and doi:
        candidate_urls.extend(("publisher", u) for u in publisher_pdf_candidates(doi))
        landing = f"https://doi.org/{doi}"
        for link in extract_pdf_links(landing):
            candidate_urls.append(("publisher", link))
    if row["landing_url"] and "publisher" in methods:
        for link in extract_pdf_links(row["landing_url"]):
            candidate_urls.append(("publisher", link))

    seen = set()
    for method, url in candidate_urls:
        if not url or url in seen:
            continue
        if is_supplementary_pdf_url(url):
            continue
        seen.add(url)
        if download_pdf_url(url, out_path):
            ok, pages, _ = verify_pdf(out_path)
            if ok:
                return str(out_path), method, "ok", pages
            out_path.unlink(missing_ok=True)

    proxy_url = os.environ.get("PROXY_URL")
    proxy_type = os.environ.get("PROXY_TYPE", "prefix")
    if "ezproxy" in methods and proxy_url and doi:
        proxied = apply_proxy(f"https://doi.org/{doi}", proxy_url, proxy_type)
        for link in [proxied] + [apply_proxy(u, proxy_url, proxy_type) for _, u in candidate_urls]:
            if is_supplementary_pdf_url(link):
                continue
            if download_pdf_url(link, out_path):
                ok, pages, _ = verify_pdf(out_path)
                if ok:
                    return str(out_path), "ezproxy", "ok", pages
                out_path.unlink(missing_ok=True)

    if "webvpn" in methods and doi:
        targets = [f"https://doi.org/{doi}"] + [u for _, u in candidate_urls]
        for target in targets:
            if is_supplementary_pdf_url(target):
                continue
            if webvpn_fetch_pdf(target, out_path):
                ok, pages, _ = verify_pdf(out_path)
                if ok:
                    return str(out_path), "webvpn", "ok", pages
                out_path.unlink(missing_ok=True)

    return None, None, "missing", 0


def fetch_pdf_worker(row: Dict[str, Any], pdf_dir: str, methods: Sequence[str]) -> Tuple[str, Optional[str], Optional[str], str, int]:
    path, method, status, pages = fetch_one_pdf(row, Path(pdf_dir), methods)
    return str(row.get("id") or ""), path, method, status, pages


def resolve_fetch_scope(profile: Dict[str, Any], cli_scope: Optional[str]) -> str:
    if cli_scope:
        return cli_scope
    policy = profile_str(profile, "pdf_fetch_policy", "after_screening").lower()
    aliases = {
        "all": "all",
        "all_candidates": "all",
        "before_screening": "all",
        "screened": "screened",
        "after_screening": "screened",
        "after_metadata_screen": "screened",
        "after_abstract_screen": "screened",
        "pending_pdf": "pending_pdf",
        "included": "included",
    }
    return aliases.get(policy, "screened")


def fetch_scope_statuses(scope: str) -> Tuple[str, ...]:
    if scope == "all":
        return ("candidate", "pending_pdf", "pending_fulltext", "peripheral")
    if scope == "included":
        return ("included",)
    if scope == "pending_pdf":
        return ("pending_pdf",)
    return ("pending_pdf", "pending_fulltext")


def command_fetch(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    pdf_dir = pdir / "pdfs"
    methods_text = getattr(args, "methods", None) or profile_str(profile, "pdf_methods", "unpaywall,publisher,webvpn,ezproxy")
    methods = [m.strip().lower() for m in methods_text.split(",") if m.strip()]
    scope = resolve_fetch_scope(profile, getattr(args, "scope", None))
    statuses = fetch_scope_statuses(scope)
    conn = connect(pdir / "library.sqlite")
    try:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT * FROM papers
            WHERE status IN ({placeholders})
              AND (pdf_path IS NULL OR pdf_path='' OR pdf_status!='ok')
            ORDER BY relevance_score DESC, citation_count DESC, year DESC
            """,
            statuses,
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                1 if priority_journal_match(row["journal"], profile) else 0,
                1 if row["status"] == "pending_pdf" else 0,
                float(row["relevance_score"] or 0),
                int(row["citation_count"] or 0),
                int(row["year"] or 0),
            ),
            reverse=True,
        )[: args.limit]
        ok_count = 0
        missing = 0
        concurrency = max(1, int(getattr(args, "concurrency", DEFAULT_PDF_FETCH_CONCURRENCY) or DEFAULT_PDF_FETCH_CONCURRENCY))
        log(f"fetch scope={scope}; statuses={','.join(statuses)}; targets={len(rows[: args.limit])}")
        if concurrency > 1 and len(rows) > 1:
            row_by_id = {str(row["id"]): row for row in rows}
            worker_rows = [dict(row) for row in rows]
            log(f"parallel fetch enabled: processes={min(concurrency, len(worker_rows))}")
            with ProcessPoolExecutor(max_workers=min(concurrency, len(worker_rows))) as pool:
                futures = [pool.submit(fetch_pdf_worker, row, str(pdf_dir), methods) for row in worker_rows]
                for idx, future in enumerate(as_completed(futures), 1):
                    row_id, path, method, status, pages = future.result()
                    row = row_by_id.get(row_id)
                    if row is None:
                        continue
                    log(f"fetch done {idx}/{len(rows)}: {(row['title'] or '')[:80]} [{status}]")
                    if path:
                        ok_count += 1
                        conn.execute(
                            "UPDATE papers SET pdf_path=?, pdf_method=?, pdf_status=?, pdf_pages=?, status=CASE WHEN status IN ('candidate','pending_pdf') THEN 'pending_fulltext' ELSE status END, updated_at=? WHERE id=?",
                            (path, method, status, pages, utc_now(), row["id"]),
                        )
                    else:
                        missing += 1
                        new_status = "pending_pdf" if args.require_pdf and row["status"] not in ("peripheral", "excluded", "included") else row["status"]
                        conn.execute(
                            "UPDATE papers SET pdf_status=?, status=?, updated_at=? WHERE id=?",
                            ("missing", new_status, utc_now(), row["id"]),
                        )
                    conn.commit()
        else:
            for idx, row in enumerate(rows, 1):
                log(f"fetch {idx}/{len(rows)}: {(row['title'] or '')[:80]}")
                path, method, status, pages = fetch_one_pdf(row, pdf_dir, methods)
                if path:
                    ok_count += 1
                    conn.execute(
                        "UPDATE papers SET pdf_path=?, pdf_method=?, pdf_status=?, pdf_pages=?, status=CASE WHEN status IN ('candidate','pending_pdf') THEN 'pending_fulltext' ELSE status END, updated_at=? WHERE id=?",
                        (path, method, status, pages, utc_now(), row["id"]),
                    )
                else:
                    missing += 1
                    new_status = "pending_pdf" if args.require_pdf and row["status"] not in ("peripheral", "excluded", "included") else row["status"]
                    conn.execute(
                        "UPDATE papers SET pdf_status=?, status=?, updated_at=? WHERE id=?",
                        ("missing", new_status, utc_now(), row["id"]),
                    )
                conn.commit()
        write_pdf_manifest(conn, pdir, profile)
        write_missing_pdf(conn, pdir, profile)
        record_run(conn, "fetch", {"ok": ok_count, "missing": missing, "methods": methods, "scope": scope, "statuses": statuses, "concurrency": concurrency})
    finally:
        conn.close()
    log("fetch complete")


def parse_pdf_text(path: Path) -> Tuple[str, int]:
    try:
        import fitz  # type: ignore
    except Exception as e:
        raise RuntimeError("PyMuPDF/fitz is required for PDF text extraction") from e
    doc = fitz.open(str(path))
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    pages = len(doc)
    doc.close()
    text = "\n\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"-\n(\w)", r"\1", text)
    return text.strip(), pages


DOI_IN_TEXT_PATTERN = r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+"


def extract_doi_from_text(text: str) -> str:
    for match in re.findall(DOI_IN_TEXT_PATTERN, text or "", flags=re.I):
        doi = normalize_doi(match).rstrip(".,;:)]}>")
        if doi:
            return doi
    return ""


def title_match_score(title: str, text: str) -> float:
    title_norm = clean_html_text(title).lower()
    text_norm = clean_html_text(text).lower()
    words = [w for w in re.findall(r"[a-z0-9]{4,}", title_norm) if w not in {"with", "from", "using", "based", "study", "into"}]
    if not words:
        return 0.0
    return sum(1 for w in words if w in text_norm) / max(1, len(words))


def match_pdf_to_existing_paper(conn: sqlite3.Connection, preview: str, parsed_text: str = "") -> Optional[sqlite3.Row]:
    text = f"{preview}\n{parsed_text[:12000]}"
    doi = extract_doi_from_text(text)
    if doi:
        row = conn.execute("SELECT * FROM papers WHERE doi=?", (doi,)).fetchone()
        if row:
            return row
        # If the PDF itself exposes a DOI and that DOI is not in this library,
        # do not title-match it onto another record. This prevents correction
        # notices, references, or misnamed local files from contaminating an
        # existing paper's full-text evidence.
        return None
    candidates = conn.execute(
        """
        SELECT * FROM papers
        WHERE status IN ('pending_pdf','pending_fulltext','included','peripheral')
        ORDER BY status='pending_pdf' DESC, relevance_score DESC, citation_count DESC
        LIMIT 500
        """
    ).fetchall()
    scored: List[Tuple[float, sqlite3.Row]] = []
    for row in candidates:
        score = title_match_score(row["title"] or "", text)
        if score >= 0.85:
            scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], float(item[1]["relevance_score"] or 0)), reverse=True)
    return scored[0][1]


def import_local_pdfs(conn: sqlite3.Connection, pdir: Path, pdf_dir: Path, add_unmatched: bool = False) -> None:
    """Add PDFs from an external directory and match them to existing records."""
    if not pdf_dir.exists():
        raise FileNotFoundError(f"pdf directory not found: {pdf_dir}")
    target_dir = pdir / "pdfs"
    unmatched: List[Dict[str, Any]] = []
    imported = 0
    for pdf in pdf_dir.rglob("*.pdf"):
        ok, pages, preview = verify_pdf(pdf)
        if not ok:
            unmatched.append({"file": str(pdf), "reason": "invalid_pdf", "doi": "", "title_guess": ""})
            continue
        parsed_text = ""
        try:
            parsed_text, pages = parse_pdf_text(pdf)
        except Exception:
            parsed_text = ""
        matched = match_pdf_to_existing_paper(conn, preview, parsed_text)
        if matched:
            dst = target_dir / (sanitize_filename(matched["id"]) + ".pdf")
            if pdf.resolve() != dst.resolve():
                shutil.copyfile(pdf, dst)
            new_status = "pending_fulltext" if matched["status"] in ("candidate", "pending_pdf") else matched["status"]
            conn.execute(
                """
                UPDATE papers
                SET pdf_path=?, pdf_method='local_import', pdf_status='ok', pdf_pages=?,
                    full_text=COALESCE(NULLIF(?, ''), full_text), status=?, updated_at=?
                WHERE id=?
                """,
                (str(dst), pages, parsed_text, new_status, utc_now(), matched["id"]),
            )
            imported += 1
            continue
        if not add_unmatched:
            unmatched.append({"file": str(pdf), "reason": "no_existing_record_match", "doi": extract_doi_from_text(f"{preview}\n{parsed_text[:12000]}"), "title_guess": pdf.stem[:300]})
            continue
        dst = target_dir / pdf.name
        if not dst.exists():
            shutil.copyfile(pdf, dst)
        title = ""
        for line in preview.splitlines():
            line = line.strip()
            if len(line) > 20:
                title = line[:300]
                break
        title = title or pdf.stem.replace("_", " ")
        doi_match = re.search(r"(10\.\d{4,9}/[^\s,;()]+)", preview, flags=re.I)
        doi = normalize_doi(doi_match.group(1)) if doi_match else extract_doi_from_text(preview)
        pid = paper_id(doi, title)
        existing = conn.execute("SELECT id FROM papers WHERE id=? OR (doi IS NOT NULL AND doi=?)", (pid, doi or None)).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO papers(id, doi, title, sources, source_ids, search_queries, status,
                    pdf_path, pdf_method, pdf_status, pdf_pages, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid,
                    doi or None,
                    title,
                    json.dumps(["local_pdf"]),
                    json.dumps({"local_pdf": pdf.name}),
                    json.dumps(["local import"]),
                    "pending_fulltext",
                    str(dst),
                    "local",
                    "ok",
                    pages,
                    utc_now(),
                    utc_now(),
                ),
            )
        else:
            conn.execute(
                "UPDATE papers SET pdf_path=?, pdf_method='local', pdf_status='ok', pdf_pages=?, status=CASE WHEN status='candidate' THEN 'pending_fulltext' ELSE status END, updated_at=? WHERE id=?",
                (str(dst), pages, utc_now(), existing["id"]),
            )
        imported += 1
    if unmatched:
        write_csv(pdir / "exports" / "unmatched_imported_pdfs.csv", unmatched, ["file", "reason", "doi", "title_guess"])
    conn.commit()
    log(f"local PDF import complete: imported={imported}, unmatched={len(unmatched)}")


def command_ingest(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    try:
        if args.pdf_dir:
            import_local_pdfs(conn, pdir, Path(args.pdf_dir), getattr(args, "add_unmatched", False))
        rows = conn.execute("SELECT * FROM papers WHERE pdf_path IS NOT NULL AND pdf_path!='' LIMIT ?", (args.limit,)).fetchall()
        parsed = 0
        for idx, row in enumerate(rows, 1):
            path = Path(row["pdf_path"])
            if args.verify_pdf:
                ok, pages, _ = verify_pdf(path)
                if not ok:
                    conn.execute("UPDATE papers SET pdf_status='invalid', updated_at=? WHERE id=?", (utc_now(), row["id"]))
                    conn.commit()
                    continue
            if row["full_text"] and not args.force:
                continue
            log(f"ingest {idx}/{len(rows)}: {path.name}")
            try:
                text, pages = parse_pdf_text(path)
                status = "pending_fulltext" if row["status"] in ("candidate", "pending_pdf") else row["status"]
                conn.execute(
                    "UPDATE papers SET full_text=?, pdf_pages=?, pdf_status='ok', status=?, updated_at=? WHERE id=?",
                    (text, pages, status, utc_now(), row["id"]),
                )
                parsed += 1
                conn.commit()
            except Exception as e:
                log(f"  parse failed: {e}")
        record_run(conn, "ingest", {"parsed": parsed})
    finally:
        conn.close()
    log(f"ingest complete; parsed: {parsed}")


def sentences(text: str, limit: int = 80) -> List[str]:
    text = re.sub(r"\s+", " ", text or " ").strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 500][:limit]


def count_patterns(text: str, patterns: Sequence[str]) -> int:
    return sum(len(re.findall(p, text, flags=re.I)) for p in patterns)


def best_evidence(text: str, patterns: Sequence[str]) -> str:
    for sent in sentences(text, limit=160):
        if any(re.search(p, sent, flags=re.I) for p in patterns):
            return sent
    return ""


SPINEL_STRONG_PATTERNS = [
    r"\bspinel(?:[- ](?:type|structured|structure|phase|oxide|oxides|ferrite|ferrites))?\b",
    r"\bspinel[- ]structured oxides?\b",
    r"\bspinel oxides?\b",
    r"\bspinel ferrites?\b",
    r"\bhigh[- ]entropy spinel\b",
    r"\bspinel high[- ]entropy oxides?\b",
    r"\bCo\s*3\s*O\s*4\b",
    r"\bMn\s*3\s*O\s*4\b",
    r"\bFe\s*3\s*O\s*4\b",
    r"\bNi\s*Co\s*2\s*O\s*4\b",
    r"\bMn\s*Co\s*2\s*O\s*4\b",
    r"\bZn\s*Co\s*2\s*O\s*4\b",
    r"\bCu\s*Co\s*2\s*O\s*4\b",
    r"\bMg\s*Co\s*2\s*O\s*4\b",
    r"\bNi\s*Fe\s*2\s*O\s*4\b",
    r"\bCo\s*Fe\s*2\s*O\s*4\b",
    r"\bZn\s*Fe\s*2\s*O\s*4\b",
    r"\bCu\s*Fe\s*2\s*O\s*4\b",
    r"\bMg\s*Fe\s*2\s*O\s*4\b",
    r"\bMn\s*Fe\s*2\s*O\s*4\b",
    r"\bLi\s*Mn\s*2\s*O\s*4\b",
    r"\bLi\s*Ni\s*0?\.\d+\s*Mn\s*1?\.\d+\s*O\s*4\b",
    r"\b(?:cobalt|nickel|manganese|zinc|copper|magnesium|iron) ferrites?\b",
    r"\b(?:nickel|manganese|zinc|copper|magnesium) cobaltites?\b",
]

SPINEL_SUPPORT_PATTERNS = [
    r"\bcation inversion\b",
    r"\binverse spinel\b",
    r"\bnormal spinel\b",
    r"\bAB\s*2\s*O\s*4\b",
    r"\bA\s*B\s*2\s*O\s*4\b",
    r"\bferrite oxides?\b",
    r"\bcobaltite oxides?\b",
]

SPINEL_OXIDE_CONTEXT_PATTERNS = [
    r"\boxides?\b",
    r"\bmetal oxides?\b",
    r"\btransition[- ]metal oxides?\b",
    r"\bferrites?\b",
    r"\bcobaltites?\b",
    r"\bmanganites?\b",
    r"\boxygen vacancies?\b",
]

SPINEL_CATALYSIS_PATTERNS = [
    r"\b\w*catalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\bcatalytic\b",
    r"\belectrocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\bthermocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\boxidation reaction\b",
    r"\breduction reaction\b",
    r"\boxygen evolution reaction\b",
    r"\boxygen reduction reaction\b",
    r"\bwater splitting\b",
    r"\bmetal[- ]air batter(?:y|ies)\b",
    r"\bFenton(?:-like)?\b",
    r"\bpersulfate activation\b",
    r"\bperoxymonosulfate activation\b",
    r"\bozonation\b",
    r"\bselective catalytic reduction\b",
    r"\bSCR\b",
]

SPINEL_PHOTO_EXCLUSION_PATTERNS = [
    r"\bphotocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\bphoto[- ]?catalytic\b",
    r"\bphoto[- ]?Fenton\b",
    r"\bphotoelectrocatalys",
    r"\bvisible[- ]light\b",
    r"\blight irradiation\b",
    r"\bsolar[- ]driven\b",
]

SPINEL_SYNTHESIS_EXCLUSION_PATTERNS = [
    r"\borganic synthesis\b",
    r"\bchemical synthesis\b",
    r"\bfine chemical synthesis\b",
    r"\bC[- ]H activation\b",
    r"\bcross[- ]coupling\b",
    r"\bSuzuki\b",
    r"\bHeck\b",
    r"\bSonogashira\b",
    r"\bFischer[- ]Tropsch synthesis\b",
    r"\bammonia synthesis\b",
    r"\burea synthesis\b",
    r"\bmethanol synthesis\b",
    r"\bselective hydrogenation\b",
    r"\bbenzyl alcohol oxidation\b",
    r"\bsyngas(?:[- ]to[- ]|\s+conversion|\s+to\s+|\s+conversion)\b",
    r"\bsyngas[- ]to[- ](?:light )?olefins?\b",
    r"\bsyngas[- ]to[- ]aromatics?\b",
    r"\bCO\s*/\s*H\s*2 activation\b",
    r"\bCO hydrogenation\b",
    r"\bCO2 hydrogenation\b",
    r"\bCO\s*2 hydrogenation\b",
    r"\bFischer[-– ]Tropsch\b",
    r"\bpropane dehydrogenation\b",
    r"\btransfer hydrogenation\b",
    r"\bhydrogenation of quinoxaline\b",
    r"\bbiomass (?:derivatives?|platform|conversion|hydrogenation)\b",
    r"\blignin\b",
    r"\bfurfural\b",
    r"\b5[- ]hydroxymethylfurfural\b",
    r"\bhydroxymethylfurfural\b",
    r"\bHMF\b",
    r"\baromatic aldehydes?\b",
    r"\baromatic ketones?\b",
    r"\balcohol oxidation to aldehyde\b",
    r"\belectrocatalytic alcohol oxidation\b",
    r"\bethanol to .*acetaldehyde\b",
    r"\bpropylene epoxidation\b",
    r"\bepoxidation\b",
]

SPINEL_NON_OXIDE_EXCLUSION_PATTERNS = [
    r"\bspinel sulfides?\b",
    r"\bspinel selenides?\b",
    r"\bspinel nitrides?\b",
    r"\bspinel carbides?\b",
    r"\bthiospinel\b",
]

SPINEL_REVIEW_PATTERNS = [
    r"\breview\b",
    r"\bmini[- ]review\b",
    r"\bperspective\b",
    r"\bviewpoint\b",
    r"\bcomment(?:ary)?\b",
    r"\beditorial\b",
    r"\bcorrection\b",
    r"\berratum\b",
    r"\bnews\b",
]

SPINEL_DIRECTION_RULES = [
    (
        "non_photo_aop_fenton_persulfate",
        [
            r"\badvanced oxidation\b",
            r"\bAOPs?\b",
            r"\bFenton(?:-like)?\b",
            r"\bperoxymonosulfate\b",
            r"\bperoxydisulfate\b",
            r"\bpersulfate\b",
            r"\bPMS\b",
            r"\bPDS\b",
            r"\bH2O2\b",
            r"\bhydrogen peroxide\b",
            r"\bradical\b",
            r"\bsinglet oxygen\b",
            r"\bpollutant degradation\b",
        ],
    ),
    (
        "catalytic_ozonation",
        [r"\bozonation\b", r"\bozone\b", r"\bperoxone\b"],
    ),
    (
        "air_pollution_control",
        [
            r"\bVOC\b",
            r"\bvolatile organic compounds?\b",
            r"\bNOx\b",
            r"\bselective catalytic reduction\b",
            r"\bSCR\b",
            r"\bCO oxidation\b",
            r"\bsoot\b",
            r"\bmercury oxidation\b",
            r"\bflue gas\b",
        ],
    ),
    (
        "energy_electrocatalysis",
        [
            r"\belectrocatalys",
            r"\boxygen evolution reaction\b",
            r"\boxygen reduction reaction\b",
            r"\bOER\b",
            r"\bORR\b",
            r"\bHER\b",
            r"\bwater splitting\b",
            r"\bmetal[- ]air batter",
            r"\bzinc[- ]air batter",
            r"\blithium[- ]air batter",
        ],
    ),
    (
        "thermal_redox_catalysis",
        [
            r"\bthermocatalys",
            r"\bthermal catalys",
            r"\bcatalytic oxidation\b",
            r"\bcatalytic reduction\b",
            r"\bmethane combustion\b",
            r"\bpropane oxidation\b",
            r"\btoluene oxidation\b",
            r"\bformaldehyde oxidation\b",
        ],
    ),
    (
        "composition_defect_cation_design",
        [
            r"\bcation inversion\b",
            r"\bcation distribution\b",
            r"\boxygen vacancies?\b",
            r"\bdefect",
            r"\bdop(?:ed|ing)\b",
            r"\bhigh[- ]entropy\b",
            r"\bcomposition(?:al)? design\b",
        ],
    ),
    (
        "operando_dft_mechanism_reconstruction",
        [
            r"\boperando\b",
            r"\bin situ\b",
            r"\bDFT\b",
            r"\bdensity functional theory\b",
            r"\bmechanism\b",
            r"\breconstruction\b",
            r"\bsurface reconstruction\b",
            r"\bactive sites?\b",
        ],
    ),
    (
        "stability_deactivation_real_conditions",
        [
            r"\bstability\b",
            r"\bdurability\b",
            r"\bdeactivation\b",
            r"\bleaching\b",
            r"\bpoisoning\b",
            r"\breal wastewater\b",
            r"\breal water\b",
            r"\bindustrial conditions?\b",
            r"\blong[- ]term\b",
        ],
    ),
]


def classify_spinel_direction(text: str) -> str:
    scores = {label: count_patterns(text, patterns) for label, patterns in SPINEL_DIRECTION_RULES}
    for label in [
        "non_photo_aop_fenton_persulfate",
        "catalytic_ozonation",
        "air_pollution_control",
        "energy_electrocatalysis",
        "thermal_redox_catalysis",
        "stability_deactivation_real_conditions",
        "operando_dft_mechanism_reconstruction",
        "composition_defect_cation_design",
    ]:
        if scores.get(label, 0) > 0:
            return label
    return "other_spinel_catalysis"


def screen_spinel_row(row: sqlite3.Row, stage: str, require_pdf: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:140000]}"
    early_text = f"{title_abs}\n\n{full[:6000]}"
    title = row["title"] or ""

    spinel_strong = count_patterns(text, SPINEL_STRONG_PATTERNS)
    spinel_support = count_patterns(text, SPINEL_SUPPORT_PATTERNS)
    oxide_context = count_patterns(text, SPINEL_OXIDE_CONTEXT_PATTERNS)
    catalysis_count = count_patterns(text, SPINEL_CATALYSIS_PATTERNS)
    catalysis_early = count_patterns(early_text, SPINEL_CATALYSIS_PATTERNS)
    method_count = count_patterns(text, METHOD_PATTERNS)
    direction = classify_spinel_direction(title_abs)
    if direction == "other_spinel_catalysis" and stage == "fulltext":
        direction = classify_spinel_direction(early_text)
    spinel_evidence = best_evidence(text, SPINEL_STRONG_PATTERNS + SPINEL_SUPPORT_PATTERNS)
    catalysis_evidence = best_evidence(text, SPINEL_CATALYSIS_PATTERNS)
    direction_evidence = best_evidence(text, [p for _, patterns in SPINEL_DIRECTION_RULES for p in patterns])
    evidence = spinel_evidence or catalysis_evidence or direction_evidence
    method_primary = 1 if method_count else 0
    score = float(spinel_strong * 8 + spinel_support * 4 + catalysis_count * 5 + method_count + min(int(row["citation_count"] or 0), 200) / 20)

    has_spinel_oxide = spinel_strong > 0 and (oxide_context > 0 or not count_patterns(text, SPINEL_NON_OXIDE_EXCLUSION_PATTERNS))
    if not has_spinel_oxide and not (spinel_support > 0 and oxide_context > 0):
        return "excluded", direction, method_primary, evidence or "No clear spinel oxide catalyst evidence found.", score
    if count_patterns(text, SPINEL_NON_OXIDE_EXCLUSION_PATTERNS) and oxide_context == 0:
        return "excluded", direction, method_primary, evidence or "Non-oxide spinel material is outside the configured scope.", score
    if count_patterns(title_abs, SPINEL_PHOTO_EXCLUSION_PATTERNS):
        return "excluded", direction, method_primary, evidence or "Photocatalysis/photo-assisted catalysis is excluded by profile.", score
    if stage == "fulltext" and count_patterns(early_text, SPINEL_PHOTO_EXCLUSION_PATTERNS):
        return "excluded", direction, method_primary, evidence or "Photocatalysis/photo-assisted catalysis is excluded by profile.", score
    if count_patterns(title_abs, SPINEL_SYNTHESIS_EXCLUSION_PATTERNS):
        return "excluded", direction, method_primary, evidence or "Chemical/organic synthesis catalysis is excluded by profile.", score
    if catalysis_count == 0 or (stage == "fulltext" and catalysis_early == 0):
        return "excluded", direction, method_primary, evidence or "No catalytic application or catalytic mechanism evidence found.", score
    if any(re.search(p, title, flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if count_patterns(title_abs, SPINEL_REVIEW_PATTERNS) or (stage == "fulltext" and count_patterns(early_text[:5000], SPINEL_REVIEW_PATTERNS)):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/commentary; separated from original research.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence or catalysis_evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence or catalysis_evidence, score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence or catalysis_evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence or catalysis_evidence, score


HTS_CORE_PATTERNS = [
    r"\bhigh[- ]throughput\b",
    r"\bhigh[- ]throughput screening\b",
    r"\bhigh[- ]throughput experiment(?:ation|s)?\b",
    r"\bhigh[- ]throughput computation(?:al)?\b",
    r"\bcomputational screening\b",
    r"\btheoretical screening\b",
    r"\bvirtual screening\b",
    r"\blarge[- ]scale screening\b",
    r"\bscreen(?:ed|ing) (?:of|for) (?:catalysts?|materials?|electrocatalysts?|photocatalysts?|adsorbents?|MOFs?|COFs?|zeolites?|polymers?|alloys?|oxides?|perovskites?|reactions?|ligands?|solvents?|electrolytes?)\b",
    r"\bactive learning\b",
    r"\bBayesian optimization\b",
    r"\bBayesian optimis(?:ation|e)\b",
    r"\bBO[- ](?:guided|driven|based)\b",
    r"\bclosed[- ]loop (?:optimization|optimisation|experimentation|discovery|screening|platform|workflow|Bayesian)\b",
    r"\bself[- ]driving (?:lab|laboratory|experiments?|platform)\b",
    r"\bautonomous (?:discovery|experimentation|laboratory|lab|platform|optimization|optimisation)\b",
    r"\bautonomous (?:chemistry|chemical research|scientific research|materials research)\b",
    r"\bAI chemist\b",
    r"\brobotic AI chemist\b",
    r"\brobotic.*chemist\b",
    r"\bmultiagent[- ]driven robotic\b",
    r"\brobotic(?:ally)? (?:synthesis|experimentation|screening|platform|workflow)\b",
    r"\bautomated (?:synthesis|experimentation|screening|reaction optimization|materials discovery|catalyst discovery)\b",
    r"\bcombinatorial (?:chemistry|synthesis|library|screening|optimization|materials?)\b",
    r"\bmegalibrar(?:y|ies)\b",
    r"\binverse design\b",
    r"\bgenerative (?:model|AI|machine learning).{0,80}(?:design|discovery|screening|materials?|catalysts?)\b",
    r"\baccelerated (?:materials?|catalyst|reaction|chemical) discovery\b",
]

HTS_DOMAIN_PATTERNS = [
    r"\b\w*catalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\bcatalytic\b",
    r"\belectrocatalys",
    r"\bphotocatalys",
    r"\bthermocatalys",
    r"\bheterogeneous catalysis\b",
    r"\bhomogeneous catalysis\b",
    r"\borganocatalysis\b",
    r"\breaction (?:discovery|optimization|optimisation|screening|condition|conditions|selectivity|yield)\b",
    r"\bchemical synthesis\b",
    r"\borganic synthesis\b",
    r"\bC[- ]H activation\b",
    r"\bcross[- ]coupling\b",
    r"\bligand\b",
    r"\bsolvent\b",
    r"\bmaterials?\b",
    r"\bmaterials discovery\b",
    r"\bmaterials design\b",
    r"\binorganic\b",
    r"\bcrystal(?:line)?\b",
    r"\bsolid[- ]state\b",
    r"\bpolymer\b",
    r"\belectrolyte\b",
    r"\bbatter(?:y|ies)\b",
    r"\benergy storage\b",
    r"\benergy conversion\b",
    r"\bmetal[- ]organic framework\b",
    r"\bMOF\b",
    r"\bcovalent organic framework\b",
    r"\bCOF\b",
    r"\bporous\b",
    r"\badsorption\b",
    r"\bseparation\b",
    r"\bzeolite\b",
    r"\bperovskite\b",
    r"\boxide\b",
    r"\balloy\b",
    r"\bhigh[- ]entropy\b",
    r"\bsemiconductor\b",
    r"\bnanomaterial\b",
    r"\bsingle[- ]atom\b",
    r"\bOER\b",
    r"\bORR\b",
    r"\bHER\b",
    r"\bCO\s*2RR\b",
    r"\boxygen evolution\b",
    r"\boxygen reduction\b",
    r"\bhydrogen evolution\b",
    r"\bwater splitting\b",
]

HTS_BIO_EXCLUSION_PATTERNS = [
    r"\bdrug discovery\b",
    r"\bdrug screening\b",
    r"\bgenome[- ]wide\b",
    r"\bCRISPR\b",
    r"\b(?:living|cancer|bacterial|microbial|human|mammalian|cellular) cells?\b",
    r"\bcancer\b",
    r"\bclinical\b",
    r"\bpatient\b",
    r"\btherapy\b",
    r"\bvaccine\b",
    r"\bbiomarker\b",
    r"\bmetabolite\b",
    r"\bprotein language model\b",
    r"\benzyme variant\b",
    r"\btoxicity prediction\b",
    r"\btoxicology\b",
    r"\breceptor\b",
    r"\baptamer\b",
]

HTS_REVIEW_PATTERNS = [
    r"\breview\b",
    r"\bmini[- ]review\b",
    r"\bperspective\b",
    r"\bviewpoint\b",
    r"\bcomment(?:ary)?\b",
    r"\beditorial\b",
    r"\bcorrection\b",
    r"\berratum\b",
    r"\bnews\b",
    r"\bpreview\b",
    r"\bresearch briefing\b",
    r"\boutlook\b",
]

HTS_DIRECTION_RULES = [
    (
        "autonomous_closed_loop_platforms",
        [
            r"\bclosed[- ]loop (?:optimization|optimisation|experimentation|discovery|screening|platform|workflow|Bayesian)\b",
            r"\bself[- ]driving\b",
            r"\bautonomous\b",
            r"\brobotic",
            r"\bautomated (?:synthesis|experimentation|screening|reaction optimization|materials discovery)\b",
            r"\bBayesian optimization\b",
            r"\bactive learning\b",
        ],
    ),
    (
        "computational_catalyst_screening",
        [
            r"\b\w*catalys",
            r"\belectrocatalys",
            r"\bphotocatalys",
            r"\bDFT\b",
            r"\bdensity functional theory\b",
            r"\bfirst[- ]principles\b",
            r"\bcomputational screening\b",
            r"\bhigh[- ]throughput computation",
        ],
    ),
    (
        "experimental_combinatorial_catalysis",
        [
            r"\b\w*catalys",
            r"\bhigh[- ]throughput experiment",
            r"\bcombinatorial",
            r"\bmegalibrar",
            r"\bautomated synthesis\b",
            r"\breaction optimization\b",
        ],
    ),
    (
        "energy_electro_photo_screening",
        [
            r"\belectrocatalys",
            r"\bphotocatalys",
            r"\bOER\b",
            r"\bORR\b",
            r"\bHER\b",
            r"\bCO\s*2RR\b",
            r"\bNRR\b",
            r"\boxygen evolution\b",
            r"\boxygen reduction\b",
            r"\bhydrogen evolution\b",
            r"\bwater splitting\b",
            r"\bbatter",
            r"\belectrolyte\b",
        ],
    ),
    (
        "porous_framework_adsorption_screening",
        [
            r"\bmetal[- ]organic framework\b",
            r"\bMOF\b",
            r"\bcovalent organic framework\b",
            r"\bCOF\b",
            r"\bporous\b",
            r"\bzeolite\b",
            r"\badsorption\b",
            r"\bseparation\b",
            r"\bgas storage\b",
        ],
    ),
    (
        "reaction_condition_synthesis_screening",
        [
            r"\breaction (?:optimization|optimisation|condition|conditions|screening|discovery)\b",
            r"\borganic synthesis\b",
            r"\bchemical synthesis\b",
            r"\bC[- ]H activation\b",
            r"\bcross[- ]coupling\b",
            r"\bligand\b",
            r"\bsolvent\b",
        ],
    ),
    (
        "materials_discovery_screening",
        [
            r"\bmaterials? discovery\b",
            r"\bmaterials? design\b",
            r"\binorganic\b",
            r"\bcrystal",
            r"\bpolymer\b",
            r"\balloy\b",
            r"\bhigh[- ]entropy\b",
            r"\bperovskite\b",
            r"\boxide\b",
            r"\bsemiconductor\b",
        ],
    ),
    (
        "ml_inverse_design_methods",
        [
            r"\binverse design\b",
            r"\bgenerative\b",
            r"\bgraph neural\b",
            r"\bdeep learning\b",
            r"\bfoundation model\b",
            r"\blarge language model\b",
            r"\bdescriptor",
        ],
    ),
]


def classify_hts_direction(text: str) -> str:
    energy = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "energy_electro_photo_screening" for p in patterns])
    porous = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "porous_framework_adsorption_screening" for p in patterns])
    reaction = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "reaction_condition_synthesis_screening" for p in patterns])
    materials = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "materials_discovery_screening" for p in patterns])
    catalyst = count_patterns(text, [r"\b\w*catalys", r"\belectrocatalys", r"\bphotocatalys"])
    experimental = count_patterns(text, [r"\bhigh[- ]throughput experiment", r"\bcombinatorial", r"\bmegalibrar", r"\bautomated synthesis\b", r"\breaction optimization\b"])
    autonomous = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "autonomous_closed_loop_platforms" for p in patterns])
    method = count_patterns(text, [p for label, patterns in HTS_DIRECTION_RULES if label == "ml_inverse_design_methods" for p in patterns])

    if energy:
        return "energy_electro_photo_screening"
    if porous:
        return "porous_framework_adsorption_screening"
    if reaction and not catalyst:
        return "reaction_condition_synthesis_screening"
    if catalyst:
        if experimental and not re.search(r"\bDFT\b|density functional theory|first[- ]principles|computational|virtual", text, flags=re.I):
            return "experimental_combinatorial_catalysis"
        return "computational_catalyst_screening"
    if autonomous:
        return "autonomous_closed_loop_platforms"
    if materials:
        return "materials_discovery_screening"
    if reaction:
        return "reaction_condition_synthesis_screening"
    if method:
        return "ml_inverse_design_methods"
    return "other_high_throughput_screening"


def screen_high_throughput_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:160000]}"
    early_text = f"{title_abs}\n\n{full[:8000]}"
    title = row["title"] or ""

    hts_count = count_patterns(text, HTS_CORE_PATTERNS)
    hts_title_abs = count_patterns(title_abs, HTS_CORE_PATTERNS)
    hts_early = count_patterns(early_text, HTS_CORE_PATTERNS)
    domain_count = count_patterns(text, HTS_DOMAIN_PATTERNS)
    domain_early = count_patterns(early_text, HTS_DOMAIN_PATTERNS)
    bio_noise = count_patterns(title_abs, HTS_BIO_EXCLUSION_PATTERNS)
    method_count = count_patterns(text, METHOD_PATTERNS) + hts_count
    evidence = best_evidence(text, HTS_CORE_PATTERNS) or best_evidence(text, HTS_DOMAIN_PATTERNS)
    direction = classify_hts_direction(title_abs)
    if direction == "other_high_throughput_screening" and stage == "fulltext":
        direction = classify_hts_direction(early_text)
    method_primary = 1 if (hts_title_abs > 0 or hts_early >= 2 or re.search(r"\b(active learning|Bayesian optimization|closed[- ]loop|self[- ]driving|autonomous|inverse design)\b", title_abs, flags=re.I)) else 0
    score = float(hts_count * 8 + domain_count * 3 + method_count * 2 + min(int(row["citation_count"] or 0), 250) / 25)

    if any(re.search(p, title, flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if count_patterns(title_abs, HTS_REVIEW_PATTERNS) or (stage == "fulltext" and count_patterns(early_text[:6000], HTS_REVIEW_PATTERNS)):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/commentary; separated from original research.", score
    if bio_noise and domain_count < 2 and not re.search(r"\b(material|catalys|chemistry|reaction|MOF|COF|polymer|battery|electrolyte)\b", title_abs, flags=re.I):
        return "excluded", direction, method_primary, evidence or "Biomedical, genomic, clinical, toxicology, or drug-screening record outside chemistry/materials/catalysis.", score
    if domain_count == 0 or (stage == "fulltext" and domain_early == 0):
        return "excluded", direction, method_primary, evidence or "No catalysis, chemistry, or materials-domain evidence found.", score
    if hts_count == 0 or (stage == "fulltext" and hts_early == 0):
        return "excluded", direction, method_primary, evidence or "No high-throughput screening, computational screening, active-learning, Bayesian, autonomous, combinatorial, or inverse-design evidence found.", score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "High-throughput screening appears peripheral rather than a core method.", score
    if stage == "fulltext" and int(row["pdf_pages"] or 0) == 1:
        return "excluded", direction, 0, evidence or "One-page/non-main-article PDF; excluded from final original research set.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence, score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence, score


PHENOL_SUBSTRATE_PATTERNS = [
    r"\bphenol(?:s|ic|ate)?\b",
    r"\bphenolic (?:compound|compounds|substrate|substrates|monomer|monomers)\b",
    r"\bhydroxybenzene\b",
    r"\bcresol(?:s)?\b",
    r"\bguaiacol\b",
    r"\bcatechol\b",
    r"\bsyringol\b",
    r"\bvanillin\b",
    r"\beugenol\b",
    r"\blignin[- ]derived phenol(?:s|ic|ics)?\b",
    r"\blignin[- ]derived aromatic(?:s)?\b",
    r"\bbiomass[- ]derived phenol(?:s|ic|ics)?\b",
    r"\baryl ether(?:s)?\b",
    r"\bdiaryl ether(?:s)?\b",
    r"\baryl C[- ]O bond(?:s)?\b",
]

PHENOL_ELECTRO_REDUCTION_PATTERNS = [
    r"\belectrocatalytic hydrogenation\b",
    r"\belectro[- ]?hydrogenation\b",
    r"\belectrochemical hydrogenation\b",
    r"\belectrocatalytic reduction\b",
    r"\belectrochemical reduction\b",
    r"\belectro[- ]?reduction\b",
    r"\bcathodic hydrogenation\b",
    r"\bcathodic reduction\b",
    r"\belectrochemical hydrodeoxygenation\b",
    r"\belectrocatalytic hydrodeoxygenation\b",
    r"\belectro[- ]?hydrodeoxygenation\b",
    r"\b(?:phenol|phenolic|guaiacol|cresol|catechol|syringol|vanillin).{0,80}(?:reduction|hydrogenation|hydrodeoxygenation|deoxygenation)\b",
    r"\b(?:reduction|hydrogenation|hydrodeoxygenation|deoxygenation).{0,80}(?:phenol|phenolic|guaiacol|cresol|catechol|syringol|vanillin)\b",
]

PHENOL_ELECTROCHEM_CONTEXT_PATTERNS = [
    r"\belectrocatalytic\b",
    r"\belectrocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\belectrochemical\b",
    r"\belectrolysis\b",
    r"\belectrode\b",
    r"\bcathode\b",
    r"\bcathodic\b",
    r"\bproton[- ]coupled electron transfer\b",
    r"\bPCET\b",
    r"\badsorbed hydrogen\b",
    r"\bH\*\b",
]

PHENOL_PRODUCT_PATH_PATTERNS = [
    r"\bcyclohexanol\b",
    r"\bcyclohexanone\b",
    r"\bcyclohexane\b",
    r"\bbenzene\b",
    r"\bdearomatization\b",
    r"\baromatic ring hydrogenation\b",
    r"\bC[- ]O bond cleavage\b",
    r"\baryl C[- ]O bond(?:s)?(?: [^.\n]{0,80})?(?:cleavage|hydrogenation|hydrogenolysis|hydrodeoxygenation)\b",
    r"\bhydrogenolysis\b",
    r"\bhydrodeoxygenation\b",
    r"\bdeoxygenation\b",
]

PHENOL_DOMAIN_PATTERNS = [
    r"\b\w*catalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\bcatalytic\b",
    r"\belectrocatalys",
    r"\bmaterials?\b",
    r"\bnanomaterial\b",
    r"\belectrode\b",
    r"\bmetal\b",
    r"\balloy\b",
    r"\bsingle[- ]atom\b",
    r"\bbiomass\b",
    r"\blignin\b",
    r"\bupgrading\b",
    r"\bvalorization\b",
    r"\bvalorisation\b",
]

PHENOL_REVIEW_PATTERNS = [
    r"^\s*review\b",
    r"^\s*mini[- ]review\b",
    r"^\s*perspective\b",
    r"^\s*viewpoint\b",
    r"^\s*comment(?:ary)?\b",
    r"^\s*editorial\b",
    r"^\s*correction\b",
    r"^\s*erratum\b",
    r"\bresearch briefing\b",
]

PHENOL_PHOTO_EXCLUSION_PATTERNS = [
    r"\bphotocatal",
    r"\bphotoelectrocatal",
    r"\bphoto[- ]?Fenton\b",
    r"\bvisible[- ]light\b",
    r"\bsolar[- ]driven\b",
    r"\blight[- ]driven\b",
]

PHENOL_OXIDATION_EXCLUSION_PATTERNS = [
    r"\bphenol(?:ic)? (?:oxidation|oxidative degradation|degradation|mineralization|mineralisation|removal)\b",
    r"\b(?:oxidation|oxidative degradation|degradation|mineralization|mineralisation|removal) of phenol(?:s|ic)?\b",
    r"\banodic oxidation\b",
    r"\belectrochemical oxidation\b",
    r"\belectrocatalytic oxidation\b",
    r"\badvanced oxidation\b",
    r"\bFenton[- ]like\b",
    r"\bperoxymonosulfate\b",
    r"\bpersulfate\b",
    r"\bozonation\b",
    r"\bpollutant degradation\b",
    r"\bwastewater\b",
]

PHENOL_NON_REACTION_EXCLUSION_PATTERNS = [
    r"\bsensor(?:s|ing)?\b",
    r"\bdetection\b",
    r"\banalytical\b",
    r"\badsorption\b",
    r"\badsorbent\b",
    r"\bextract(?:ion|ive)\b",
    r"\btoxicolog",
    r"\bbiological\b",
    r"\b(?:living|cancer|bacterial|microbial|human|mammalian|cellular) cells?\b",
    r"\bphenol as (?:a |the )?(?:proton|hydrogen) (?:donor|source)\b",
    r"\bphenol(?:s)? (?:was|were|is|are) used as (?:a |the )?(?:proton|hydrogen) (?:donor|source)\b",
    r"\bwith .*phenol as (?:a |the )?(?:proton|hydrogen) (?:donor|source)\b",
    r"\bphenol(?:s)? (?:degradation|remediation)\b",
    r"\bdegrades? .*phenol\b",
    r"\bCO\s*2 (?:electro)?reduction\b",
    r"\bcarbon dioxide (?:electro)?reduction\b",
    r"\bnitrogen (?:to ammonia|reduction|electroreduction)\b",
    r"\bN\s*2 (?:to ammonia|reduction|electroreduction)\b",
    r"\bN\s*2\s*O (?:reduction|electroreduction)\b",
    r"\bnitrous oxide (?:reduction|electroreduction)\b",
]

PHENOL_CENTRALITY_PATTERNS = [
    r"\bphenol(?:s|ic)? [^.\n]{0,80}(?:to|into|hydrogenation|reduction|hydrodeoxygenation|deoxygenation|conversion|upgrading|valorization|valorisation|cyclohexanol|cyclohexanone|cyclohexane|benzene)\b",
    r"\bphenolic compounds? [^.\n]{0,120}(?:aryl C[- ]O|C[- ]O bond|cleavage|hydrogenation|hydrogenolysis|hydrodeoxygenation|reduction|conversion|upgrading)\b",
    r"\b(?:hydrogenation|reduction|hydrodeoxygenation|deoxygenation|conversion|upgrading|valorization|valorisation) of phenol(?:s|ic)?\b",
    r"\b(?:hydrogenation|reduction|valorization|valorisation) of [^.\n]{0,120}(?:phenol|phenolic contaminant|phenolic contaminants)\b",
    r"\b(?:contaminant|contaminants) [^.\n]{0,120}(?:hydrogenation|valorization|valorisation) [^.\n]{0,120}(?:phenol|phenolic)\b",
    r"\b(?:electrochemical|electrocatalytic|cathodic) [^.\n]{0,80}(?:hydrogenolysis|hydrodeoxygenation|C[- ]O bond cleavage) of (?:aryl ethers?|phenolic compounds?)\b",
    r"\bphenolic contaminant(?:s)?[- ]to[- ]chemical conversion\b",
    r"\bguaiacol .{0,80}(?:hydrogenation|reduction|hydrodeoxygenation|deoxygenation|conversion)\b",
    r"\b(?:hydrogenation|reduction|hydrodeoxygenation|deoxygenation|conversion) of guaiacol\b",
    r"\baryl C[- ]O bond(?:s)?\b",
    r"\b(?:aryl|diaryl) ether(?:s)? [^.\n]{0,100}(?:electrocatalytic hydrogenolysis|electrochemical hydrogenolysis|C[- ]O cleavage|hydrogenolysis|hydrodeoxygenation|hydrogenation)\b",
]


def classify_phenol_direction(text: str) -> str:
    if count_patterns(text, [r"\bflow\b", r"\bmembrane\b", r"\bpaired electrolysis\b", r"\breactor\b", r"\bcell design\b"]):
        return "reactor_system_engineering"
    if count_patterns(text, [r"\bguaiacol\b", r"\bsyringol\b", r"\bvanillin\b", r"\blignin\b", r"\bbiomass\b"]):
        if count_patterns(text, [r"\bhydrodeoxygenation\b", r"\bdeoxygenation\b", r"\bC[- ]O bond cleavage\b"]):
            return "phenolic_hydrodeoxygenation"
        return "lignin_phenolic_upgrading"
    if count_patterns(text, [r"\bhydrodeoxygenation\b", r"\bdeoxygenation\b", r"\bC[- ]O bond cleavage\b", r"\bhydrogenolysis\b"]):
        return "phenolic_hydrodeoxygenation"
    if count_patterns(text, [r"\bDFT\b", r"\bdensity functional theory\b", r"\boperando\b", r"\bin situ\b", r"\bmechanis", r"\bPCET\b"]):
        return "mechanism_operando_dft"
    if count_patterns(text, [r"\bcatalyst design\b", r"\belectrode\b", r"\balloy\b", r"\bsingle[- ]atom\b", r"\bnanomaterial\b", r"\binterface\b"]):
        return "catalyst_material_design"
    if count_patterns(text, [r"\bphenol\b", r"\bcyclohexanol\b", r"\bcyclohexanone\b", r"\bcyclohexane\b"]):
        return "direct_phenol_electrohydrogenation"
    return "other_phenol_electroreduction"


def screen_phenol_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:160000]}"
    early_text = f"{title_abs}\n\n{full[:10000]}"
    title = row["title"] or ""

    substrate_count = count_patterns(text, PHENOL_SUBSTRATE_PATTERNS)
    substrate_title_abs = count_patterns(title_abs, PHENOL_SUBSTRATE_PATTERNS)
    centrality_count = count_patterns(text, PHENOL_CENTRALITY_PATTERNS)
    centrality_title_abs = count_patterns(title_abs, PHENOL_CENTRALITY_PATTERNS)
    electro_reduction_count = count_patterns(text, PHENOL_ELECTRO_REDUCTION_PATTERNS)
    electro_title_abs = count_patterns(title_abs, PHENOL_ELECTRO_REDUCTION_PATTERNS)
    electro_context_count = count_patterns(text, PHENOL_ELECTROCHEM_CONTEXT_PATTERNS)
    electro_context_early = count_patterns(early_text, PHENOL_ELECTROCHEM_CONTEXT_PATTERNS)
    product_count = count_patterns(text, PHENOL_PRODUCT_PATH_PATTERNS)
    domain_count = count_patterns(text, PHENOL_DOMAIN_PATTERNS)
    domain_early = count_patterns(early_text, PHENOL_DOMAIN_PATTERNS)
    direction = classify_phenol_direction(title_abs)
    if direction == "other_phenol_electroreduction" and stage == "fulltext":
        direction = classify_phenol_direction(early_text)
    substrate_evidence = best_evidence(text, PHENOL_SUBSTRATE_PATTERNS)
    reaction_evidence = best_evidence(text, PHENOL_ELECTRO_REDUCTION_PATTERNS + PHENOL_PRODUCT_PATH_PATTERNS)
    electro_evidence = best_evidence(text, PHENOL_ELECTROCHEM_CONTEXT_PATTERNS)
    evidence = reaction_evidence or substrate_evidence or electro_evidence
    method_primary = 1 if (electro_title_abs > 0 or (electro_reduction_count > 0 and electro_context_count > 0)) else 0
    score = float(substrate_count * 6 + centrality_count * 8 + electro_reduction_count * 8 + electro_context_count * 3 + product_count * 3 + domain_count * 2 + min(int(row["citation_count"] or 0), 200) / 20)

    if any(re.search(p, title, flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if count_patterns(title_abs, PHENOL_REVIEW_PATTERNS) or (stage == "fulltext" and count_patterns(early_text[:6000], PHENOL_REVIEW_PATTERNS)):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/commentary; separated from original research.", score
    if count_patterns(title_abs, PHENOL_PHOTO_EXCLUSION_PATTERNS) or (stage == "fulltext" and count_patterns(early_text, PHENOL_PHOTO_EXCLUSION_PATTERNS)):
        return "excluded", direction, method_primary, evidence or "Photocatalysis/photoelectrocatalysis is excluded by profile.", score
    if substrate_count == 0 or (stage == "fulltext" and substrate_title_abs == 0 and count_patterns(early_text, PHENOL_SUBSTRATE_PATTERNS) == 0):
        return "excluded", direction, method_primary, evidence or "No phenol/phenolic substrate or product-family evidence found.", score
    if count_patterns(title_abs, PHENOL_NON_REACTION_EXCLUSION_PATTERNS) and count_patterns(title, PHENOL_SUBSTRATE_PATTERNS) == 0:
        return "excluded", direction, method_primary, evidence or "Phenol is used as a proton donor, sensing/adsorption target, or other non-substrate context.", score
    if centrality_count == 0 or (stage != "fulltext" and centrality_title_abs == 0 and substrate_title_abs == 0):
        return "excluded", direction, method_primary, evidence or "Phenol/phenolic compound is not clearly a central reduction, hydrogenation, hydrodeoxygenation, or upgrading substrate/product family.", score
    if domain_count == 0 or (stage == "fulltext" and domain_early == 0):
        return "excluded", direction, method_primary, evidence or "No catalysis, chemistry, or materials-domain evidence found.", score
    if electro_reduction_count == 0 or electro_context_count == 0 or (stage == "fulltext" and electro_context_early == 0):
        return "excluded", direction, method_primary, evidence or "No central electrochemical/electrocatalytic phenol reduction or hydrogenation evidence found.", score
    if count_patterns(title_abs, PHENOL_OXIDATION_EXCLUSION_PATTERNS) and electro_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Phenol oxidation/degradation/AOP topic rather than cathodic reduction or hydrogenation.", score
    if count_patterns(title_abs, PHENOL_NON_REACTION_EXCLUSION_PATTERNS) and centrality_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Phenol is used in sensing, adsorption, extraction, or non-reaction context.", score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "Electrocatalytic phenol reduction/hydrogenation appears peripheral rather than central.", score
    if stage == "fulltext" and int(row["pdf_pages"] or 0) == 1:
        return "excluded", direction, 0, evidence or "One-page/non-main-article PDF; excluded from final original research set.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence, score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence, score


EHR_REACTION_PATTERNS = [
    r"\belectrocatalytic hydrogenation\b",
    r"\belectrochemical hydrogenation\b",
    r"\belectro[- ]?hydrogenation\b",
    r"\belectrocatalytic reduction\b",
    r"\belectrochemical reduction\b",
    r"\belectro[- ]?reduction\b",
    r"\bcathodic hydrogenation\b",
    r"\bcathodic reduction\b",
    r"\belectrochemical hydrodeoxygenation\b",
    r"\belectrocatalytic hydrodeoxygenation\b",
    r"\belectro[- ]?hydrodeoxygenation\b",
    r"\belectrochemical hydrogenolysis\b",
    r"\belectrocatalytic hydrogenolysis\b",
    r"\belectrochemical hydrodehalogenation\b",
    r"\belectrocatalytic hydrodehalogenation\b",
    r"\bCO2 electroreduction\b",
    r"\bCO2 reduction reaction\b",
    r"\bcarbon dioxide electroreduction\b",
    r"\belectrochemical CO2 reduction\b",
    r"\belectrocatalytic CO2 reduction\b",
    r"\bnitrate electroreduction\b",
    r"\belectrochemical nitrate reduction\b",
    r"\belectrocatalytic nitrate reduction\b",
    r"\bnitrite electroreduction\b",
    r"\bnitrogen reduction reaction\b",
    r"\belectrochemical nitrogen reduction\b",
    r"\belectrocatalytic nitrogen reduction\b",
    r"\bnitric oxide electroreduction\b",
    r"\belectrochemical NO reduction\b",
    r"\bN2O electroreduction\b",
]

EHR_ELECTROCHEM_CONTEXT_PATTERNS = [
    r"\belectrocatalytic\b",
    r"\belectrocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\belectrochemical\b",
    r"\belectrolysis\b",
    r"\belectroly[sz]er\b",
    r"\belectrode\b",
    r"\bcathode\b",
    r"\bcathodic\b",
    r"\bFaradaic\b",
    r"\bcurrent density\b",
    r"\bapplied potential\b",
    r"\bpotential vs\.? RHE\b",
]

EHR_TARGET_PATTERNS = [
    r"\bCO2\b",
    r"\bcarbon dioxide\b",
    r"\bCO2RR\b",
    r"\bformate\b",
    r"\bformic acid\b",
    r"\bmethanol\b",
    r"\bmethane\b",
    r"\bethanol\b",
    r"\bethene\b",
    r"\bethane\b",
    r"\bC2\+?\b",
    r"\bnitrate\b",
    r"\bnitrite\b",
    r"\bammonia\b",
    r"\bammonium\b",
    r"\bnitrogen\b",
    r"\bN2\b",
    r"\bnitric oxide\b",
    r"\bNO\b",
    r"\bnitrous oxide\b",
    r"\bN2O\b",
    r"\bphenol(?:s|ic)?\b",
    r"\bfurfural\b",
    r"\b5[- ]?hydroxymethylfurfural\b",
    r"\bHMF\b",
    r"\bbiomass[- ]derived\b",
    r"\blignin[- ]derived\b",
    r"\bbenzaldehyde\b",
    r"\baldehyde(?:s)?\b",
    r"\bketone(?:s)?\b",
    r"\bcarbonyl(?:s)?\b",
    r"\bnitroarene(?:s)?\b",
    r"\bnitrobenzene\b",
    r"\balkyne(?:s)?\b",
    r"\balkene(?:s)?\b",
    r"\barene(?:s)?\b",
    r"\bC[- ]O bond(?:s)?\b",
    r"\bC[- ]X bond(?:s)?\b",
    r"\baryl ether(?:s)?\b",
    r"\bhalogenated\b",
    r"\bchlorinated\b",
    r"\bdechlorination\b",
    r"\bdehalogenation\b",
    r"\bcontaminant(?:s)?\b",
    r"\bpollutant(?:s)?\b",
    r"\btrichloroacetic acid\b",
    r"\bTCAA\b",
]

EHR_DOMAIN_PATTERNS = [
    r"\b\w*catalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\bcatalytic\b",
    r"\belectrocatalys",
    r"\bmaterials?\b",
    r"\bnanomaterial\b",
    r"\belectrode\b",
    r"\bsingle[- ]atom\b",
    r"\balloy\b",
    r"\bcluster\b",
    r"\bframework\b",
    r"\bmembrane\b",
    r"\breactor\b",
]

EHR_REVIEW_PATTERNS = [
    r"^\s*review\b",
    r"^\s*mini[- ]review\b",
    r"^\s*perspective\b",
    r"^\s*viewpoint\b",
    r"^\s*comment(?:ary)?\b",
    r"^\s*editorial\b",
    r"^\s*correction\b",
    r"^\s*erratum\b",
    r"\bresearch briefing\b",
]

EHR_EXCLUSION_PATTERNS = [
    r"\bhydrogen evolution reaction\b",
    r"\bHER\b",
    r"\boxygen evolution reaction\b",
    r"\bOER\b",
    r"\boxygen reduction reaction\b",
    r"\bORR\b",
    r"\bwater splitting\b",
    r"\bseawater splitting\b",
    r"\bmetal[- ]air batter",
    r"\bfuel cell\b",
    r"\blithium[- ]ion batter",
    r"\bsodium[- ]ion batter",
    r"\bzinc[- ]ion batter",
    r"\bsupercapacitor\b",
    r"\bphotocatal",
    r"\bphotoelectrocatal",
    r"\bphotoelectrochemical\b",
    r"\bphotocathode(?:s)?\b",
    r"\bvisible[- ]light\b",
    r"\bsolar[- ]driven\b",
    r"\bsensing\b",
    r"\bsensor(?:s)?\b",
    r"\badsorption\b",
]

EHR_STRONG_TITLE_EXCLUSION_PATTERNS = [
    r"\b(hydrogen evolution reaction|HER|oxygen evolution reaction|OER|oxygen reduction reaction|ORR|water splitting|metal[- ]air batter|fuel cell|photoelectrochemical|photocathode)\b",
    r"\b(lithium|sodium|zinc)[- ]ion batter",
    r"\bsupercapacitor\b",
]

EHR_CO2RR_EXCLUSION_PATTERNS = [
    r"\bCO2RR\b",
    r"\bC\s*O\s*(?:2|₂)\s*(?:electro)?reduction\b",
    r"\bC\s*O\s*(?:2|₂)\s*reduction reaction\b",
    r"\belectroreduction of C\s*O\s*(?:2|₂)\b",
    r"\breduction of C\s*O\s*(?:2|₂)\b",
    r"\bC\s*O\s*(?:2|₂)\s*electrochemical conversion\b",
    r"\bC\s*O\s*(?:2|₂)\s*hydrogenation\b",
    r"\belectrochemical conversion of C\s*O\s*(?:2|₂)\b",
    r"\bhydrogenation of C\s*O\s*(?:2|₂)\b",
    r"\belectrocatalytic hydrogenation of C\s*O\s*(?:2|₂)\b",
    r"\belectrochemical hydrogenation of C\s*O\s*(?:2|₂)\b",
    r"\bcarbon dioxide (?:electro)?reduction\b",
    r"\breduction of carbon dioxide\b",
    r"\belectroreduction of carbon dioxide\b",
    r"\bcarbon dioxide electrochemical conversion\b",
    r"\bcarbon dioxide hydrogenation\b",
    r"\bhydrogenation of carbon dioxide\b",
    r"\belectrochemical CO\s*2 reduction\b",
    r"\belectrocatalytic CO\s*2 reduction\b",
    r"\bECR\b.{0,80}\b(?:formate|formic acid|CO|C2|ethylene|ethanol|methanol|methane)\b",
    r"\bC\s*O\s*(?:2|₂)[- ]to[- ](?:CO|formate|formic acid|methanol|methane|C2)\b",
]

EHR_NITRATE_EXCLUSION_PATTERNS = [
    r"\bnitrate (?:electro)?reduction\b",
    r"\bnitrate reduction reaction\b",
    r"\breduction of nitrate\b",
    r"\belectroreduction of nitrate\b",
    r"\belectrochemical nitrate reduction\b",
    r"\belectrocatalytic nitrate reduction\b",
    r"\bnitrate electrochemical hydrogenation\b",
    r"\bnitrate hydrogenation\b",
    r"\belectrochemical nitrate hydrogenation\b",
    r"\belectrocatalytic nitrate hydrogenation\b",
    r"\bnitrate electrochemical conversion\b",
    r"\bammonia from nitrate\b",
    r"\bnitrite (?:electro)?reduction\b",
    r"\bnitrite reduction reaction\b",
    r"\breduction of nitrite\b",
    r"\belectroreduction of nitrite\b",
    r"\belectrochemical nitrite reduction\b",
    r"\belectrocatalytic nitrite reduction\b",
    r"\bNO\s*3\s*RR\b",
    r"\bNO3RR\b",
    r"\bNO\s*2\s*RR\b",
    r"\bNO2RR\b",
    r"\bNO\s*3\s*[-−]\s*(?:electro)?reduction\b",
    r"\bNO\s*2\s*[-−]\s*(?:electro)?reduction\b",
]

EHR_DIRECTION_RULES = [
    ("hydrogen_peroxide_anthraquinone_reduction", [r"\banthraquinone\b", r"\bhydrogen peroxide\b", r"\bH\s*2\s*O\s*2\b"]),
    ("co_electroreduction", [r"\bCO electroreduction\b", r"\bCO reduction\b", r"\bcarbon monoxide electroreduction\b", r"\bcarbon monoxide reduction\b", r"\bCO to ethanol\b", r"\bCO-to-ethanol\b"]),
    ("co2_electroreduction", [r"\bCO2\b", r"\bcarbon dioxide\b", r"\bCO2RR\b", r"\bCO2 reduction\b", r"\bformate\b", r"\bformic acid\b"]),
    ("nitrogen_oxide_nitrate_reduction", [r"\bnitrate\b", r"\bnitrite\b", r"\bnitrogen reduction\b", r"\bN2\b", r"\bnitric oxide\b", r"\bN2O\b", r"\bammonia\b", r"\bammonium\b"]),
    ("organic_electrosynthesis_hydrogenation", [r"\baldehyde", r"\bketone", r"\bcarbonyl", r"\bnitroarene", r"\bnitrobenzene", r"\balkyne", r"\balkene", r"\barene", r"\bbenzaldehyde"]),
    ("biomass_phenolic_furan_upgrading", [r"\bphenol", r"\bphenolic", r"\bfurfural", r"\bHMF\b", r"\b5[- ]?hydroxymethylfurfural", r"\blignin", r"\bbiomass"]),
    ("hydrodehalogenation_dehalogenation", [r"\bdehalogenation", r"\bdechlorination", r"\bhydrodehalogenation", r"\bhalogenated", r"\bchlorinated", r"\bC[- ]X bond"]),
    ("contaminant_water_purification_reduction", [r"\bcontaminant", r"\bpollutant", r"\bwater purification", r"\bwastewater", r"\btrichloroacetic acid", r"\bTCAA"]),
    ("reactor_membrane_paired_electrolysis", [r"\bmembrane", r"\bflow cell", r"\bflow reactor", r"\bpaired electrolysis", r"\belectroly[sz]er", r"\breactor"]),
    ("catalyst_material_design_mechanism", [r"\bsingle[- ]atom", r"\balloy", r"\bcluster", r"\bnanotube", r"\bframework", r"\bDFT\b", r"\bmechanis", r"\boperando", r"\bin situ"]),
]


def classify_ehr_direction(text: str) -> str:
    titleish = text[:1200]
    if count_patterns(titleish, [r"\bmembrane", r"\bflow cell", r"\bflow reactor", r"\bpaired electrolysis", r"\belectroly[sz]er"]):
        return "reactor_membrane_paired_electrolysis"
    scores = {label: count_patterns(text, patterns) for label, patterns in EHR_DIRECTION_RULES}
    if scores:
        best = max(scores.items(), key=lambda kv: kv[1])
        if best[1] > 0:
            return best[0]
    return "catalyst_material_design_mechanism"


def screen_ehr_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:160000]}"
    early_text = f"{title_abs}\n\n{full[:12000]}"
    title = row["title"] or ""

    reaction_count = count_patterns(text, EHR_REACTION_PATTERNS)
    reaction_title_abs = count_patterns(title_abs, EHR_REACTION_PATTERNS)
    electro_count = count_patterns(text, EHR_ELECTROCHEM_CONTEXT_PATTERNS)
    electro_early = count_patterns(early_text, EHR_ELECTROCHEM_CONTEXT_PATTERNS)
    target_count = count_patterns(text, EHR_TARGET_PATTERNS)
    target_title_abs = count_patterns(title_abs, EHR_TARGET_PATTERNS)
    domain_count = count_patterns(text, EHR_DOMAIN_PATTERNS)
    domain_early = count_patterns(early_text, EHR_DOMAIN_PATTERNS)
    title_exclusion = count_patterns(title, EHR_STRONG_TITLE_EXCLUSION_PATTERNS)
    exclusion_count = count_patterns(title_abs, EHR_EXCLUSION_PATTERNS)
    direction = classify_ehr_direction(title_abs)
    if direction == "catalyst_material_design_mechanism" and stage == "fulltext":
        direction = classify_ehr_direction(early_text)

    reaction_evidence = best_evidence(text, EHR_REACTION_PATTERNS)
    target_evidence = best_evidence(text, EHR_TARGET_PATTERNS)
    electro_evidence = best_evidence(text, EHR_ELECTROCHEM_CONTEXT_PATTERNS)
    evidence = reaction_evidence or target_evidence or electro_evidence
    method_primary = 1 if (reaction_title_abs > 0 or (reaction_count > 0 and electro_count > 0 and target_count > 0)) else 0
    score = float(reaction_count * 9 + electro_count * 3 + target_count * 5 + domain_count * 2 + min(int(row["citation_count"] or 0), 250) / 20)

    if any(re.search(p, title, flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if re.search(r"\b(?:review|perspective|perspectives|outlook|challenges and perspectives)\b", title, flags=re.I):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/outlook; separated from original research.", score
    if count_patterns(title_abs, EHR_REVIEW_PATTERNS) or (stage == "fulltext" and count_patterns(early_text[:6000], EHR_REVIEW_PATTERNS)):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/commentary; separated from original research.", score
    if re.search(r"\b(?:photoelectrochemical|photoelectrocatalytic|photocathodes?|photocatalytic)\b", title, flags=re.I):
        return "excluded", direction, method_primary, evidence or "Photoelectrochemical/photocatalytic topic is outside the configured electrocatalysis-only scope.", score
    if title_exclusion and reaction_title_abs == 0 and target_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Energy storage, HER/OER/ORR, water splitting, or fuel-cell topic without central electrocatalytic reduction/hydrogenation target.", score
    if exclusion_count and reaction_title_abs == 0 and target_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Excluded topic such as HER/OER/ORR, water splitting, battery, photocatalysis, sensing, or adsorption without central ECH/ER.", score
    if reaction_count == 0 or electro_count == 0:
        return "excluded", direction, method_primary, evidence or "No central electrochemical/electrocatalytic hydrogenation or reduction evidence found.", score
    if target_count == 0 or (stage != "fulltext" and target_title_abs == 0 and reaction_title_abs == 0):
        return "excluded", direction, method_primary, evidence or "No clear reducible substrate, pollutant, CO2/nitrogen species, or organic hydrogenation target found.", score
    if domain_count == 0 or (stage == "fulltext" and domain_early == 0):
        return "excluded", direction, method_primary, evidence or "No catalysis, chemistry, or materials-domain evidence found.", score
    if stage == "fulltext" and electro_early == 0:
        return "excluded", direction, method_primary, evidence or "Electrochemical context appears peripheral in the full text.", score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "Electrocatalytic hydrogenation/reduction appears peripheral rather than central.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence, score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence, score


def screen_ehr_no_co2rr_nitrate_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    direction = classify_ehr_direction(title_abs)
    score = float(min(int(row["citation_count"] or 0), 250) / 20)
    if count_patterns(title_abs, EHR_CO2RR_EXCLUSION_PATTERNS):
        evidence = best_evidence(title_abs, EHR_CO2RR_EXCLUSION_PATTERNS)
        return "excluded", "excluded_co2rr", 0, evidence or "Excluded by profile: CO2RR is out of scope.", score
    if count_patterns(title_abs, EHR_NITRATE_EXCLUSION_PATTERNS):
        evidence = best_evidence(title_abs, EHR_NITRATE_EXCLUSION_PATTERNS)
        return "excluded", "excluded_nitrate_nitrite_reduction", 0, evidence or "Excluded by profile: nitrate/nitrite reduction is out of scope.", score
    return screen_ehr_row(row, stage, require_pdf, require_method_primary)


WOR_REACTION_PATTERNS = [
    r"\btwo[- ]electron water oxidation\b",
    r"\b2\s*e(?:\s*[-–−]|\s*electron)?[- ]water oxidation\b",
    r"\b2\s*e(?:\s*[-–−]|\s*electron)?[- ]?WOR\b",
    r"\bone[- ]electron water oxidation\b",
    r"\b1\s*e(?:\s*[-–−]|\s*electron)?[- ]water oxidation\b",
    r"\b1\s*e(?:\s*[-–−]|\s*electron)?[- ]?WOR\b",
    r"\bwater oxidation reaction [^.\n]{0,120}(?:hydrogen peroxide|H\s*2\s*O\s*2|peroxide|hydroxyl radical|[·•]\s*OH)\b",
    r"\bwater oxidation [^.\n]{0,120}(?:hydrogen peroxide|H\s*2\s*O\s*2|peroxide|hydroxyl radical|[·•]\s*OH|reactive oxygen species|ROS)\b",
    r"\b(?:hydrogen peroxide|H\s*2\s*O\s*2|peroxide|hydroxyl radical|[·•]\s*OH|reactive oxygen species|ROS) [^.\n]{0,120}(?:from|via|through|by) [^.\n]{0,80}water oxidation\b",
    r"\bwater[- ]derived (?:hydroxyl radical|[·•]\s*OH|reactive oxygen species|ROS|high[- ]valent|metal[- ]oxo)\b",
    r"\belectrocatalytic [^.\n]{0,80}(?:hydroxyl radical|[·•]\s*OH) generation [^.\n]{0,120}water\b",
    r"\belectrocatalytic [^.\n]{0,120}one[- ]electron [^.\n]{0,80}water oxidation\b",
    r"\belectrochemical [^.\n]{0,120}water oxidation [^.\n]{0,120}(?:H\s*2\s*O\s*2|hydrogen peroxide|[·•]\s*OH|hydroxyl radical)\b",
    r"\bwater activation [^.\n]{0,120}(?:high[- ]valent metal[- ]oxo|metal[- ]oxo|M\s*=\s*O|[·•]\s*OH|hydroxyl radical)\b",
]

WOR_PRODUCT_PATTERNS = [
    r"\bhydrogen peroxide\b",
    r"\bH\s*2\s*O\s*2\b",
    r"\bperoxide\b",
    r"\bhydroperoxyl\b",
    r"\bHOO\*\b",
    r"\bOOH\*\b",
    r"\bhydroxyl radical\b",
    r"\bOH radical\b",
    r"[·•]\s*OH\b",
    r"\bsurface[- ]bound hydroxyl\b",
    r"\breactive oxygen species\b",
    r"\bROS\b",
    r"\bhigh[- ]valent metal[- ]oxo\b",
    r"\bmetal[- ]oxo\b",
    r"\bM\s*=\s*O\b",
    r"\bCo\s*\(?IV\)?\s*=\s*O\b",
    r"\bferryl\b",
    r"\boxygen atom transfer\b",
]

WOR_CONTEXT_PATTERNS = [
    r"\belectrocatalys(?:t|ts|is|ed|es|ing|tic)\b",
    r"\belectrochemical\b",
    r"\belectro[- ]oxidation\b",
    r"\banodic oxidation\b",
    r"\banode\b",
    r"\bthermal catalys",
    r"\bthermocatalys",
    r"\bcatalytic\b",
    r"\broom[- ]temperature\b",
    r"\bambient\b",
    r"\bwastewater\b",
    r"\bwater purification\b",
    r"\badvanced oxidation\b",
    r"\bAOPs?\b",
]

WOR_DOMAIN_PATTERNS = [
    r"\bcatalys(?:t|ts|is|ed|es|ing|tic|ator|ators)\b",
    r"\belectrode\b",
    r"\banode\b",
    r"\bmaterials?\b",
    r"\bsingle[- ]atom\b",
    r"\bmetal[- ]oxide\b",
    r"\bMOF\b",
    r"\bCOF\b",
    r"\bcarbon\b",
    r"\bDFT\b",
    r"\boperando\b",
    r"\bin situ\b",
]

WOR_REVIEW_PATTERNS = [
    r"^\s*review\b",
    r"^\s*mini[- ]review\b",
    r"^\s*perspective\b",
    r"^\s*viewpoint\b",
    r"^\s*comment(?:ary)?\b",
    r"^\s*editorial\b",
    r"^\s*correction\b",
    r"^\s*erratum\b",
    r"\bprospects and promises\b",
    r"\brecent advances\b",
]

WOR_PHOTO_EXCLUSION_PATTERNS = [
    r"\bphotocatal",
    r"\bphotoelectro",
    r"\bphoto[- ]?Fenton\b",
    r"\bvisible[- ]light\b",
    r"\bsolar[- ]driven\b",
    r"\blight[- ]driven\b",
    r"\bphotosynth",
]

WOR_4E_EXCLUSION_PATTERNS = [
    r"\boxygen evolution reaction\b",
    r"\bOER\b",
    r"\b4\s*e(?:\s*[-–−]|\s*electron)?[- ]water oxidation\b",
    r"\bfour[- ]electron water oxidation\b",
    r"\bwater splitting\b",
    r"\boverall water splitting\b",
    r"\bwater electrolysis\b",
    r"\belectroly[sz]er\b",
    r"\boxygen evolution\b",
    r"\bacidic water oxidation\b",
    r"\balkaline water oxidation\b",
]

WOR_UNRELATED_EXCLUSION_PATTERNS = [
    r"\boxygen reduction reaction\b",
    r"\bORR\b",
    r"\bhydrogen evolution reaction\b",
    r"\bHER\b",
    r"\bCO\s*2 reduction\b",
    r"\bCO2RR\b",
    r"\bnitrate reduction\b",
    r"\bNO3RR\b",
    r"\bbatter(?:y|ies)\b",
    r"\bfuel cell\b",
    r"\bsupercapacitor\b",
    r"\badsorption\b",
    r"\bsensing\b",
]


def classify_wor_direction(text: str) -> str:
    if count_patterns(text, [r"\bH\s*2\s*O\s*2\b", r"\bhydrogen peroxide\b", r"\btwo[- ]electron\b", r"\b2\s*e"]):
        if count_patterns(text, [r"\bcarbonate\b", r"\bCO\s*2\b", r"\bmediated\b"]):
            return "2e_wor_h2o2_mediated_pathways"
        return "2e_wor_h2o2_electrocatalysis"
    if count_patterns(text, [r"\bone[- ]electron\b", r"\b1\s*e", r"\bhydroxyl radical\b", r"[·•]\s*OH\b"]):
        if count_patterns(text, [r"\bwastewater\b", r"\bwater purification\b", r"\bcontaminant\b", r"\bpollutant\b"]):
            return "1e_wor_hydroxyl_radical_water_treatment"
        return "1e_wor_hydroxyl_radical"
    if count_patterns(text, [r"\bhigh[- ]valent\b", r"\bmetal[- ]oxo\b", r"\bM\s*=\s*O\b", r"\bCo\s*\(?IV\)?\s*=\s*O\b", r"\bferryl\b"]):
        return "high_valent_metal_oxo_water_activation"
    if count_patterns(text, [r"\broom[- ]temperature\b", r"\bambient\b", r"\bthermal catalys", r"\bthermocatalys"]):
        return "ambient_thermal_catalytic_wor"
    if count_patterns(text, [r"\bDFT\b", r"\boperando\b", r"\bin situ\b", r"\bmechanis", r"\bPCET\b", r"\bOOH\*\b", r"\bOH\*\b"]):
        return "mechanism_dft_operando_pcet"
    if count_patterns(text, [r"\bflow\b", r"\bmembrane\b", r"\breactor\b", r"\banode\b"]):
        return "reactor_anode_device_real_water"
    return "other_1e_2e_wor"


def screen_wor_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:180000]}"
    early_text = f"{title_abs}\n\n{full[:16000]}"
    title = row["title"] or ""

    reaction_count = count_patterns(text, WOR_REACTION_PATTERNS)
    reaction_title_abs = count_patterns(title_abs, WOR_REACTION_PATTERNS)
    product_count = count_patterns(text, WOR_PRODUCT_PATTERNS)
    product_title_abs = count_patterns(title_abs, WOR_PRODUCT_PATTERNS)
    context_count = count_patterns(text, WOR_CONTEXT_PATTERNS)
    context_early = count_patterns(early_text, WOR_CONTEXT_PATTERNS)
    domain_count = count_patterns(text, WOR_DOMAIN_PATTERNS)
    domain_early = count_patterns(early_text, WOR_DOMAIN_PATTERNS)
    photo_count = count_patterns(title_abs, WOR_PHOTO_EXCLUSION_PATTERNS)
    four_e_title = count_patterns(title, WOR_4E_EXCLUSION_PATTERNS)
    four_e_title_abs = count_patterns(title_abs, WOR_4E_EXCLUSION_PATTERNS)
    unrelated_title = count_patterns(title, WOR_UNRELATED_EXCLUSION_PATTERNS)
    direction = classify_wor_direction(title_abs)
    if direction == "other_1e_2e_wor" and stage == "fulltext":
        direction = classify_wor_direction(early_text)

    reaction_evidence = best_evidence(text, WOR_REACTION_PATTERNS)
    product_evidence = best_evidence(text, WOR_PRODUCT_PATTERNS)
    context_evidence = best_evidence(text, WOR_CONTEXT_PATTERNS)
    evidence = reaction_evidence or product_evidence or context_evidence
    method_primary = 1 if (reaction_title_abs > 0 or (reaction_count > 0 and product_count > 0 and context_count > 0)) else 0
    score = float(reaction_count * 10 + product_count * 5 + context_count * 3 + domain_count * 2 + min(int(row["citation_count"] or 0), 250) / 20)

    if any(re.search(p, title, flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if count_patterns(title_abs, WOR_REVIEW_PATTERNS):
        return "peripheral", direction, method_primary, evidence or "Review/perspective/commentary; separated from original research.", score
    if photo_count:
        return "excluded", direction, method_primary, evidence or "Photocatalysis/photoelectrocatalysis is excluded.", score
    if unrelated_title and reaction_title_abs == 0 and product_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Unrelated energy/storage/reduction/sensing topic without central 1e/2e WOR evidence.", score
    if four_e_title and reaction_title_abs == 0 and not count_patterns(title_abs, [r"\bhydrogen peroxide\b", r"\bH\s*2\s*O\s*2\b", r"\bhydroxyl radical\b", r"[·•]\s*OH\b", r"\bone[- ]electron\b", r"\btwo[- ]electron\b"]):
        return "excluded", direction, method_primary, evidence or "Title indicates conventional 4e OER/water splitting rather than 1e/2e WOR.", score
    if stage != "fulltext" and four_e_title_abs and reaction_title_abs == 0 and product_title_abs == 0:
        return "excluded", direction, method_primary, evidence or "Abstract indicates 4e OER/water splitting without 1e/2e WOR product.", score
    if reaction_count == 0:
        return "excluded", direction, method_primary, evidence or "No explicit one-/two-electron water oxidation, water-to-H2O2, or water-derived radical/oxo evidence.", score
    if product_count == 0:
        return "excluded", direction, method_primary, evidence or "No H2O2, hydroxyl radical, ROS, or high-valent metal-oxo product/intermediate evidence.", score
    if context_count == 0 or (stage == "fulltext" and context_early == 0):
        return "excluded", direction, method_primary, evidence or "No electrochemical/catalytic/ambient/water-treatment context found.", score
    if domain_count == 0 or (stage == "fulltext" and domain_early == 0):
        return "excluded", direction, method_primary, evidence or "No catalysis, chemistry, or materials-domain evidence found.", score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "1e/2e WOR appears peripheral rather than central.", score
    if stage == "fulltext" and int(row["pdf_pages"] or 0) <= 1:
        return "excluded", direction, 0, evidence or "One-page/non-main-article PDF; excluded from final original research set.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence, score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence, score


def screen_row_for_profile(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool, profile: Dict[str, Any]) -> Tuple[str, str, int, str, float]:
    screening_mode = profile_str(profile, "screening_mode", "generic")
    if screening_mode == "spinel_oxide_catalysis":
        return screen_spinel_row(row, stage, require_pdf)
    if screening_mode == "high_throughput_screening":
        return screen_high_throughput_row(row, stage, require_pdf, require_method_primary)
    if screening_mode == "phenol_electrocatalytic_hydrogenation_reduction":
        return screen_phenol_row(row, stage, require_pdf, require_method_primary)
    if screening_mode == "electrocatalytic_hydrogenation_reduction":
        return screen_ehr_row(row, stage, require_pdf, require_method_primary)
    if screening_mode == "electrocatalytic_hydrogenation_reduction_no_co2rr_nitrate":
        return screen_ehr_no_co2rr_nitrate_row(row, stage, require_pdf, require_method_primary)
    if screening_mode == "one_two_electron_wor":
        return screen_wor_row(row, stage, require_pdf, require_method_primary)
    if screening_mode == "environmental_catalysis_theory_ml":
        return screen_environmental_row(row, stage, require_pdf, require_method_primary)
    return screen_generic_row(row, stage, require_pdf, require_method_primary, profile)


def classify_direction(text: str) -> str:
    scores: Dict[str, int] = {}
    for label, patterns in DIRECTION_RULES:
        scores[label] = count_patterns(text, patterns)
    non_hybrid = {k: v for k, v in scores.items() if k != "hybrid_theory_experiment"}
    best = max(non_hybrid.items(), key=lambda kv: kv[1])
    if best[1] > 0:
        return best[0]
    if scores.get("hybrid_theory_experiment", 0) > 0:
        return "hybrid_theory_experiment"
    return "other"


def consolidate_direction(direction: str, text: str) -> str:
    if re.search(r"machine learning|deep learning|graph neural|random forest|XGBoost|QSAR|knowledge extraction|data[- ]driven", text, flags=re.I):
        return "ml_data_driven_screening"
    if direction in {"mechanism_dft", "reaction_pathway", "molecular_simulation", "hybrid_theory_experiment", "catalyst_screening"}:
        return "theory_simulation_mechanism"
    return direction


def screen_environmental_row(row: sqlite3.Row, stage: str, require_pdf: bool, require_method_primary: bool) -> Tuple[str, str, int, str, float]:
    title_abs = f"{row['title'] or ''}. {row['abstract'] or ''}"
    full = row["full_text"] or ""
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:120000]}"
    early_text = f"{title_abs}\n\n{full[:4000]}"
    domain_count = count_patterns(text, DOMAIN_PATTERNS)
    domain_early_count = count_patterns(early_text, DOMAIN_PATTERNS)
    environmental_core_count = count_patterns(text, ENVIRONMENTAL_CORE_PATTERNS)
    environmental_core_early_count = count_patterns(early_text, ENVIRONMENTAL_CORE_PATTERNS)
    environmental_core_title_count = count_patterns(title_abs, ENVIRONMENTAL_CORE_PATTERNS)
    energy_only_count = count_patterns(title_abs, ENERGY_ONLY_EXCLUSION_PATTERNS)
    catalysis_count = count_patterns(text, CATALYSIS_CONTEXT_PATTERNS)
    catalysis_early_count = count_patterns(early_text, CATALYSIS_CONTEXT_PATTERNS)
    generic_env_ml_count = count_patterns(title_abs, GENERIC_ENV_ML_EXCLUSION_PATTERNS)
    method_count = count_patterns(text, METHOD_PATTERNS)
    exp_count = count_patterns(text, EXPERIMENT_PATTERNS)
    evidence = best_evidence(text, METHOD_PATTERNS)
    catalysis_evidence = best_evidence(early_text, CATALYSIS_CONTEXT_PATTERNS) or best_evidence(text, CATALYSIS_CONTEXT_PATTERNS)
    direction = classify_direction(text)
    if re.search(r"machine learning|deep learning|graph neural|random forest|XGBoost|QSAR", title_abs, flags=re.I):
        direction = "ml_prediction"
    elif re.search(r"high-throughput|computational screening", title_abs, flags=re.I):
        direction = "catalyst_screening"
    elif re.search(r"\bDFT\b|density functional theory|first-principles|ab initio", title_abs, flags=re.I):
        direction = "mechanism_dft"
    method_primary = 0
    if method_count >= 2:
        method_primary = 1
    if re.search(r"\b(DFT|machin[eg] learning|density functional theory|deep learning|molecular dynamics)\b", row["title"] or "", flags=re.I):
        method_primary = 1
    if stage == "fulltext" and full:
        method_section = "\n".join(s for s in sentences(full, limit=250) if re.search(r"method|comput|DFT|machine learning|simulation|model", s, flags=re.I))
        if count_patterns(method_section, METHOD_PATTERNS) >= 2:
            method_primary = 1
            evidence = evidence or best_evidence(method_section, METHOD_PATTERNS)
    title = row["title"] or ""
    strong_method_title = bool(
        re.search(
            r"\b(density functional theory study|first-principles calculation study|computational study|computational screening|high-throughput|combined density functional theory and machine learning study|machine learning[- ]based prediction|machine learning approach)\b",
            title,
            flags=re.I,
        )
    )
    supporting_method_title = bool(
        re.search(
            r"\b(experimental study and analysis using|synthesis.*(?:DFT|density functional theory|computational simulations?)|characteri[sz]ation.*(?:DFT|density functional theory|computational simulations?)|DFT analysis and application|insights? into .* from density functional theory|machine learning[- ]assisted modeling)\b",
            title,
            flags=re.I,
        )
    )
    if exp_count > max(6, method_count * 3) and not re.search(r"\b(DFT|machin[eg] learning|deep learning|theoretical)\b", title, flags=re.I):
        method_primary = 0 if require_method_primary else method_primary
    if stage == "fulltext" and supporting_method_title and exp_count > method_count:
        method_primary = 0 if require_method_primary else method_primary
    if stage == "fulltext" and exp_count > max(8, method_count * 1.5) and not strong_method_title:
        method_primary = 0 if require_method_primary else method_primary
    is_review = bool(re.search(r"\b(review|minireview|perspective)\b", early_text[:5000], flags=re.I))
    method_in_title = bool(re.search(r"\b(DFT|density functional theory|machine[- ]learn(?:ing|ed)|deep learning|molecular dynamics|computational|high-throughput|QSAR|microkinetic|ab initio|first-principles)\b", row["title"] or "", flags=re.I))
    if is_review and not method_in_title:
        method_primary = 0 if require_method_primary else method_primary
    explicit_method_topic = method_in_title or strong_method_title or bool(
        re.search(r"\b(?:DFT|density functional theory|machine[- ]learn(?:ing|ed)|computational|microkinetic|ab initio|first-principles)[- ]?guided\b|\bguided by (?:DFT|density functional theory|machine learning|computational)", title_abs, flags=re.I)
    )
    if stage == "fulltext" and full and not explicit_method_topic and exp_count >= max(4, method_count * 0.6):
        method_primary = 0 if require_method_primary else method_primary
    weak_dft_title = bool(re.search(r"\band DFT calculation\b|\bwith DFT calculation\b", row["title"] or "", flags=re.I))
    strong_dft_evidence = count_patterns(
        full[:80000],
        [r"DFT calculations? (?:reveal|demonstrate|show|confirm|indicate|suggest|were used|was used)", r"density functional theory calculations? (?:reveal|demonstrate|show|confirm|indicate|suggest|were used|was used)"],
    )
    if weak_dft_title and strong_dft_evidence < 2:
        method_primary = 0 if require_method_primary else method_primary
    score = float(domain_count * 2 + catalysis_count * 3 + method_count * 3 + min(int(row["citation_count"] or 0), 100) / 25)

    if domain_count == 0 or (stage == "fulltext" and domain_early_count == 0):
        return "excluded", direction, method_primary, evidence or "No environmental catalysis/remediation evidence found.", score
    if energy_only_count and environmental_core_title_count == 0:
        return "excluded", direction, method_primary, evidence or "Energy-conversion catalysis title/topic without explicit environmental remediation, pollutant, water-treatment, air-pollution, AOP, NOx/SCR, or nitrate/nitrite-removal focus.", score
    if energy_only_count and environmental_core_count == 0:
        return "excluded", direction, method_primary, evidence or "Energy-conversion catalysis without pollutant remediation, water treatment, air-pollution, AOP, NOx/SCR, or nitrate/nitrite-removal context.", score
    if stage == "fulltext" and energy_only_count and environmental_core_early_count == 0:
        return "excluded", direction, method_primary, evidence or "Energy-conversion catalysis appears outside the configured environmental-catalysis scope.", score
    if catalysis_count == 0 or (stage == "fulltext" and catalysis_early_count == 0):
        return "excluded", direction, method_primary, evidence or "No catalytic/AOP/photocatalytic/electrocatalytic context found.", score
    if generic_env_ml_count and catalysis_early_count == 0:
        return "excluded", direction, method_primary, evidence or "Generic environmental prediction/classification work without catalytic context.", score
    if method_count == 0:
        return "excluded", direction, method_primary, evidence or "No theory/simulation/machine-learning method evidence found.", score
    if any(re.search(p, row["title"] or "", flags=re.I) for p in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction/erratum/editorial record, not an original research paper.", score
    if stage == "fulltext" and is_review:
        return "peripheral", direction, 0 if require_method_primary else method_primary, evidence or "Review/perspective; not an original theory/ML-primary study.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence or catalysis_evidence, score
    if stage == "fulltext" and not row["full_text"]:
        return "pending_fulltext", direction, method_primary, evidence or catalysis_evidence, score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "Theory/ML appears secondary rather than primary.", score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and row["full_text"]:
        return "included", direction, method_primary, evidence, score
    fallback_status = "pending_fulltext" if row["pdf_path"] else "pending_pdf"
    return fallback_status, direction, method_primary, evidence, score


def literal_term_patterns(values: Any) -> List[str]:
    """Convert profile keyword lists to case-insensitive-safe literal regexes."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    patterns: List[str] = []
    for value in values:
        term = str(value or "").strip()
        if not term:
            continue
        escaped = re.escape(term).replace(r"\ ", r"\s+")
        if term[0].isalnum() and term[-1].isalnum():
            escaped = rf"(?<!\w){escaped}(?!\w)"
        patterns.append(escaped)
    return patterns


def classify_profile_direction(text: str, profile: Dict[str, Any]) -> str:
    configured = profile.get("direction_terms")
    if isinstance(configured, dict):
        scores = {
            str(label): count_patterns(text, literal_term_patterns(terms))
            for label, terms in configured.items()
        }
        scores = {label: score for label, score in scores.items() if score > 0}
        if scores:
            return max(scores.items(), key=lambda item: item[1])[0]
    return classify_direction(text)


def screen_generic_row(
    row: sqlite3.Row,
    stage: str,
    require_pdf: bool,
    require_method_primary: bool,
    profile: Dict[str, Any],
) -> Tuple[str, str, int, str, float]:
    """Profile-driven screening used by the public, domain-neutral workflow."""
    title = row["title"] or ""
    abstract = row["abstract"] or ""
    full = row["full_text"] or ""
    title_abs = f"{title}. {abstract}"
    text = title_abs if stage != "fulltext" else f"{title_abs}\n\n{full[:120000]}"

    topic_values = profile.get("screening_terms") or profile.get("search_terms") or []
    topic_patterns = literal_term_patterns(topic_values)
    method_patterns = literal_term_patterns(profile.get("method_terms") or [])
    exclude_patterns = literal_term_patterns(profile.get("exclude_terms") or [])
    topic_count = count_patterns(text, topic_patterns) if topic_patterns else 1
    method_count = count_patterns(text, method_patterns) if method_patterns else 1
    title_method_count = count_patterns(title, method_patterns) if method_patterns else 1
    direction = classify_profile_direction(text, profile)
    evidence_patterns = method_patterns or topic_patterns
    evidence = best_evidence(text, evidence_patterns) if evidence_patterns else ""
    method_primary = 1 if not method_patterns else int(title_method_count > 0 or method_count >= 2)

    if stage == "fulltext" and full and method_patterns:
        method_section = "\n".join(
            sentence
            for sentence in sentences(full, limit=250)
            if re.search(r"method|experiment|comput|model|analysis|approach", sentence, flags=re.I)
        )
        if count_patterns(method_section, method_patterns) >= 2:
            method_primary = 1
            evidence = evidence or best_evidence(method_section, method_patterns)

    score = float(topic_count * 3 + method_count * 2 + min(int(row["citation_count"] or 0), 100) / 25)
    if exclude_patterns and count_patterns(title_abs, exclude_patterns):
        return "excluded", direction, method_primary, evidence or "Matched an explicit exclusion term.", score
    if topic_patterns and topic_count == 0:
        return "excluded", direction, method_primary, evidence or "No configured topic term found.", score
    if method_patterns and method_count == 0:
        return "excluded", direction, 0, evidence or "No configured method term found.", score
    if any(re.search(pattern, title, flags=re.I) for pattern in BIBLIOGRAPHIC_EXCLUSION_PATTERNS):
        return "excluded", direction, 0, evidence or "Bibliographic correction, erratum, or editorial record.", score
    if profile_bool(profile, "original_research_only", False) and re.search(
        r"\b(review|minireview|perspective|commentary|editorial)\b", title_abs, flags=re.I
    ):
        return "peripheral", direction, 0 if require_method_primary else method_primary, evidence or "Non-original article type.", score
    if require_pdf and not row["pdf_path"]:
        return "pending_pdf", direction, method_primary, evidence, score
    if stage == "fulltext" and not full:
        return "pending_fulltext", direction, method_primary, evidence, score
    if require_method_primary and not method_primary:
        return "peripheral", direction, method_primary, evidence or "Configured method appears secondary.", score
    if stage == "fulltext" and require_pdf and row["pdf_path"] and full:
        return "included", direction, method_primary, evidence, score
    return ("pending_fulltext" if row["pdf_path"] else "pending_pdf"), direction, method_primary, evidence, score


def command_screen(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    require_pdf = args.require_pdf or bool(profile.get("require_pdf_for_inclusion", True))
    require_method_primary = args.require_method_primary or bool(profile.get("require_method_primary", True))
    year_from, year_to = profile_year_bounds(profile)
    conn = connect(pdir / "library.sqlite")
    try:
        rows = conn.execute("SELECT * FROM papers LIMIT ?", (args.limit,)).fetchall()
        counts: Dict[str, int] = {}
        for row in rows:
            if args.stage != "fulltext" and row["status"] == "included":
                counts["included_preserved"] = counts.get("included_preserved", 0) + 1
                continue
            status, direction, method_primary, evidence, score = screen_row_for_profile(row, args.stage, require_pdf, require_method_primary, profile)
            year = int(row["year"] or 0)
            if (year_from and year and year < year_from) or (year_to and year and year > year_to):
                status = "excluded"
                evidence = f"Outside configured year range {year_from or 'open'}-{year_to or 'open'}."
            if profile_bool(profile, "exclude_mdpi", False) and is_mdpi_record(row["doi"], row["journal"]):
                status = "excluded"
                method_primary = 0 if require_method_primary else method_primary
                evidence = "Excluded by profile: MDPI journals/DOI prefix 10.3390 are out of scope."
            if profile_bool(profile, "require_q1_journal", False) and not q1_journal_match(row["journal"], profile):
                status = "excluded"
                method_primary = 0 if require_method_primary else method_primary
                evidence = "Excluded by profile: journal is not in the configured Q1 allowlist."
            if profile_bool(profile, "strict_top_journal_whitelist", False) and not top_journal_match_for_profile(row["journal"], profile):
                status = "excluded"
                method_primary = 0 if require_method_primary else method_primary
                evidence = "Excluded by profile: journal is not in the configured top-journal allowlist."
            if normalize_doi(row["doi"]) in profile_excluded_dois(profile):
                status = "excluded"
                method_primary = 0 if require_method_primary else method_primary
                evidence = "Excluded by profile after manual topic audit."
            if profile_bool(profile, "consolidate_directions", False):
                direction = consolidate_direction(
                    direction,
                    f"{row['title'] or ''}. {row['abstract'] or ''}\n\n{(row['full_text'] or '')[:120000]}",
                )
            if priority_journal_match(row["journal"], profile):
                score += 10.0
            counts[status] = counts.get(status, 0) + 1
            conn.execute(
                "UPDATE papers SET status=?, direction=?, method_primary=?, method_evidence=?, relevance_score=?, updated_at=? WHERE id=?",
                (status, direction, method_primary, evidence, score, utc_now(), row["id"]),
            )
        conn.commit()
        write_missing_pdf(conn, pdir, profile)
        write_papers_csv(conn, pdir, profile)
        record_run(conn, "screen", {"stage": args.stage, "counts": counts})
    finally:
        conn.close()
    log(f"screen complete: {counts}")


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [{k: row[k] for k in row.keys()} for row in rows]


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    try:
        f = target.open("w", newline="", encoding="utf-8-sig")
    except PermissionError:
        target = path.with_name(f"{path.stem}.latest{path.suffix}")
        try:
            f = target.open("w", newline="", encoding="utf-8-sig")
        except PermissionError:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            target = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
            f = target.open("w", newline="", encoding="utf-8-sig")
        log(f"{path.name} is locked; wrote {target.name} instead")
    with f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def add_priority_flags(rows: List[Dict[str, Any]], profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not profile:
        return rows
    for row in rows:
        matched = priority_journal_match(row.get("journal"), profile)
        row["priority_journal"] = "yes" if matched else "no"
        row["priority_journal_match"] = matched
        q1_matched = q1_journal_match(row.get("journal"), profile)
        row["q1_journal"] = "yes" if q1_matched else "no"
        row["q1_journal_match"] = q1_matched
        row["excluded_publisher"] = "MDPI" if is_mdpi_record(row.get("doi"), row.get("journal")) else ""
    return rows


def write_papers_csv(conn: sqlite3.Connection, pdir: Path, profile: Optional[Dict[str, Any]] = None) -> None:
    rows = rows_to_dicts(conn.execute("SELECT * FROM papers ORDER BY status, direction, year DESC, citation_count DESC").fetchall())
    rows = add_priority_flags(rows, profile)
    fields = ["status", "direction", "priority_journal", "priority_journal_match", "q1_journal", "q1_journal_match", "excluded_publisher", "method_primary", "title", "year", "journal", "doi", "citation_count", "sources", "pdf_path", "pdf_method", "method_evidence"]
    write_csv(pdir / "exports" / "papers.csv", rows, fields)


def write_missing_pdf(conn: sqlite3.Connection, pdir: Path, profile: Optional[Dict[str, Any]] = None) -> None:
    rows = rows_to_dicts(
        conn.execute(
            "SELECT * FROM papers WHERE status='pending_pdf' AND doi IS NOT NULL ORDER BY relevance_score DESC, citation_count DESC"
        ).fetchall()
    )
    rows = add_priority_flags(rows, profile)
    fields = ["priority_journal", "priority_journal_match", "q1_journal", "q1_journal_match", "excluded_publisher", "title", "year", "journal", "doi", "citation_count", "sources", "status", "method_evidence", "landing_url", "pdf_url"]
    write_csv(pdir / "exports" / "missing_pdf_candidates.csv", rows, fields)


def write_pdf_manifest(conn: sqlite3.Connection, pdir: Path, profile: Optional[Dict[str, Any]] = None) -> None:
    rows = rows_to_dicts(conn.execute("SELECT status, direction, title, year, journal, doi, pdf_path, pdf_method, pdf_status, pdf_pages, sources FROM papers WHERE pdf_path IS NOT NULL AND pdf_path!='' ORDER BY status, direction, year DESC").fetchall())
    rows = add_priority_flags(rows, profile)
    fields = ["status", "direction", "priority_journal", "priority_journal_match", "q1_journal", "q1_journal_match", "excluded_publisher", "title", "year", "journal", "doi", "pdf_path", "pdf_method", "pdf_status", "pdf_pages", "sources"]
    write_csv(pdir / "exports" / "pdf_manifest.csv", rows, fields)
    (pdir / "exports" / "pdf_manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    included_rows = [row for row in rows if row.get("status") == "included"]
    write_csv(pdir / "exports" / "included_pdf_manifest.csv", included_rows, fields)
    (pdir / "exports" / "included_pdf_manifest.json").write_text(json.dumps(included_rows, ensure_ascii=False, indent=2), encoding="utf-8")


def publisher_bucket(doi: str) -> str:
    nd = normalize_doi(doi)
    if nd.startswith("10.1021/"):
        return "ACS"
    if nd.startswith("10.1016/"):
        return "Elsevier"
    if nd.startswith("10.1002/"):
        return "Wiley"
    if nd.startswith("10.1080/"):
        return "TaylorFrancis"
    return nd.split("/", 1)[0] if "/" in nd else nd


def write_pdf_gap_analysis(conn: sqlite3.Connection, pdir: Path, profile: Dict[str, Any], limit: int = 100000) -> None:
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT doi, title, year, journal, direction, citation_count, relevance_score, pdf_url, landing_url
            FROM papers
            WHERE status='pending_pdf' AND doi IS NOT NULL
            ORDER BY relevance_score DESC, citation_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )
    rows = add_priority_flags(rows, profile)

    def analyze_gap(row: Dict[str, Any]) -> Dict[str, Any]:
        doi = row.get("doi") or ""
        metadata = unpaywall_metadata(doi)
        pdf_urls: List[str] = []
        repo_urls: List[str] = []
        for loc in [metadata.get("best_oa_location")] + list(metadata.get("oa_locations") or []):
            if not loc:
                continue
            url = loc.get("url_for_pdf") or loc.get("url") or loc.get("url_for_landing_page")
            if not url:
                continue
            if loc.get("url_for_pdf") and loc.get("url_for_pdf") not in pdf_urls:
                pdf_urls.append(loc.get("url_for_pdf"))
            if loc.get("host_type") == "repository" and url not in repo_urls:
                repo_urls.append(url)
        hal_urls = hal_pdf_urls(doi)
        semantic_urls = [u for u in semantic_scholar_pdf_urls(doi) if u]
        crossref_urls = crossref_pdf_urls(doi)
        pmcids, epmc_urls = europe_pmc_locations(doi)
        pub = publisher_bucket(doi)
        if hal_urls or pmcids or epmc_urls:
            next_action = "retry legal repository download"
        elif metadata.get("is_oa") and pub == "Elsevier":
            next_action = "publisher OA but ScienceDirect/Cell blocks noninteractive PDF; use browser/WebVPN/manual import"
        elif pub in {"ACS", "Wiley", "TaylorFrancis"}:
            next_action = "publisher/institutional access or manual browser import likely required"
        elif metadata.get("is_oa"):
            next_action = "OA metadata exists; retry publisher/browser download"
        else:
            next_action = "closed/no repository copy found; institutional access/manual import required"
        return {
            **row,
            "publisher_bucket": pub,
            "q1_journal": "yes" if q1_journal_match(row.get("journal"), profile) else "no",
            "q1_journal_match": q1_journal_match(row.get("journal"), profile),
            "excluded_publisher": "MDPI" if is_mdpi_record(row.get("doi"), row.get("journal")) else "",
            "is_oa": metadata.get("is_oa", ""),
            "oa_status": metadata.get("oa_status", ""),
            "has_repository_copy": metadata.get("has_repository_copy", ""),
            "unpaywall_pdf_urls": " | ".join(pdf_urls),
            "unpaywall_repository_urls": " | ".join(repo_urls),
            "hal_urls": " | ".join(hal_urls),
            "semantic_scholar_urls": " | ".join(semantic_urls),
            "crossref_urls": " | ".join(crossref_urls),
            "europe_pmc_or_pmc": " | ".join(list(pmcids) + list(epmc_urls)),
            "next_action": next_action,
        }

    output: List[Dict[str, Any]] = [dict() for _ in rows]
    max_workers = min(8, max(1, len(rows)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(analyze_gap, row): idx for idx, row in enumerate(rows)}
        for future in as_completed(futures):
            output[futures[future]] = future.result()
    fields = [
        "doi",
        "title",
        "year",
        "journal",
        "direction",
        "priority_journal",
        "priority_journal_match",
        "q1_journal",
        "q1_journal_match",
        "excluded_publisher",
        "publisher_bucket",
        "citation_count",
        "relevance_score",
        "pdf_url",
        "is_oa",
        "oa_status",
        "has_repository_copy",
        "unpaywall_pdf_urls",
        "unpaywall_repository_urls",
        "hal_urls",
        "semantic_scholar_urls",
        "crossref_urls",
        "europe_pmc_or_pmc",
        "next_action",
    ]
    write_csv(pdir / "exports" / "pdf_gap_analysis.csv", output, fields)


def write_direction_representatives(conn: sqlite3.Connection, pdir: Path, profile: Dict[str, Any]) -> None:
    rep_limit = representative_limit(profile, 20)
    include_candidates = profile_bool(profile, "representatives_include_candidates", True)
    status_filter = "status IN ('included','pending_pdf','peripheral')" if include_candidates else "status='included'"
    rows = conn.execute(
        f"""
        SELECT * FROM papers
        WHERE {status_filter}
        ORDER BY direction, relevance_score DESC, citation_count DESC, year DESC
        """
    ).fetchall()
    by_dir: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        by_dir.setdefault(row["direction"] or "other", []).append(row)
    output: List[Dict[str, Any]] = []
    for direction, items in sorted(by_dir.items()):
        sorted_items = sorted(
            items,
            key=lambda r: (
                1 if r["status"] == "included" else 0,
                1 if priority_journal_match(r["journal"], profile) else 0,
                float(r["relevance_score"] or 0),
                int(r["citation_count"] or 0),
                int(r["year"] or 0),
            ),
            reverse=True,
        )
        for rank, row in enumerate(sorted_items[:rep_limit], 1):
            priority_match = priority_journal_match(row["journal"], profile)
            output.append(
                {
                    "direction": direction,
                    "rank": rank,
                    "status": row["status"],
                    "pdf_state": "verified" if row["pdf_path"] else "missing",
                    "priority_journal": "yes" if priority_match else "no",
                    "priority_journal_match": priority_match,
                    "q1_journal": "yes" if q1_journal_match(row["journal"], profile) else "no",
                    "q1_journal_match": q1_journal_match(row["journal"], profile),
                    "excluded_publisher": "MDPI" if is_mdpi_record(row["doi"], row["journal"]) else "",
                    "method_primary": row["method_primary"],
                    "title": row["title"],
                    "year": row["year"],
                    "journal": row["journal"],
                    "doi": row["doi"],
                    "citation_count": row["citation_count"],
                    "relevance_score": row["relevance_score"],
                    "pdf_path": row["pdf_path"],
                    "pdf_method": row["pdf_method"],
                    "method_evidence": row["method_evidence"],
                }
            )
    fields = [
        "direction",
        "rank",
        "status",
        "pdf_state",
        "priority_journal",
        "priority_journal_match",
        "q1_journal",
        "q1_journal_match",
        "excluded_publisher",
        "method_primary",
        "title",
        "year",
        "journal",
        "doi",
        "citation_count",
        "relevance_score",
        "pdf_path",
        "pdf_method",
        "method_evidence",
    ]
    write_csv(pdir / "exports" / "direction_representatives.csv", output, fields)


def write_priority_pdf_queue(conn: sqlite3.Connection, pdir: Path, profile: Dict[str, Any]) -> None:
    rows = conn.execute(
        """
        SELECT * FROM papers
        WHERE status='pending_pdf'
        ORDER BY relevance_score DESC, citation_count DESC, year DESC
        """
    ).fetchall()
    output: List[Dict[str, Any]] = []
    for row in rows:
        priority_match = priority_journal_match(row["journal"], profile)
        doi_url = f"https://doi.org/{row['doi']}" if row["doi"] else ""
        output.append(
            {
                "priority_sort": 1 if priority_match else 0,
                "priority_journal": "yes" if priority_match else "no",
                "priority_journal_match": priority_match,
                "direction": row["direction"],
                "status": row["status"],
                "title": row["title"],
                "year": row["year"],
                "journal": row["journal"],
                "doi": row["doi"],
                "doi_url": doi_url,
                "pdf_url": row["pdf_url"] or "",
                "landing_url": row["landing_url"] or doi_url,
                "citation_count": row["citation_count"],
                "relevance_score": row["relevance_score"],
                "method_evidence": row["method_evidence"] or "",
            }
        )
    output.sort(
        key=lambda r: (
            int(r["priority_sort"]),
            float(r["relevance_score"] or 0),
            int(r["citation_count"] or 0),
            int(r["year"] or 0),
        ),
        reverse=True,
    )
    for idx, row in enumerate(output, 1):
        row["download_rank"] = idx
        row.pop("priority_sort", None)
    fields = [
        "download_rank",
        "priority_journal",
        "priority_journal_match",
        "direction",
        "status",
        "title",
        "year",
        "journal",
        "doi",
        "doi_url",
        "pdf_url",
        "landing_url",
        "citation_count",
        "relevance_score",
        "method_evidence",
    ]
    write_csv(pdir / "exports" / "priority_pdf_queue.csv", output, fields)


def bibtex_key(row: sqlite3.Row) -> str:
    authors = json_list(row["authors"])
    first = "paper"
    if authors:
        first = re.sub(r"[^A-Za-z]+", "", str(authors[0]).split()[-1]).lower() or "paper"
    year = row["year"] or "nd"
    suffix = hashlib.sha1((row["doi"] or row["title"] or row["id"]).encode("utf-8")).hexdigest()[:6]
    return f"{first}{year}{suffix}"


def make_bibtex(rows: Sequence[sqlite3.Row]) -> str:
    entries = []
    for row in rows:
        authors = " and ".join(json_list(row["authors"]))
        fields = {
            "title": row["title"],
            "author": authors,
            "journal": row["journal"],
            "year": row["year"],
            "doi": row["doi"],
        }
        lines = [f"@article{{{bibtex_key(row)},"]
        for key, val in fields.items():
            if val:
                safe = str(val).replace("{", "").replace("}", "")
                lines.append(f"  {key} = {{{safe}}},")
        lines.append("}")
        entries.append("\n".join(lines))
    return "\n\n".join(entries)


def command_export(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    try:
        rows = conn.execute("SELECT * FROM papers ORDER BY status, direction, year DESC, citation_count DESC").fetchall()
        formats = [x.strip().lower() for x in args.format.split(",") if x.strip()]
        if "csv" in formats:
            write_papers_csv(conn, pdir, profile)
        if "bibtex" in formats:
            included = conn.execute("SELECT * FROM papers WHERE status='included' ORDER BY year DESC, citation_count DESC").fetchall()
            (pdir / "exports" / "included_papers.bib").write_text(make_bibtex(included), encoding="utf-8")
        if "markdown" in formats:
            write_report(conn, pdir, profile)
        if args.include_pdf_manifest:
            write_pdf_manifest(conn, pdir, profile)
            write_missing_pdf(conn, pdir, profile)
            write_direction_representatives(conn, pdir, profile)
            write_priority_pdf_queue(conn, pdir, profile)
        record_run(conn, "export", {"formats": formats})
    finally:
        conn.close()
    log(f"exported to {pdir / 'exports'}")


def command_audit_pdfs(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    try:
        write_pdf_gap_analysis(conn, pdir, profile, args.limit)
        record_run(conn, "audit-pdfs", {"limit": args.limit, "output": str(pdir / "exports" / "pdf_gap_analysis.csv")})
    finally:
        conn.close()
    log(f"PDF gap analysis written: {pdir / 'exports' / 'pdf_gap_analysis.csv'}")


def command_audit_search(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    coverage_path = pdir / "exports" / "query_coverage_report.csv"
    if not coverage_path.exists():
        raise FileNotFoundError(f"query coverage report not found: {coverage_path}")
    plan_path = pdir / "exports" / "search_plan_summary.json"
    plan_summary: Dict[str, Any] = {}
    if plan_path.exists():
        try:
            plan_summary = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except Exception:
            plan_summary = {}
    with coverage_path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    def as_int(value: Any) -> int:
        try:
            text = str(value or "").strip()
            if not text:
                return 0
            return int(float(text))
        except Exception:
            return 0

    by_source: Dict[str, Dict[str, int]] = {}
    flagged: List[Dict[str, Any]] = []
    for row in rows:
        source = row.get("source") or "unknown"
        stats = by_source.setdefault(source, {"rows": 0, "fetched": 0, "truncated": 0})
        stats["rows"] += 1
        stats["fetched"] += as_int(row.get("fetched_records"))
        is_truncated = str(row.get("truncated") or "").strip().lower() in {"yes", "true", "1"}
        if is_truncated:
            stats["truncated"] += 1
            total = as_int(row.get("total_available"))
            fetched = as_int(row.get("fetched_records"))
            requested = as_int(row.get("requested_limit"))
            gap = max(0, total - fetched) if total else max(0, requested - fetched)
            flagged.append({**row, "estimated_gap": gap})

    flagged.sort(key=lambda r: as_int(r.get("estimated_gap")), reverse=True)
    gap_fields = [
        "query_index",
        "chunk_index",
        "query",
        "year_from",
        "year_to",
        "source",
        "requested_limit",
        "fetched_records",
        "total_available",
        "pages_fetched",
        "truncated",
        "estimated_gap",
    ]
    write_csv(pdir / "exports" / "search_coverage_gaps.csv", flagged, gap_fields)

    english = profile_str(profile, "output_language", "en").lower().startswith("en")
    lines = (
        [
            "# Search coverage audit",
            "",
            f"Project: {profile.get('project') or pdir.name}",
            f"Coverage report: {coverage_path}",
            f"Plan summary: {plan_path if plan_path.exists() else 'missing'}",
            f"Audit time (UTC): {utc_now()}",
            "",
            "## Conclusion",
            "",
        ]
        if english
        else [
            "# 检索覆盖度审计",
            "",
            f"项目：{profile.get('project') or pdir.name}",
            f"覆盖报告：{coverage_path}",
            f"计划摘要：{plan_path if plan_path.exists() else '缺失'}",
            f"审计时间（UTC）：{utc_now()}",
            "",
            "## 结论",
            "",
        ]
    )
    query_plan_truncated = bool(plan_summary.get("query_plan_truncated"))
    global_cap_reached = bool(plan_summary.get("global_cap_reached"))
    expected_tasks = as_int(plan_summary.get("expected_source_tasks"))
    completed_tasks = as_int(plan_summary.get("completed_source_tasks"))
    incomplete_tasks = bool(expected_tasks and completed_tasks < expected_tasks)
    if not rows:
        lines.append("No coverage records were found; completeness cannot be assessed." if english else "未找到任何检索覆盖记录，不能证明检索完整。")
    elif flagged or query_plan_truncated or global_cap_reached or incomplete_tasks:
        reasons = []
        if flagged:
            reasons.append(
                f"{len(flagged)} query/source/year partitions were truncated or reached a retrieval limit"
                if english
                else f"{len(flagged)} 个 query/source/year 分片触发截断或达到取回上限"
            )
        if query_plan_truncated:
            reasons.append("the query plan did not cover the full topic × method matrix" if english else "查询计划未覆盖完整 topic × method 组合")
        if global_cap_reached:
            reasons.append("the global record cap stopped the search early" if english else "全局记录上限提前终止了检索")
        if incomplete_tasks:
            reasons.append(
                f"only {completed_tasks}/{expected_tasks} source tasks completed"
                if english
                else f"只完成 {completed_tasks}/{expected_tasks} 个 source 检索任务"
            )
        if english:
            lines.append("Do not claim complete coverage: " + "; ".join(reasons) + ".")
            lines.append("Raise per-query/page limits, use smaller year partitions, or add more specific `custom_queries` before reassessing coverage.")
        else:
            lines.append("当前不能宣称检索已完整；原因：" + "；".join(reasons) + "。")
            lines.append("建议提高 `search_max_per_query`、`search_source_max_pages`，启用或减小年份分片，或为这些宽泛查询补充更具体的 `custom_queries`。")
    else:
        if english:
            lines.append("No truncated partition was found in the current report. This supports coverage only within the configured sources, queries, and date range.")
            lines.append("It does not prove that no relevant paper exists outside those boundaries.")
        else:
            lines.append("当前覆盖报告中未发现截断分片；在既定数据源、检索式和年份范围内，可以作为检索完整性的证据。")
            lines.append("注意：这仍不等于数据库之外不存在相关论文；完整性边界由 profile 中的数据源、检索词、年份和期刊/主题范围定义。")
    if plan_summary:
        lines += (
            [
                "",
                "## Search plan",
                "",
                f"- Depth: {plan_summary.get('depth')}",
                f"- Query strategy: {plan_summary.get('query_strategy')}",
                f"- Queries: {plan_summary.get('queries')}; full topic × method matrix: {plan_summary.get('full_pairwise_queries')}",
                f"- Year chunks: {plan_summary.get('year_chunks')}; years per chunk: {plan_summary.get('year_chunk_size')}",
                f"- Global cap: {plan_summary.get('max_records')}; per-query/source cap: {plan_summary.get('max_per_query')}; page cap: {plan_summary.get('source_max_pages')}",
                f"- Completed source tasks: {completed_tasks}/{expected_tasks or completed_tasks}",
            ]
            if english
            else [
                "",
                "## 检索计划",
                "",
                f"- 搜索深度：{plan_summary.get('depth')}",
                f"- 查询策略：{plan_summary.get('query_strategy')}",
                f"- 查询数：{plan_summary.get('queries')}；完整 topic × method 组合数：{plan_summary.get('full_pairwise_queries')}",
                f"- 年份分片：{plan_summary.get('year_chunks')}；每片年份数：{plan_summary.get('year_chunk_size')}",
                f"- 全局记录上限：{plan_summary.get('max_records')}；每 query/source 上限：{plan_summary.get('max_per_query')}；每 query/source 最大页数：{plan_summary.get('source_max_pages')}",
                f"- source 任务完成度：{completed_tasks}/{expected_tasks or completed_tasks}",
            ]
        )
    lines += ["", "## Statistics by source" if english else "## 按数据源统计", ""]
    for source, stats in sorted(by_source.items()):
        lines.append(
            f"- {source}: partitions={stats['rows']}; fetched={stats['fetched']}; truncated={stats['truncated']}."
            if english
            else f"- {source}：分片 {stats['rows']} 个；取回记录 {stats['fetched']} 条；截断分片 {stats['truncated']} 个。"
        )
    if flagged:
        lines += ["", "## Most severely truncated partitions" if english else "## 截断最严重的分片", ""]
        for row in flagged[: args.top]:
            year_range = f"{row.get('year_from') or 'open'}-{row.get('year_to') or 'open'}"
            if english:
                lines.append(
                    f"- `{row.get('source')}` {year_range}: fetched {row.get('fetched_records')} / "
                    f"total {row.get('total_available') or 'unknown'}; query=`{row.get('query')}`"
                )
            else:
                lines.append(
                    f"- `{row.get('source')}` {year_range}：取回 {row.get('fetched_records')} / "
                    f"总量 {row.get('total_available') or '未知'}；query=`{row.get('query')}`"
                )
    path = pdir / "exports" / "search_coverage_audit.md"
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    conn = connect(pdir / "library.sqlite")
    try:
        record_run(conn, "audit-search", {"coverage": str(coverage_path), "flagged": len(flagged), "output": str(path)})
    finally:
        conn.close()
    log(f"search coverage audit written: {path}")


def write_generic_report(conn: sqlite3.Connection, pdir: Path, profile: Dict[str, Any]) -> Path:
    rows = conn.execute(
        "SELECT * FROM papers WHERE status IN ('included','pending_pdf','pending_fulltext','peripheral') "
        "ORDER BY direction, status='included' DESC, relevance_score DESC, year DESC, citation_count DESC"
    ).fetchall()
    counts = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM papers GROUP BY status").fetchall()
    }
    include_candidates = profile_bool(profile, "representatives_include_candidates", False)
    rep_limit = representative_limit(profile, 20)
    lines = [
        f"# Literature research report: {profile.get('project') or pdir.name}",
        "",
        f"Topic: {profile.get('topic') or ''}",
        f"Generated (UTC): {utc_now()}",
        "",
        "## Evidence status",
        "",
        f"- Final PDF/full-text verified (`included`): {counts.get('included', 0)}",
        f"- Awaiting PDF (`pending_pdf`): {counts.get('pending_pdf', 0)}",
        f"- Awaiting full-text screening (`pending_fulltext`): {counts.get('pending_fulltext', 0)}",
        f"- Relevant but peripheral (`peripheral`): {counts.get('peripheral', 0)}",
        f"- Excluded: {counts.get('excluded', 0)}",
        "",
        "Only `included` records belong to the final evidence set. Automated ranking and screening require expert review.",
        "",
    ]
    by_direction: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        if row["status"] == "included" or include_candidates:
            by_direction.setdefault(row["direction"] or "other", []).append(row)
    if not by_direction:
        lines += ["No full-text-verified papers are available yet.", ""]
    for direction, direction_rows in sorted(by_direction.items()):
        included_rows = [row for row in direction_rows if row["status"] == "included"]
        unresolved_rows = [row for row in direction_rows if row["status"] != "included"]
        lines += [
            f"## {direction}",
            "",
            f"Final PDF/full-text verified papers: {len(included_rows)}",
            "",
        ]
        for row in included_rows[:rep_limit]:
            doi = f"https://doi.org/{row['doi']}" if row["doi"] else "DOI unavailable"
            lines.append(
                f"- [included; PDF verified] {row['title']} "
                f"({row['year'] or 'year unavailable'}, {row['journal'] or 'journal unavailable'}). {doi}"
            )
            if row["method_evidence"]:
                lines.append(f"  Evidence: {row['method_evidence']}")
        if not included_rows:
            lines.append("- No verified paper in this direction.")
        if include_candidates:
            lines += ["", "Unresolved candidates (not part of the final evidence set):", ""]
            for row in unresolved_rows[:rep_limit]:
                doi = f"https://doi.org/{row['doi']}" if row["doi"] else "DOI unavailable"
                lines.append(
                    f"- [{row['status']}] {row['title']} "
                    f"({row['year'] or 'year unavailable'}, {row['journal'] or 'journal unavailable'}). {doi}"
                )
            if not unresolved_rows:
                lines.append("- None.")
        lines.append("")
    path = pdir / "exports" / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_report(conn: sqlite3.Connection, pdir: Path, profile: Dict[str, Any]) -> Path:
    if profile_str(profile, "output_language", "en").lower().startswith("en"):
        return write_generic_report(conn, pdir, profile)
    included = conn.execute("SELECT * FROM papers WHERE status='included' ORDER BY direction, relevance_score DESC, year DESC, citation_count DESC").fetchall()
    include_candidate_reps = profile_bool(profile, "representatives_include_candidates", True)
    representative_status_filter = "status IN ('included','pending_pdf','peripheral')" if include_candidate_reps else "status='included'"
    representative_rows = conn.execute(
        f"SELECT * FROM papers WHERE {representative_status_filter} ORDER BY direction, relevance_score DESC, year DESC, citation_count DESC"
    ).fetchall()
    pending_by_direction = {
        row["direction"] or "other": row["n"]
        for row in conn.execute("SELECT direction, COUNT(*) AS n FROM papers WHERE status='pending_pdf' GROUP BY direction").fetchall()
    }
    peripheral_by_direction = {
        row["direction"] or "other": row["n"]
        for row in conn.execute("SELECT direction, COUNT(*) AS n FROM papers WHERE status='peripheral' GROUP BY direction").fetchall()
    }
    pending_pdf = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE status='pending_pdf'").fetchone()["n"]
    missing = pending_pdf
    priority_count = conn.execute("SELECT journal FROM papers WHERE status='included'").fetchall()
    priority_count_n = sum(1 for row in priority_count if priority_journal_match(row["journal"], profile))
    q1_count_n = sum(1 for row in priority_count if q1_journal_match(row["journal"], profile))
    rep_limit = representative_limit(profile, 20)
    lines = [
        f"# 文献调研报告：{profile.get('project') or pdir.name}",
        "",
        f"主题：{profile.get('topic') or ''}",
        "",
        f"生成时间（UTC）：{utc_now()}",
        "",
        f"最终纳入的全文PDF验证文献：{len(included)} 篇",
        f"待获取PDF候选文献：{pending_pdf} 篇",
        f"缺失PDF记录：{missing} 篇",
        f"Q1白名单内最终纳入文献：{q1_count_n} 篇",
        f"优先期刊最终纳入文献：{priority_count_n} 篇",
        (
            f"每个方向列出代表候选：最多 {rep_limit} 篇；不足时按实际数量列出"
            if include_candidate_reps
            else f"每个方向列出PDF全文验证代表文献：最多 {rep_limit} 篇；不足时按实际数量列出"
        ),
        "最终纳入文献必须已完成本地PDF与全文证据验证；待获取PDF或方法非主导候选仅单独展示，不计入最终纳入集合。",
        (
            ""
            if include_candidate_reps
            else "未解决PDF或方法非主导候选不进入方向代表文献计数；详见 priority_pdf_queue.csv、missing_pdf_candidates.csv 和 pdf_gap_analysis.csv。"
        ),
        "",
    ]
    by_dir: Dict[str, List[sqlite3.Row]] = {}
    for row in representative_rows:
        by_dir.setdefault(row["direction"] or "other", []).append(row)
    if not by_dir:
        lines += ["目前尚无最终纳入文献。请先运行PDF获取、全文解析和全文筛选流程。", ""]
    for direction, rows in sorted(by_dir.items()):
        note = DIRECTION_NOTES.get(direction, DIRECTION_NOTES["other"])
        display_direction = DIRECTION_LABELS.get(direction, direction)
        included_rows = [r for r in rows if r["status"] == "included"]
        candidate_rows = [r for r in rows if r["status"] != "included"]
        priority_rows = [r for r in included_rows if priority_journal_match(r["journal"], profile)]
        priority_candidate_rows = [r for r in rows if priority_journal_match(r["journal"], profile)]
        actual_listed = min(len(rows), rep_limit)
        included_listed = min(len(included_rows), rep_limit)
        lines += [
            f"## {display_direction}",
            "",
            f"本方向PDF验证纳入数量：{len(included_rows)} 篇",
            f"本方向列出的PDF验证文献：{included_listed} 篇",
            (
                f"本方向列出的代表候选：{actual_listed} 篇"
                if include_candidate_reps
                else f"本方向列出的代表文献：{included_listed} 篇"
            ),
            f"本方向待获取PDF候选：{pending_by_direction.get(direction, 0)} 篇",
            f"本方向方法非主导/外围候选：{peripheral_by_direction.get(direction, 0)} 篇",
            f"本方向优先期刊纳入数量：{len(priority_rows)} 篇",
            (
                f"本方向优先期刊候选/外围数量：{len(priority_candidate_rows)} 篇"
                if include_candidate_reps
                else "本方向优先期刊候选/外围数量已在独立表格中导出。"
            ),
            "",
            f"主要创新点：{note['innovation']}",
            f"科学研究价值：{note['value']}",
            f"实施难度：{note['difficulty']}",
            f"创新性强度：{note['novelty']}",
            f"出成果潜力：{note['potential']}",
            "",
            f"PDF全文验证代表文献（最多 {rep_limit} 篇，不足时按实际数量）：",
            "",
        ]
        sorted_rows = sorted(
            included_rows,
            key=lambda r: (
                1 if priority_journal_match(r["journal"], profile) else 0,
                float(r["relevance_score"] or 0),
                int(r["citation_count"] or 0),
                int(r["year"] or 0),
            ),
            reverse=True,
        )
        for row in sorted_rows[:rep_limit]:
            status = STATUS_LABELS.get(row["status"], row["status"])
            doi = f"https://doi.org/{row['doi']}" if row["doi"] else "DOI缺失"
            priority = journal_scope_label(row["journal"], profile)
            pdf_state = "PDF已验证" if row["pdf_path"] else "PDF缺失"
            lines.append(f"- [{status}; {pdf_state}; {priority}] {row['title']} ({row['year'] or '年份未知'}, {row['journal'] or '期刊未知'}). {doi}")
            if row["method_evidence"]:
                lines.append(f"  方法主导性原文证据：{row['method_evidence']}")
        if not sorted_rows:
            lines.append("- 本方向暂无PDF全文验证文献。")
        if include_candidate_reps:
            lines += [
                "",
                f"仍需PDF或全文复核的代表候选（最多 {rep_limit} 篇；不计入最终纳入）：",
                "",
            ]
            sorted_candidates = sorted(
                candidate_rows,
                key=lambda r: (
                    1 if priority_journal_match(r["journal"], profile) else 0,
                    float(r["relevance_score"] or 0),
                    int(r["citation_count"] or 0),
                    int(r["year"] or 0),
                ),
                reverse=True,
            )
            for row in sorted_candidates[:rep_limit]:
                status = STATUS_LABELS.get(row["status"], row["status"])
                doi = f"https://doi.org/{row['doi']}" if row["doi"] else "DOI缺失"
                priority = journal_scope_label(row["journal"], profile)
                pdf_state = "PDF已验证但方法非主导/外围" if row["pdf_path"] else "PDF缺失"
                lines.append(f"- [{status}; {pdf_state}; {priority}] {row['title']} ({row['year'] or '年份未知'}, {row['journal'] or '期刊未知'}). {doi}")
                if row["method_evidence"]:
                    lines.append(f"  方法主导性原文证据：{row['method_evidence']}")
            if not sorted_candidates:
                lines.append("- 本方向暂无未解决候选。")
        lines.append("")
    path = pdir / "exports" / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    return path


def command_summarize(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    conn = connect(pdir / "library.sqlite")
    try:
        path = write_report(conn, pdir, profile)
        write_papers_csv(conn, pdir, profile)
        write_pdf_manifest(conn, pdir, profile)
        write_missing_pdf(conn, pdir, profile)
        write_direction_representatives(conn, pdir, profile)
        write_priority_pdf_queue(conn, pdir, profile)
        record_run(conn, "summarize", {"group_by": args.group_by, "report": str(path)})
    finally:
        conn.close()
    log(f"report written: {path}")


def command_ask(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    question_terms = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", args.question)]
    conn = connect(pdir / "library.sqlite")
    try:
        rows = conn.execute("SELECT * FROM papers WHERE full_text IS NOT NULL AND full_text!='' ORDER BY status='included' DESC, relevance_score DESC LIMIT 500").fetchall()
        scored: List[Tuple[int, sqlite3.Row, str]] = []
        for row in rows:
            text = f"{row['title'] or ''}\n{row['abstract'] or ''}\n{row['full_text'][:80000]}"
            lower = text.lower()
            score = sum(lower.count(t) for t in question_terms)
            evidence = ""
            if score:
                evidence = best_evidence(text, [re.escape(t) for t in question_terms[:12]])
            if score:
                scored.append((score, row, evidence))
        scored.sort(key=lambda x: x[0], reverse=True)
        english = profile_str(profile, "output_language", "en").lower().startswith("en")
        lines = [f"# Local literature evidence: {args.question}", ""] if english else [f"# 本地文献问答证据：{args.question}", ""]
        if not scored:
            lines.append("No matching evidence was found in the local full-text library." if english else "本地全文库中未找到匹配证据。")
        else:
            lines.append("Most relevant evidence papers:" if english else "最相关的证据文献：")
            lines.append("")
            for score, row, evidence in scored[: args.top_k]:
                direction = (row["direction"] or "other") if english else DIRECTION_LABELS.get(row["direction"] or "other", row["direction"] or "other")
                status = row["status"] if english else STATUS_LABELS.get(row["status"], row["status"])
                lines.append(f"- {row['title']} ({row['year'] or ('year unavailable' if english else '年份未知')})")
                lines.append(f"  DOI: {row['doi'] or 'unavailable'}" if english else f"  DOI：{row['doi'] or '缺失'}")
                lines.append(f"  Direction/status: {direction} / {status}" if english else f"  方向/状态：{direction} / {status}")
                if args.evidence:
                    lines.append(
                        f"  Evidence: {evidence or row['method_evidence'] or 'matching full text found'}"
                        if english
                        else f"  原文证据：{evidence or row['method_evidence'] or '找到匹配全文'}"
                    )
            lines.append("")
            lines.append("Evidence distribution:" if english else "综合判断：")
            dirs = {}
            for _, row, _ in scored[: args.top_k]:
                dirs[row["direction"] or "other"] = dirs.get(row["direction"] or "other", 0) + 1
            parts = []
            for key, value in sorted(dirs.items(), key=lambda kv: kv[1], reverse=True):
                parts.append(f"{key} ({value})" if english else f"{DIRECTION_LABELS.get(key, key)}（{value}）")
            lines.append("Local evidence is concentrated in: " + ", ".join(parts) + "." if english else "本地证据主要集中在：" + "、".join(parts) + "。")
        out = "\n".join(lines)
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8" if english else "utf-8-sig")
            log(f"answer written: {args.output}")
        else:
            print(out)
    finally:
        conn.close()


def command_update(args: argparse.Namespace) -> None:
    search_args = argparse.Namespace(**vars(args))
    search_args.sources = args.sources
    search_args.year_from = dt.date.today().year if args.days <= 366 else None
    search_args.year_to = dt.date.today().year
    search_args.max = args.max
    search_args.max_queries = args.max_queries
    search_args.pause = args.pause
    command_search(search_args)
    if args.fetch_pdfs:
        profile = load_profile(args.profile)
        fetch_scope = resolve_fetch_scope(profile, args.fetch_scope)
        if fetch_scope in {"screened", "pending_pdf"}:
            pre_screen_args = argparse.Namespace(**vars(args))
            pre_screen_args.stage = "abstract"
            pre_screen_args.require_pdf = True
            pre_screen_args.require_method_primary = True
            pre_screen_args.limit = 100000
            command_screen(pre_screen_args)
        fetch_args = argparse.Namespace(**vars(args))
        fetch_args.methods = args.methods
        fetch_args.require_pdf = True
        fetch_args.limit = args.fetch_limit
        fetch_args.scope = fetch_scope
        fetch_args.concurrency = getattr(args, "fetch_concurrency", DEFAULT_PDF_FETCH_CONCURRENCY)
        command_fetch(fetch_args)
        ingest_args = argparse.Namespace(**vars(args))
        ingest_args.verify_pdf = True
        ingest_args.force = False
        ingest_args.limit = args.fetch_limit
        command_ingest(ingest_args)
        screen_args = argparse.Namespace(**vars(args))
        screen_args.stage = "fulltext"
        screen_args.require_pdf = True
        screen_args.require_method_primary = True
        screen_args.limit = 100000
        command_screen(screen_args)
    if args.digest:
        sum_args = argparse.Namespace(**vars(args))
        sum_args.group_by = "direction"
        command_summarize(sum_args)


def command_migrate(args: argparse.Namespace) -> None:
    load_env_file(args.env_file)
    profile = load_profile(args.profile)
    pdir = ensure_project(profile)
    target_db = pdir / "library.sqlite"
    conn = connect(target_db)
    imported = 0
    try:
        src_db = Path(args.source_db)
        if src_db.exists():
            src = sqlite3.connect(str(src_db))
            src.row_factory = sqlite3.Row
            try:
                for row in src.execute("SELECT * FROM papers").fetchall():
                    paper = {
                        "doi": row["doi"] if "doi" in row.keys() else None,
                        "title": row["title"] if "title" in row.keys() else "",
                        "abstract": row["abstract"] if "abstract" in row.keys() else None,
                        "authors": json_list(row["authors"] if "authors" in row.keys() else None),
                        "publication_date": row["publication_date"] if "publication_date" in row.keys() else None,
                        "year": parse_year(row["publication_date"] if "publication_date" in row.keys() else None),
                        "journal": row["journal"] if "journal" in row.keys() else None,
                        "citation_count": row["citation_count"] if "citation_count" in row.keys() else 0,
                        "pdf_url": row["pdf_url"] if "pdf_url" in row.keys() else None,
                    }
                    upsert_paper(conn, paper, "migrated", "migrate")
                    pid = paper_id(paper["doi"], paper["title"])
                    if "pdf_path" in row.keys() and row["pdf_path"]:
                        src_pdf = Path(row["pdf_path"])
                        if src_pdf.exists():
                            dst = pdir / "pdfs" / src_pdf.name
                            shutil.copyfile(src_pdf, dst)
                            ok, pages, _ = verify_pdf(dst)
                            conn.execute(
                                "UPDATE papers SET pdf_path=?, pdf_method='migrated', pdf_status=?, pdf_pages=?, full_text=COALESCE(full_text, ?), updated_at=? WHERE id=?",
                                (str(dst), "ok" if ok else "invalid", pages, row["full_text"] if "full_text" in row.keys() else None, utc_now(), pid),
                            )
                    imported += 1
                conn.commit()
            finally:
                src.close()
        if args.pdf_dir and Path(args.pdf_dir).exists():
            for pdf in Path(args.pdf_dir).glob("*.pdf"):
                dst = pdir / "pdfs" / pdf.name
                if not dst.exists():
                    shutil.copyfile(pdf, dst)
        write_pdf_manifest(conn, pdir, profile)
        record_run(conn, "migrate", {"imported": imported, "source_db": args.source_db})
    finally:
        conn.close()
    log(f"migrated records: {imported}")


def command_doctor(args: argparse.Namespace) -> None:
    """Report capabilities without printing credential values or changing state."""
    load_env_file(args.env_file)
    modules = {
        "yaml": "PyYAML",
        "fitz": "PyMuPDF",
        "curl_cffi": "curl-cffi",
        "httpx": "httpx",
        "cryptography": "cryptography",
        "playwright": "playwright",
    }
    report: Dict[str, Any] = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "default_data_root": str(DEFAULT_DATA_ROOT),
        "runtime_config": str(runtime_config_path().resolve()) if runtime_config_path() else None,
        "dependencies": {label: importlib.util.find_spec(module) is not None for module, label in modules.items()},
        "credentials_configured": {
            "wos": bool(os.environ.get("WOS_API_KEY")),
            "openalex_email": bool(os.environ.get("OPENALEX_EMAIL")),
            "unpaywall_email": bool(os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("OPENALEX_EMAIL")),
            "webvpn": all(os.environ.get(key) for key in ("WEBVPN_URL", "WEBVPN_USERNAME", "WEBVPN_PASSWORD")),
            "proxy": bool(os.environ.get("PROXY_URL")),
        },
        "webvpn_tls_verification": webvpn_verify_tls(),
    }
    if args.profile:
        profile = load_profile(args.profile)
        report["profile"] = {
            "path": str(Path(args.profile).resolve()),
            "project": project_name(profile),
            "screening_mode": profile_str(profile, "screening_mode", "generic"),
            "project_dir": str(project_dir(profile)),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, help="Path to research profile YAML")
    parser.add_argument("--env-file", help="Optional .env file to load without printing secrets")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDF-grounded literature research pipeline")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="Check optional dependencies and configured capabilities")
    p.add_argument("--profile", help="Optional profile to validate and resolve")
    p.add_argument("--env-file", help="Optional .env file to load without printing secrets")
    p.set_defaults(func=command_doctor)

    p = sub.add_parser("init")
    add_common(p)
    p.add_argument("--name", required=True)
    p.set_defaults(func=command_init)

    p = sub.add_parser("search")
    add_common(p)
    p.add_argument("--sources", default="wos,openalex,crossref")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--depth", choices=sorted(SEARCH_DEPTH_DEFAULTS), help="Search depth preset: quick, balanced, deep, or exhaustive")
    p.add_argument("--query-strategy", choices=["balanced_matrix", "priority", "exhaustive_pairwise"], help="How topic/method query pairs are ordered")
    p.add_argument("--max", type=int, help="Global raw-record budget across sources; 0 means no global cap")
    p.add_argument("--max-queries", type=int, help="Maximum generated topic-method queries; 0 means full pairwise query set")
    p.add_argument("--max-per-query", type=int, help="Maximum records requested from each source for each query")
    p.add_argument("--source-max-pages", type=int, help="Maximum pages/cursor batches fetched per source/query")
    p.add_argument("--split-years", action="store_true", help="Split the configured date range into year chunks to reduce source-level truncation")
    p.add_argument("--no-split-years", action="store_true", help="Disable year chunking even if the profile/depth enables it")
    p.add_argument("--year-chunk-size", type=int, help="Number of years per search chunk when year splitting is enabled")
    p.add_argument("--pause", type=float, default=0.2)
    p.set_defaults(func=command_search)

    p = sub.add_parser("search-journals")
    add_common(p)
    p.add_argument("--sources", default="openalex,crossref")
    p.add_argument("--journal-scope", choices=["high_level", "priority", "q1", "tracked"], default="high_level")
    p.add_argument("--journals", help="Semicolon-separated journal names; overrides --journal-scope")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--max-per-journal-year", type=int, help="Maximum records per journal/year/source")
    p.add_argument("--source-max-pages", type=int, help="Maximum pages/cursor batches per journal/year/source")
    p.add_argument("--pause", type=float, default=0.2)
    p.set_defaults(func=command_search_journals)

    p = sub.add_parser("fetch")
    add_common(p)
    p.add_argument("--require-pdf", action="store_true")
    p.add_argument("--methods", help="Comma-separated PDF methods; defaults to profile pdf_methods or unpaywall,publisher,webvpn,ezproxy")
    p.add_argument("--scope", choices=["all", "screened", "pending_pdf", "included"], help="PDF target scope: all candidates, screened candidates, pending PDF records, or included records")
    p.add_argument("--concurrency", type=int, default=DEFAULT_PDF_FETCH_CONCURRENCY, help=f"PDF fetch process concurrency; default {DEFAULT_PDF_FETCH_CONCURRENCY}, set 1 for conservative sequential publisher access")
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=command_fetch)

    p = sub.add_parser("ingest")
    add_common(p)
    p.add_argument("--pdf-dir", help="Reserved; PDFs are read from manifest/database")
    p.add_argument("--verify-pdf", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=command_ingest)

    p = sub.add_parser("import-pdfs")
    add_common(p)
    p.add_argument("--pdf-dir", required=True, help="Directory containing manually downloaded PDFs to match and import")
    p.add_argument("--verify-pdf", action="store_true", default=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--add-unmatched", action="store_true", help="Also create local records for PDFs that do not match existing candidates")
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=command_ingest)

    p = sub.add_parser("screen")
    add_common(p)
    p.add_argument("--stage", choices=["metadata", "abstract", "fulltext"], default="abstract")
    p.add_argument("--require-pdf", action="store_true")
    p.add_argument("--require-method-primary", action="store_true")
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=command_screen)

    p = sub.add_parser("summarize")
    add_common(p)
    p.add_argument("--group-by", default="direction")
    p.set_defaults(func=command_summarize)

    p = sub.add_parser("ask")
    add_common(p)
    p.add_argument("--question", required=True)
    p.add_argument("--evidence", action="store_true")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--output")
    p.set_defaults(func=command_ask)

    p = sub.add_parser("update")
    add_common(p)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--sources", default="wos,openalex,crossref")
    p.add_argument("--depth", choices=sorted(SEARCH_DEPTH_DEFAULTS), help="Search depth preset for update")
    p.add_argument("--query-strategy", choices=["balanced_matrix", "priority", "exhaustive_pairwise"], help="How topic/method query pairs are ordered")
    p.add_argument("--max", type=int, help="Global raw-record budget across sources; 0 means no global cap")
    p.add_argument("--max-queries", type=int, help="Maximum generated topic-method queries; 0 means full pairwise query set")
    p.add_argument("--max-per-query", type=int, help="Maximum records requested from each source for each query")
    p.add_argument("--source-max-pages", type=int, help="Maximum pages/cursor batches fetched per source/query")
    p.add_argument("--split-years", action="store_true", help="Split the configured date range into year chunks to reduce source-level truncation")
    p.add_argument("--no-split-years", action="store_true", help="Disable year chunking even if the profile/depth enables it")
    p.add_argument("--year-chunk-size", type=int, help="Number of years per search chunk when year splitting is enabled")
    p.add_argument("--pause", type=float, default=0.2)
    p.add_argument("--fetch-pdfs", action="store_true")
    p.add_argument("--methods", help="Comma-separated PDF methods; defaults to profile pdf_methods or unpaywall,publisher,webvpn,ezproxy")
    p.add_argument("--fetch-scope", choices=["all", "screened", "pending_pdf", "included"], help="PDF target scope when --fetch-pdfs is used")
    p.add_argument("--fetch-limit", type=int, default=500)
    p.add_argument("--fetch-concurrency", type=int, default=DEFAULT_PDF_FETCH_CONCURRENCY, help=f"PDF fetch process concurrency for --fetch-pdfs; default {DEFAULT_PDF_FETCH_CONCURRENCY}")
    p.add_argument("--digest", action="store_true")
    p.set_defaults(func=command_update)

    p = sub.add_parser("export")
    add_common(p)
    p.add_argument("--format", default="markdown,csv,bibtex")
    p.add_argument("--include-pdf-manifest", action="store_true")
    p.set_defaults(func=command_export)

    p = sub.add_parser("audit-pdfs")
    add_common(p)
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=command_audit_pdfs)

    p = sub.add_parser("audit-search")
    add_common(p)
    p.add_argument("--top", type=int, default=20, help="Number of truncated search partitions to show in the Markdown audit")
    p.set_defaults(func=command_audit_search)

    p = sub.add_parser("migrate")
    add_common(p)
    p.add_argument("--source-db", required=True)
    p.add_argument("--pdf-dir")
    p.set_defaults(func=command_migrate)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
