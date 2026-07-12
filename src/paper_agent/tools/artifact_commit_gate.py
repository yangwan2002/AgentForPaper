"""Pre-commit fact gate for Artifact-grounded manuscript sections.

The gate operates on a candidate ``SectionDraft`` before it can mutate the
workspace.  It is deliberately deterministic: a provider cannot talk its way
around a missing evidence binding, stale artifact hash, unknown entity, seed,
or numeric result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.tools.quality_gate import (
    QualityGate,
    build_allowed_values,
    extract_text_citations,
    value_matches,
)
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)


@dataclass
class ArtifactCommitResult:
    passed: bool
    violations: list[dict] = field(default_factory=list)

    @property
    def high_violations(self) -> list[dict]:
        return [item for item in self.violations if item.get("severity") == "high"]


def build_claim_manifest(content: str, evidence_ids: list[str]) -> list[dict]:
    """Extract high-risk factual sentences and bind them to the section scope."""
    if not evidence_ids:
        return []
    return [
        {
            "claim": sentence[:1000],
            "evidence_ids": list(evidence_ids),
            "kind": "artifact_fact",
        }
        for sentence in _factual_sentences(content)
    ]


_FACTUAL = re.compile(
    r"\d|提出|贡献|实验|结果|数据集|基线|指标|优于|提升|降低|验证|表明|"
    r"\b(?:propose|contribution|experiment|dataset|baseline|outperform|"
    r"result|demonstrate|achieve)\b",
    re.IGNORECASE,
)


def _factual_sentences(content: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(r"(?<=[。！？.!?])\s+|\n+", content)
        if value.strip() and _FACTUAL.search(value)
    ]


class ArtifactCommitGate:
    """Reject candidate text that is not supported by the current artifact."""

    _DATASET_PATTERNS = (
        re.compile(r"(?:在|基于|使用|采用)\s*([A-Za-z][A-Za-z0-9_.+\-]{1,40})\s*数据集"),
        re.compile(r"(?:on|using)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9_.+\-]{1,40})\s+dataset", re.I),
    )
    _SEED = re.compile(r"(?:随机种子|random\s+seed|seed)\s*(?:为|=|:|is)?\s*(\d+)", re.I)
    _NAMED_METHOD = (
        re.compile(
            r"(?:提出|采用|引入|使用|基于)\s*(?:一种|了|the)?\s*"
            r"([A-Za-z][A-Za-z0-9+_.-]{1,40})\s*"
            r"(?:方法|模型|网络|模块|算法)",
            re.I,
        ),
        re.compile(
            r"\b([A-Z][A-Za-z0-9+_.-]{1,40})\s+"
            r"(?:method|model|network|module|algorithm)\b",
            re.I,
        ),
    )
    _BASELINE_BLOCK = re.compile(
        r"^\s*(?:基线(?:方法)?|baselines?)\s*"
        r"(?:包括|采用|为|:|：|include)?\s*([^。；\n]{1,180})$",
        re.I | re.M,
    )
    _PLACEHOLDER = re.compile(
        r"(TODO|FIXME|待补充|待完善|此处填写|\[填写\]|XXX|\?\?\?|tbd)",
        re.IGNORECASE,
    )

    def check(
        self,
        workspace: PaperWorkspace,
        node: OutlineNode,
        candidate: SectionDraft,
    ) -> ArtifactCommitResult:
        artifact = workspace.artifact
        if artifact is None or artifact.is_empty():
            return ArtifactCommitResult(passed=True)

        contract = artifact.contract()
        issues: list[dict] = []
        trusted_revision_baseline = (
            workspace.input_mode is InputMode.DRAFT_REVISION
            and candidate.content
            == workspace.draft_sections.get(node.section_id, "")
        )

        if not trusted_revision_baseline:
            if self._PLACEHOLDER.search(candidate.content):
                issues.append(
                    self._issue(node, "placeholder", "候选正文含“待补充/TODO”等未完成内容。")
                )
            verified_ids = workspace.verified_reference_ids()
            for reference_id in extract_text_citations(candidate.content):
                if reference_id not in verified_ids:
                    issues.append(
                        self._issue(
                            node,
                            "text_citation_invalid",
                            f"候选正文引用了未经核验的文献 id：{reference_id}",
                        )
                    )

        if candidate.artifact_hash != contract.artifact_hash:
            issues.append(self._issue(node, "stale_artifact_hash", "候选正文未基于当前 Artifact 哈希。"))

        valid_ids = set(contract.evidence)
        candidate_ids = set(candidate.evidence_ids)
        required_ids = set(node.required_evidence_ids)
        allowed_ids = set(node.allowed_evidence_ids or node.required_evidence_ids)
        unknown = candidate_ids - valid_ids
        missing = required_ids - candidate_ids
        out_of_scope = candidate_ids - allowed_ids if allowed_ids else set()
        if unknown:
            issues.append(self._issue(node, "unknown_evidence", f"未知证据ID：{sorted(unknown)}"))
        if missing:
            issues.append(self._issue(node, "missing_evidence", f"未兑现必需证据：{sorted(missing)}"))
        if out_of_scope:
            issues.append(self._issue(node, "evidence_out_of_scope", f"越权使用证据：{sorted(out_of_scope)}"))

        manifest_claims: set[str] = set()
        for manifest in candidate.claim_manifest:
            claim = str(manifest.get("claim") or "").strip()
            if claim:
                manifest_claims.add(claim)
            refs = set(manifest.get("evidence_ids") or [])
            if not claim or not refs:
                issues.append(self._issue(node, "unbound_claim", "事实声明缺少证据绑定。"))
            elif refs - candidate_ids:
                issues.append(
                    self._issue(
                        node,
                        "claim_evidence_out_of_scope",
                        f"声明引用了章节范围外证据：{sorted(refs - candidate_ids)}",
                    )
                )
            elif claim not in candidate.content:
                issues.append(
                    self._issue(node, "claim_not_in_content", "claim_manifest 声明不在候选正文中。")
                )
            elif not trusted_revision_baseline and not self._claim_supported(
                claim, refs, contract, workspace, node
            ):
                issues.append(
                    self._issue(
                        node,
                        "claim_evidence_unsupported",
                        f"声明与所绑定证据缺少可验证的语义/数值对应：{claim[:80]}",
                    )
                )

        if not trusted_revision_baseline:
            missing_claims = [
                sentence[:1000]
                for sentence in _factual_sentences(candidate.content)
                if sentence[:1000] not in manifest_claims
            ]
            if missing_claims:
                issues.append(
                    self._issue(
                        node,
                        "missing_claim_manifest",
                        f"{len(missing_claims)} 条事实声明未进入 claim_manifest。",
                    )
                )

            unfulfilled = [
                evidence_id
                for evidence_id in required_ids
                if not self._evidence_realized(
                    evidence_id, candidate.content, contract
                )
            ]
            if unfulfilled:
                issues.append(
                    self._issue(
                        node,
                        "required_evidence_not_realized",
                        f"正文未实际兑现必需证据：{sorted(unfulfilled)}",
                    )
                )

        if trusted_revision_baseline:
            return ArtifactCommitResult(
                passed=not any(item["severity"] == "high" for item in issues),
                violations=issues,
            )

        allowed_values = build_allowed_values(artifact)
        allowed_values.extend(
            QualityGate._extract_numeric_values(
                workspace.draft_sections.get(node.section_id, "")
            )
        )
        for number in QualityGate._extract_numeric_values(candidate.content):
            if allowed_values and not value_matches(number, allowed_values):
                issues.append(
                    self._issue(
                        node,
                        "fabricated_metric",
                        f"数字 {number} 未在 Artifact 或其确定性派生值中找到。",
                    )
                )

        datasets = {exp.dataset.strip().lower() for exp in artifact.experiments if exp.dataset.strip()}
        for pattern in self._DATASET_PATTERNS:
            for match in pattern.finditer(candidate.content):
                dataset = match.group(1).strip()
                normalized = dataset.lower()
                original = workspace.draft_sections.get(
                    node.section_id, ""
                ).lower()
                if not any(
                    normalized == allowed
                    or normalized in allowed
                    or allowed in normalized
                    for allowed in datasets
                ) and normalized not in original:
                    issues.append(
                        self._issue(node, "unknown_dataset", f"未知数据集：{dataset}")
                    )

        original_lower = workspace.draft_sections.get(node.section_id, "").lower()
        known_aliases = set(contract.entity_evidence)
        for pattern in self._NAMED_METHOD:
            for match in pattern.finditer(candidate.content):
                method = match.group(1).strip()
                if (
                    method.lower() not in known_aliases
                    and method.lower() not in original_lower
                ):
                    issues.append(
                        self._issue(node, "unknown_method", f"未知方法/组件：{method}")
                    )

        known_baselines = {
            baseline.strip().lower()
            for exp in artifact.experiments
            for baseline in exp.baselines
            if baseline.strip()
        }
        for block in self._BASELINE_BLOCK.finditer(candidate.content):
            segment = block.group(1)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{1,39}", segment):
                normalized = token.lower()
                if normalized in {
                    "and", "or", "the", "method", "methods", "model", "models",
                    "include", "includes", "our", "ours",
                }:
                    continue
                if not any(
                    normalized == baseline
                    or normalized in baseline
                    or baseline in normalized
                    for baseline in known_baselines
                ) and normalized not in original_lower:
                    issues.append(
                        self._issue(node, "unknown_baseline", f"未知基线：{token}")
                    )

        allowed_seeds = {exp.seed for exp in artifact.experiments if exp.seed is not None}
        original_content = workspace.draft_sections.get(node.section_id, "")
        for match in self._SEED.finditer(candidate.content):
            seed = int(match.group(1))
            if seed not in allowed_seeds and match.group(0) not in original_content:
                issues.append(self._issue(node, "unknown_seed", f"未知随机种子：{seed}"))

        return ArtifactCommitResult(
            passed=not any(item["severity"] == "high" for item in issues),
            violations=issues,
        )

    @staticmethod
    def _claim_supported(claim, refs, contract, workspace, node) -> bool:
        """Deterministic evidence entailment proxy for manifest bindings."""
        expanded = set(refs)
        for ref in list(refs):
            expanded.update(contract.evidence_links.get(ref, ()))
        texts = [
            contract.evidence[ref]
            for ref in expanded
            if ref in contract.evidence
        ]
        if not texts:
            return False
        evidence_text = "\n".join(texts)

        claim_numbers = QualityGate._extract_numeric_values(claim)
        evidence_numbers = QualityGate._extract_numeric_values(evidence_text)
        evidence_numbers.extend(
            QualityGate._extract_numeric_values(
                workspace.draft_sections.get(node.section_id, "")
            )
        )
        if claim_numbers and not all(
            value_matches(number, evidence_numbers) for number in claim_numbers
        ):
            return False

        claim_lower = claim.lower()
        mentioned_entities = [
            entity
            for entity in contract.entity_evidence
            if entity in claim_lower
        ]
        for entity in mentioned_entities:
            defining = set(contract.entity_evidence[entity])
            if expanded.isdisjoint(defining):
                return False
        if claim_numbers or mentioned_entities:
            return True

        claim_tokens = ArtifactCommitGate._semantic_tokens(claim)
        evidence_tokens = ArtifactCommitGate._semantic_tokens(evidence_text)
        return bool(claim_tokens & evidence_tokens)

    @staticmethod
    def _semantic_tokens(text: str) -> set[str]:
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{2,39}", text)
            if token.lower()
            not in {
                "the", "and", "with", "from", "using", "that", "this",
                "method", "model", "result", "results",
            }
        }
        cjk_chunks = re.findall(r"[\u3400-\u9fff]{2,}", text)
        for chunk in cjk_chunks:
            tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        return tokens

    @staticmethod
    def _evidence_realized(evidence_id: str, content: str, contract) -> bool:
        evidence_text = contract.evidence.get(evidence_id, "")
        if not evidence_text:
            return False
        normalized_content = re.sub(r"\s+", "", content).lower()
        normalized_evidence = re.sub(r"\s+", "", evidence_text).lower()
        if len(normalized_evidence) <= 240 and normalized_evidence in normalized_content:
            return True
        if evidence_id.startswith("experiment:"):
            experiment_id = evidence_id.split(":", 1)[1].lower()
            if experiment_id in content.lower():
                return True
        for entity, defining_ids in contract.entity_evidence.items():
            if evidence_id in defining_ids and entity in content.lower():
                return True
        evidence_tokens = ArtifactCommitGate._semantic_tokens(evidence_text)
        content_tokens = ArtifactCommitGate._semantic_tokens(content)
        overlap = len(evidence_tokens & content_tokens)
        return overlap >= min(6, max(2, len(evidence_tokens) // 5))

    @staticmethod
    def _issue(node: OutlineNode, issue_type: str, message: str) -> dict:
        return {
            "type": issue_type,
            "severity": "high",
            "section_id": node.section_id,
            "message": message,
        }


__all__ = [
    "ArtifactCommitGate",
    "ArtifactCommitResult",
    "build_claim_manifest",
]
