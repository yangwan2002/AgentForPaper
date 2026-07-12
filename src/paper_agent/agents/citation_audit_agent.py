"""引用审计智能体（草稿修订模式，Req 11）。

对用户初稿中的引用做三类可靠检查，产出"引用审计报告"写入工作区：
- ① 存在性：每条参考文献是否真实存在（按 DOI 或标题回查真实来源）。
- ② 元数据准确性：年份等是否与真实记录一致。
- ③ 引用-文献对应：正文引用编号与参考文献列表是否一一对应
  （悬空引用 / 冗余文献）。

不做"引用恰当性（④）"的自动判定——那属于语义层、易误判，留待后续作为
提示性功能。已核验为真实的文献会顺带写入已验证文献库，供后续写作引用。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.citation_parser import CitationParser
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry


class CitationAuditAgent(Agent):
    name = "citation_audit_agent"

    def __init__(self, parser: CitationParser, verifier: CitationVerifier) -> None:
        self._parser = parser
        self._verifier = verifier

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        draft = ws.original_draft or ""
        findings: list[dict] = []
        verified_refs: list[ReferenceEntry] = []
        alias_updates: dict[str, set[str]] = {}
        logs: list[str] = []

        if not draft.strip():
            return AgentResult(logs=["无初稿内容，跳过引用审计"])

        parsed = self._parser.parse(draft)
        logs.append(
            f"解析到参考文献 {len(parsed.references)} 条，"
            f"正文引用编号 {len(parsed.in_text_keys)} 个"
        )

        # ① 存在性 + ② 元数据
        existing_ids = {r.id for r in ws.verified_references}
        for i, ref in enumerate(parsed.references, start=1):
            result = self._verifier.verify_by_metadata(ref)
            if not result.exists:
                findings.append({
                    "type": "existence",
                    "severity": "high",
                    "ref_index": i,
                    "title": ref.title,
                    "message": f"参考文献[{i}] 疑似不存在或无法核验：{result.note}",
                })
                continue
            if result.year_matches is False:
                findings.append({
                    "type": "metadata",
                    "severity": "medium",
                    "ref_index": i,
                    "title": ref.title,
                    "message": f"参考文献[{i}] {result.note}",
                })
            # 核验为真实的文献入库，供写作引用（Req 4）。
            if result.matched is not None:
                real = result.matched
                alias = str(i)
                if real.id not in existing_ids:
                    marked = ReferenceEntry(
                        **{
                            **vars(real),
                            "verified": True,
                            "citation_aliases": sorted(
                                {*real.citation_aliases, alias}
                            ),
                        }
                    )
                    verified_refs.append(marked)
                    existing_ids.add(real.id)
                else:
                    alias_updates.setdefault(real.id, set()).add(alias)

        # ③ 引用-文献对应
        findings.extend(self._check_linkage(parsed))

        def mutate(w: PaperWorkspace) -> None:
            w.citation_audit = findings
            for reference in w.verified_references:
                additions = alias_updates.get(reference.id, set())
                if additions:
                    reference.citation_aliases = sorted(
                        {*reference.citation_aliases, *additions}
                    )
            w.verified_references.extend(verified_refs)

        logs.append(
            f"审计完成：发现 {len(findings)} 处问题，"
            f"核验入库真实文献 {len(verified_refs)} 条"
        )
        return AgentResult(mutations=[mutate], logs=logs)

    @staticmethod
    def _check_linkage(parsed) -> list[dict]:
        findings: list[dict] = []
        ref_count = len(parsed.references)
        ref_numbers = set(str(i) for i in range(1, ref_count + 1))
        cited = set(parsed.in_text_keys)

        # 悬空引用：正文引了，但参考文献列表没有对应编号。
        for key in parsed.in_text_keys:
            if key.isdigit() and key not in ref_numbers and ref_count > 0:
                findings.append({
                    "type": "linkage",
                    "severity": "high",
                    "message": f"正文引用了[{key}]，但参考文献列表无第 {key} 条（悬空引用）",
                })
        # 冗余文献：列表里有，但正文从未引用。
        for num in ref_numbers:
            if num not in cited:
                findings.append({
                    "type": "linkage",
                    "severity": "low",
                    "message": f"参考文献第 {num} 条从未在正文中被引用（可能冗余）",
                })
        return findings
