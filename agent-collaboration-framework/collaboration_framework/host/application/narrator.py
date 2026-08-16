"""Application-level validation for player-visible narration."""

from __future__ import annotations

import re
from typing import Literal

from collaboration_framework.contracts import ContractError
from collaboration_framework.host.ports import NarrationModelPort
from collaboration_framework.host.schemas import NarrationContext, NarrationOutput

from .persistent_results import (
    unsupported_inventory_acquisition_claim,
    unsupported_persistent_claim,
)

NarrationRejectionReason = Literal[
    "outer_schema",
    "fact_scope",
    "protocol_tail",
    "schema_fragment",
    "subject_ownership",
    "persistent_claim_without_evidence",
    "required_evidence_missing",
    "focus_shift_without_evidence",
    "visible_corpse_search_conflict",
    "clarification_kind",
]

_NARRATION_FIELD = (
    r"(?<![A-Za-z0-9_])"
    r"(?:kind|text|claimed_fact_ids|claimedFactIds|claimed_evidence_refs|"
    r"claimedEvidenceRefs|suggested_actions|suggestedActions)"
    r"(?![A-Za-z0-9_])"
)
_QUOTED_NARRATION_FIELD = rf"""(?:"|')?{_NARRATION_FIELD}(?:"|')?"""
_STRUCTURED_VALUE = r"(?:\[[\s\S]*?\]|\{[\s\S]*?\}|null)"
_QUOTED_STRING_VALUE = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
_NARRATION_FIELD_VALUE = (
    rf"(?:{_STRUCTURED_VALUE}|{_QUOTED_STRING_VALUE}|narration|clarification)"
)

_STANDALONE_NARRATION_FIELD_RE = re.compile(
    rf"""
    ^[ \t]*[{{,]?[ \t]*
    {_QUOTED_NARRATION_FIELD}
    [ \t]*[:：][ \t]*
    (?:{_NARRATION_FIELD_VALUE})?
    [ \t]*[,}}]?[ \t]*(?:\r?\n[ \t]*```)?[ \t]*$
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_TRAILING_NARRATION_FIELD_RE = re.compile(
    rf"""
    (?:^|[\r\n]|[。！？.!?]|(?<=\s))[ \t]*
    [{{,]?[ \t]*
    {_QUOTED_NARRATION_FIELD}
    [ \t]*[:：][ \t]*
    (?:{_NARRATION_FIELD_VALUE})?
    [ \t]*[,}}]*[ \t]*(?:\r?\n[ \t]*```)?[ \t]*$
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

_ESCAPED_TEXT_TAIL_RE = re.compile(
    rf"""
    ["'][ \t]*,[ \t]*
    {_QUOTED_NARRATION_FIELD}
    [ \t]*[:：]
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TRAILING_OBJECT_FRAGMENT_RE = re.compile(
    r"""
    (?:^|[\r\n]|[。！？.!?]|(?<=\s))[ \t]*
    (?:```(?:json)?[ \t]*[\r\n]+)?
    (?P<object>\{.*)
    [ \t]*$
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

_NARRATION_KIND_RE = re.compile(
    r"""(?:"|')?kind(?:"|')?[ \t]*[:：][ \t]*(?:"|')?(?:narration|clarification)(?:"|')?""",
    re.IGNORECASE,
)

_NARRATION_FIELD_KEY_RE = re.compile(
    r"""
    (?:"|')?
    (?P<field>kind|text|claimed_fact_ids|claimedFactIds|claimed_evidence_refs|
    claimedEvidenceRefs|suggested_actions|suggestedActions)
    (?:"|')?
    [ \t]*[:：]
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NARRATION_FIELD_TOKEN_RE = re.compile(
    r"""(?:"|')?(kind|text|claimed_fact_ids|claimedFactIds|claimed_evidence_refs|claimedEvidenceRefs|suggested_actions|suggestedActions)(?:"|')?""",
    re.IGNORECASE,
)

_SCHEMA_MARKER_RE = re.compile(
    r"""(?:"|')?(?:properties|required)(?:"|')?[ \t]*[:：]""",
    re.IGNORECASE,
)

_QUOTED_SPAN_DELIMITERS = {
    "“": "”",
    "「": "」",
    "『": "』",
    "‘": "’",
    '"': '"',
    "'": "'",
    "《": "》",
}
_FIRST_PERSON_RE = re.compile(r"[我咱]")
_CORPSE_SEARCH_QUESTION = re.compile(
    r"(?:从哪里|哪里).{0,12}(?:找|搜)|(?:寻找|搜寻).{0,12}(?:尸体|遗体)"
)


def unsupported_focus_shift_claim(
    text: str,
    *,
    focus_entity_ids: tuple[str, ...],
    visible_entities: tuple[object, ...],
    evidence_subject_ids: set[str],
) -> str | None:
    """检查叙事是否无证据切换到另一个可见实体。"""
    if not focus_entity_ids:
        return None
    focused = set(focus_entity_ids)
    normalized_text = text.replace("的", "")
    for entity in visible_entities:
        entity_id = getattr(entity, "id", None)
        if entity_id in focused or entity_id in evidence_subject_ids:
            continue
        labels = (getattr(entity, "name", ""), *getattr(entity, "aliases", ()))
        if any(
            len(normalized) >= 2 and normalized in normalized_text
            for label in labels
            if (normalized := label.replace("的", ""))
        ):
            return entity_id
    return None


class NarrationValidationError(ContractError):
    """A model candidate failed deterministic player-visible output policy."""

    def __init__(self, reason: NarrationRejectionReason) -> None:
        super().__init__("NarrationOutput 未通过玩家可见输出安全校验")
        self.reason = reason


def normalize_narration_text(text: str) -> str:
    """Convert model-emitted literal newline escapes to canonical LF characters."""

    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


_SENTENCE_END_CHARS = "。！？!?…"
_SENTENCE_CLOSING_CHARS = "」』”’\"')）】"

_NARRATION_PIECE_RE = re.compile(
    rf"[^{_SENTENCE_END_CHARS}]*[{_SENTENCE_END_CHARS}]+[{_SENTENCE_CLOSING_CHARS}]*\s*"
    rf"|[^{_SENTENCE_END_CHARS}]+"
)

# 只用来挡住"……"、"好。"这种退化片段。定得再高会把正常的短句（"陈探员此刻
# 就在这里。"）并进前一段，短叙事会整段塌成单片、退回非流式。
_MIN_CHUNK_CHARS = 6


def split_narration_chunks(
    text: str, *, min_chars: int = _MIN_CHUNK_CHARS
) -> tuple[str, ...]:
    """Split already-validated narration text at sentence boundaries.

    Only for progressive delivery of text that has *already* passed
    ``Narrator.narrate()``. Never use it to emit unvalidated model output: a
    fragment carries no independent safety guarantee, so the caller must have
    validated the whole narration first.

    Concatenating the result reproduces ``text`` byte for byte — clients that
    accumulate chunks must end up with exactly the persisted narration. Pieces
    shorter than ``min_chars`` are merged forward so a stray "……" does not
    become its own chunk.
    """

    if not text:
        return ()

    chunks: list[str] = []
    buffer = ""
    for match in _NARRATION_PIECE_RE.finditer(text):
        buffer += match.group(0)
        if len(buffer.strip()) >= min_chars:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        if chunks:
            chunks[-1] += buffer
        else:
            chunks.append(buffer)
    return tuple(chunks)


def narration_text_rejection_reason(
    text: str,
) -> Literal["protocol_tail", "schema_fragment"] | None:
    """Return a safe category for obvious Narration protocol residue."""

    if _STANDALONE_NARRATION_FIELD_RE.search(text):
        return "protocol_tail"
    if _TRAILING_NARRATION_FIELD_RE.search(text):
        return "protocol_tail"
    if _ESCAPED_TEXT_TAIL_RE.search(text):
        return "protocol_tail"

    object_match = _TRAILING_OBJECT_FRAGMENT_RE.search(text)
    if object_match is None:
        return None
    candidate = object_match.group("object")
    if _NARRATION_KIND_RE.search(candidate):
        return "protocol_tail"

    if _SCHEMA_MARKER_RE.search(candidate):
        field_tokens = {
            match.group(1).casefold()
            for match in _NARRATION_FIELD_TOKEN_RE.finditer(candidate)
        }
        if len(field_tokens) >= 2:
            return "schema_fragment"

    field_keys = {
        match.group("field").casefold()
        for match in _NARRATION_FIELD_KEY_RE.finditer(candidate)
    }
    if len(field_keys) >= 2:
        return "protocol_tail"
    return None


def narration_subject_rejection_reason(
    text: str,
) -> Literal["subject_ownership"] | None:
    """Reject first-person ownership in prose while preserving quoted speech."""

    quoted = [False] * len(text)
    for opening, closing in _QUOTED_SPAN_DELIMITERS.items():
        start: int | None = None
        for index, character in enumerate(text):
            if opening == closing:
                if character != opening:
                    continue
                if start is None:
                    start = index
                else:
                    quoted[start : index + 1] = [True] * (index + 1 - start)
                    start = None
                continue
            if character == opening and start is None:
                start = index
            elif character == closing and start is not None:
                quoted[start : index + 1] = [True] * (index + 1 - start)
                start = None

    prose = "".join(
        character for index, character in enumerate(text) if not quoted[index]
    )
    if _FIRST_PERSON_RE.search(prose):
        return "subject_ownership"
    return None


class Narrator:
    def __init__(self, model: NarrationModelPort) -> None:
        self._model = model

    async def narrate(self, context: NarrationContext) -> NarrationOutput:
        raw = await self._model.generate(context)
        if isinstance(raw, dict) and isinstance(raw.get("text"), str):
            raw = {**raw, "text": normalize_narration_text(raw["text"])}
        if isinstance(raw, dict) and "claimed_evidence_refs" not in raw:
            # PR1 读取旧 Adapter 的 fact 字段，但规范输出只保留 evidence refs。
            legacy_claims = raw.get("claimed_fact_ids")
            if isinstance(legacy_claims, (list, tuple)):
                raw = {
                    key: value
                    for key, value in raw.items()
                    if key != "claimed_fact_ids"
                }
                raw["claimed_evidence_refs"] = legacy_claims
        try:
            output = NarrationOutput.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise NarrationValidationError("outer_schema") from exc
        action_result = getattr(context, "action_result", None)
        legacy_facts = getattr(action_result, "visible_facts", ())
        allowed = set(getattr(context, "allowed_evidence_refs", ()))
        if not allowed:
            allowed = {fact.id for fact in legacy_facts}
        if not set(output.claimed_evidence_refs).issubset(allowed):
            reason = (
                "evidence_scope"
                if hasattr(context, "allowed_evidence_refs")
                else "fact_scope"
            )
            raise NarrationValidationError(reason)
        completed_steps = getattr(context, "completed_steps", ())
        required = tuple(
            item
            for step in completed_steps
            for item in getattr(step, "narration_evidence", ())
            if item.required_in_narration
        )
        mentioned_required = tuple(
            item
            for item in required
            if any(
                label and label in output.text
                for label in (item.subject_name, *item.subject_aliases)
            )
        )
        if len(mentioned_required) != len(required):
            raise NarrationValidationError("required_evidence_missing")
        claimed = tuple(
            dict.fromkeys(
                (
                    *output.claimed_evidence_refs,
                    *(item.ref for item in mentioned_required),
                )
            )
        )
        if claimed != output.claimed_evidence_refs:
            output = output.model_copy(update={"claimed_evidence_refs": claimed})
        rejection_reason = narration_text_rejection_reason(output.text)
        if rejection_reason is not None:
            raise NarrationValidationError(rejection_reason)
        subject_rejection = narration_subject_rejection_reason(output.text)
        if subject_rejection is not None:
            raise NarrationValidationError(subject_rejection)
        # 普通单动作叙事同样只能描述最终 PlayerView 已确认的持久状态，
        # 避免它绕过 ActionPlanNarrator 的证据边界自行补写 NPC 后果。
        committed_results = tuple(
            result
            for step in completed_steps
            for result in getattr(step, "committed_results", ())
        )
        persistent_rejection = unsupported_persistent_claim(
            output.text,
            committed_results,
            getattr(context, "player_view", None),
        )
        if persistent_rejection is not None:
            raise NarrationValidationError(
                f"persistent_claim_without_evidence:{persistent_rejection}"
            )
        inventory_rejection = unsupported_inventory_acquisition_claim(
            output.text,
            committed_results,
            getattr(context, "player_view", None),
        )
        if inventory_rejection is not None:
            raise NarrationValidationError(
                f"persistent_claim_without_evidence:{inventory_rejection}"
            )
        player_view = getattr(context, "player_view", None)
        scene = getattr(player_view, "scene", None)
        shifted_entity_id = unsupported_focus_shift_claim(
            output.text,
            focus_entity_ids=getattr(context, "focus_entity_ids", ()),
            visible_entities=tuple(
                (
                    *getattr(scene, "visible_entities", ()),
                    *getattr(scene, "visible_actors", ()),
                )
            ),
            evidence_subject_ids={
                item.subject_id for item in getattr(context, "narration_evidence", ())
            },
        )
        if shifted_entity_id is not None:
            raise NarrationValidationError(
                f"focus_shift_without_evidence:{shifted_entity_id}"
            )
        visible_dead = tuple(
            entity
            for entity in getattr(scene, "visible_entities", ())
            if any(
                state.key == "consciousness" and state.value == "dead"
                for state in entity.observable_state
            )
        )
        if (
            visible_dead
            and any(
                word in getattr(getattr(context, "player_input", None), "utterance", "")
                for word in ("尸体", "遗体")
            )
            and _CORPSE_SEARCH_QUESTION.search(output.text)
        ):
            raise NarrationValidationError("visible_corpse_search_conflict")
        if (
            getattr(context, "termination_status", None) == "needs_clarification"
            and output.kind != "clarification"
        ):
            raise NarrationValidationError("clarification_kind")
        return output
