from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.settings import get_user_setting_value, save_user_setting_value
from app.domain.paper_map import PaperMapQuestionCandidate

QUESTION_LIBRARY_VERSION = "paper-map-questions-v1"
USER_QUESTION_LIBRARY_KEY = "paper_map.initial_questions.v1"
MAX_USER_QUESTIONS = 40


@dataclass(frozen=True, slots=True)
class MapQuestionDefinition:
    key: str
    text: str
    dimension: str
    paper_types: tuple[str, ...] = ()
    priority: int = 100


QUESTION_LIBRARY = (
    MapQuestionDefinition("U_PROBLEM", "论文试图解决什么问题，为什么重要？", "problem", priority=1),
    MapQuestionDefinition("U_NOVELTY", "这个问题或解法的新颖性在哪里？", "novelty", priority=4),
    MapQuestionDefinition("U_CLAIMS", "论文最核心、可检验的主张是什么？", "claims", priority=2),
    MapQuestionDefinition("U_CONTEXT", "它建立在哪些研究脉络或前提上？", "context", priority=7),
    MapQuestionDefinition("U_SOLUTION", "解决方案或论证的关键是什么？", "solution", priority=3),
    MapQuestionDefinition("U_EVIDENCE", "主要证据是否支持核心主张？", "evidence", priority=5),
    MapQuestionDefinition(
        "U_CONTRIBUTION", "这篇论文可以明确归纳出哪些贡献？", "contribution", priority=6
    ),
    MapQuestionDefinition(
        "U_LIMITATIONS", "论文的适用边界、限制和风险是什么？", "limitations", priority=8
    ),
    MapQuestionDefinition(
        "U_ARTIFACTS", "数据、代码、模型或其他研究产物是否可用？", "artifacts", priority=9
    ),
    MapQuestionDefinition(
        "U_FUTURE", "作者提出了什么未来工作，还可以继续深入什么？", "future", priority=10
    ),
    MapQuestionDefinition(
        "M_EXPERIMENT_DESIGN",
        "实验如何设计，基线、变量和评价指标分别是什么？",
        "experiment",
        ("METHOD", "EMPIRICAL", "APPLICATION"),
        20,
    ),
    MapQuestionDefinition(
        "M_ABLATION",
        "消融和稳健性分析能否隔离关键组件的贡献？",
        "evidence",
        ("METHOD", "EMPIRICAL"),
        21,
    ),
    MapQuestionDefinition(
        "M_REPRODUCIBILITY",
        "训练细节、计算资源和复现条件是否充分？",
        "reproducibility",
        ("METHOD", "EMPIRICAL", "SYSTEM", "APPLICATION"),
        22,
    ),
    MapQuestionDefinition(
        "T_ASSUMPTIONS",
        "理论结果依赖哪些关键假设和边界条件？",
        "assumptions",
        ("THEORY",),
        20,
    ),
    MapQuestionDefinition(
        "T_PROOF_IDEA", "证明的关键构造或不变量是什么？", "proof", ("THEORY",), 21
    ),
    MapQuestionDefinition(
        "S_ARCHITECTURE",
        "系统由哪些组件组成，核心数据流和设计取舍是什么？",
        "architecture",
        ("SYSTEM",),
        20,
    ),
    MapQuestionDefinition(
        "S_EVALUATION",
        "系统评估是否覆盖真实工作负载、瓶颈、成本与故障边界？",
        "evidence",
        ("SYSTEM",),
        21,
    ),
    MapQuestionDefinition(
        "D_PROVENANCE",
        "数据从哪里来，如何采集、过滤、标注和质量控制？",
        "dataset",
        ("DATASET_BENCHMARK",),
        20,
    ),
    MapQuestionDefinition(
        "D_VALIDITY",
        "基准指标真正测量什么，是否存在泄漏、偏差或投机空间？",
        "validity",
        ("DATASET_BENCHMARK",),
        21,
    ),
    MapQuestionDefinition(
        "R_SCOPE",
        "综述的范围、检索策略、纳入和排除标准是什么？",
        "review_method",
        ("SURVEY_REVIEW",),
        20,
    ),
    MapQuestionDefinition(
        "R_TAXONOMY",
        "综述如何组织研究类别，共识、争议和证据空白是什么？",
        "review_synthesis",
        ("SURVEY_REVIEW",),
        21,
    ),
    MapQuestionDefinition(
        "Q_METHOD",
        "研究对象如何采样，数据如何编码、解释和交叉验证？",
        "qualitative_method",
        ("QUALITATIVE_CASE",),
        20,
    ),
    MapQuestionDefinition(
        "N_REPLICATION",
        "复现条件与原研究有何差异，负结果实际限定了什么？",
        "replication",
        ("REPLICATION_NEGATIVE",),
        20,
    ),
    MapQuestionDefinition(
        "P_ARGUMENT",
        "核心论点、必要前提和最强反对意见分别是什么？",
        "argument",
        ("POSITION_CONCEPTUAL",),
        20,
    ),
)

QUESTION_BY_KEY = {item.key: item for item in QUESTION_LIBRARY}
MIN_LIBRARY_QUESTIONS = 4


def question_text(key: str) -> str:
    return QUESTION_BY_KEY[key].text


def allowed_question_keys() -> tuple[str, ...]:
    return tuple(item.key for item in QUESTION_LIBRARY)


def _decode_legacy_library(value: str) -> object:
    return json.loads(value)


def _library_state(session: Session, *, user_id: str) -> dict[str, object]:
    payload = get_user_setting_value(
        session,
        user_id=user_id,
        key=USER_QUESTION_LIBRARY_KEY,
        default=None,
        legacy_decode=_decode_legacy_library,
    )
    if payload is None:
        return {"custom": [], "overrides": {}, "deleted": []}
    if isinstance(payload, list):
        return {"custom": payload, "overrides": {}, "deleted": []}
    if not isinstance(payload, dict):
        return {"custom": [], "overrides": {}, "deleted": []}
    return {
        "custom": payload.get("custom") if isinstance(payload.get("custom"), list) else [],
        "overrides": (
            payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {}
        ),
        "deleted": payload.get("deleted") if isinstance(payload.get("deleted"), list) else [],
    }


def _custom_definitions(state: dict[str, object]) -> tuple[MapQuestionDefinition, ...]:
    definitions: list[MapQuestionDefinition] = []
    for item in state["custom"]:
        if not isinstance(item, dict):
            continue
        key, text = item.get("key"), item.get("text")
        if (
            isinstance(key, str)
            and key.startswith("CUSTOM_")
            and isinstance(text, str)
            and 8 <= len(text.strip()) <= 500
        ):
            definitions.append(
                MapQuestionDefinition(
                    key=key,
                    text=text.strip(),
                    dimension="user_defined",
                    priority=50,
                )
            )
    return tuple(definitions[:MAX_USER_QUESTIONS])


def _save_library_state(
    session: Session,
    state: dict[str, object],
    *,
    user_id: str,
) -> None:
    value = {
        "version": 2,
        "custom": state["custom"],
        "overrides": state["overrides"],
        "deleted": state["deleted"],
    }
    save_user_setting_value(
        session,
        user_id=user_id,
        key=USER_QUESTION_LIBRARY_KEY,
        value=value,
    )


def user_question_definitions(
    session: Session,
    *,
    user_id: str,
) -> tuple[MapQuestionDefinition, ...]:
    return _custom_definitions(_library_state(session, user_id=user_id))


def all_question_definitions(
    session: Session,
    *,
    user_id: str,
) -> tuple[MapQuestionDefinition, ...]:
    state = _library_state(session, user_id=user_id)
    overrides = state["overrides"]
    deleted = {key for key in state["deleted"] if isinstance(key, str)}
    builtins = tuple(
        MapQuestionDefinition(
            key=item.key,
            text=str(overrides.get(item.key, item.text)).strip(),
            dimension=item.dimension,
            paper_types=item.paper_types,
            priority=item.priority,
        )
        for item in QUESTION_LIBRARY
        if item.key not in deleted
    )
    return (*builtins, *_custom_definitions(state))


def question_candidates(
    session: Session,
    *,
    user_id: str,
) -> tuple[PaperMapQuestionCandidate, ...]:
    user_keys = {item.key for item in user_question_definitions(session, user_id=user_id)}
    return tuple(
        PaperMapQuestionCandidate(
            question_key=item.key,
            question_text=item.text,
            dimension=item.dimension,
            paper_types=item.paper_types,
            user_defined=item.key in user_keys,
        )
        for item in all_question_definitions(session, user_id=user_id)
    )


def add_user_question(
    session: Session,
    text: str,
    *,
    user_id: str,
) -> MapQuestionDefinition:
    normalized = text.strip()
    if not 8 <= len(normalized) <= 500:
        raise ValueError("Initial map questions must contain between 8 and 500 characters.")
    state = _library_state(session, user_id=user_id)
    current = list(_custom_definitions(state))
    if len(current) >= MAX_USER_QUESTIONS:
        raise ValueError(f"At most {MAX_USER_QUESTIONS} custom initial questions are allowed.")
    if any(
        item.text.casefold() == normalized.casefold()
        for item in all_question_definitions(session, user_id=user_id)
    ):
        raise ValueError("That initial question already exists.")
    definition = MapQuestionDefinition(
        key=f"CUSTOM_{uuid.uuid4().hex[:16].upper()}",
        text=normalized,
        dimension="user_defined",
        priority=50,
    )
    state["custom"] = [
        *state["custom"],
        {"key": definition.key, "text": definition.text},
    ]
    _save_library_state(session, state, user_id=user_id)
    return definition


def update_question(
    session: Session,
    key: str,
    text: str,
    *,
    user_id: str,
) -> MapQuestionDefinition | None:
    normalized = text.strip()
    if not 8 <= len(normalized) <= 500:
        raise ValueError("Initial map questions must contain between 8 and 500 characters.")
    current = all_question_definitions(session, user_id=user_id)
    target = next((item for item in current if item.key == key), None)
    if target is None:
        return None
    if any(item.key != key and item.text.casefold() == normalized.casefold() for item in current):
        raise ValueError("That initial question already exists.")
    state = _library_state(session, user_id=user_id)
    if key in QUESTION_BY_KEY:
        overrides = dict(state["overrides"])
        if normalized == QUESTION_BY_KEY[key].text:
            overrides.pop(key, None)
        else:
            overrides[key] = normalized
        state["overrides"] = overrides
    else:
        state["custom"] = [
            {**item, "text": normalized}
            if isinstance(item, dict) and item.get("key") == key
            else item
            for item in state["custom"]
        ]
    _save_library_state(session, state, user_id=user_id)
    return MapQuestionDefinition(
        key=target.key,
        text=normalized,
        dimension=target.dimension,
        paper_types=target.paper_types,
        priority=target.priority,
    )


def delete_question(session: Session, key: str, *, user_id: str) -> bool:
    current = all_question_definitions(session, user_id=user_id)
    if not any(item.key == key for item in current):
        return False
    if len(current) <= MIN_LIBRARY_QUESTIONS:
        raise ValueError(f"At least {MIN_LIBRARY_QUESTIONS} initial questions must remain.")
    state = _library_state(session, user_id=user_id)
    if key in QUESTION_BY_KEY:
        state["deleted"] = [*{*state["deleted"], key}]
        overrides = dict(state["overrides"])
        overrides.pop(key, None)
        state["overrides"] = overrides
    else:
        state["custom"] = [
            item for item in state["custom"] if not isinstance(item, dict) or item.get("key") != key
        ]
    _save_library_state(session, state, user_id=user_id)
    return True


def default_question_keys(primary_type: str, *, limit: int = 8) -> tuple[str, ...]:
    applicable = [
        item
        for item in QUESTION_LIBRARY
        if not item.paper_types or primary_type in item.paper_types
    ]
    return tuple(item.key for item in sorted(applicable, key=lambda item: item.priority)[:limit])
