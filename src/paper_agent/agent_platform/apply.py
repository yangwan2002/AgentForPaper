"""单一写路径：把通过护栏闸门的更新意图原子落盘。

平台的**唯一**持久化出口。改工作区的工具只产出 ``ProposedChange``（不自行写入），
经护栏闸门 ``screen`` 后，仅 ``GateOutcome.accepted_mutations`` 经此落盘。由此在
结构上保证：

- 未过闸门的内容改动永不落盘（设计 Property 1）；
- 工作区变更只来自本模块（设计 Property 2）；
- 一批意图「全有或全无」，不留部分写入（设计 Property 3）。

批量原子性说明：既有 ``WorkspaceRepository.update`` 只对**单个** mutate 提供
「落盘失败回滚」，且其回滚只覆盖 *save* 失败（mutate 应用期异常在其 try 之外）。
故本模块把整批接受意图组合为**单个** mutate 一次性提交，并在外层对**应用期异常**
补一层回滚，使无论应用期还是落盘期失败，工作区都回到批前状态。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import GateOutcome, ProposedChange
from paper_agent.workspace.models import PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository


def apply_screened(
    repo: WorkspaceRepository, ws: PaperWorkspace, outcome: GateOutcome
) -> PaperWorkspace:
    """把 ``outcome.accepted_mutations`` 作为一个原子批次落盘（设计 Property 2/3）。

    无接受意图时为 no-op（不触盘、不改状态）。任一意图应用或落盘失败 → 工作区
    回滚至批前状态并向上抛出，绝不留部分写入。
    """
    mutations = outcome.accepted_mutations
    if not mutations:
        return ws

    snapshot = copy.deepcopy(ws.to_dict())

    def _composed(target: PaperWorkspace) -> None:
        for mutation in mutations:
            mutation(target)

    try:
        return repo.update(ws, _composed)
    except Exception:
        # 兜底 all-or-nothing：覆盖 repo.update 未处理的「应用期异常」路径。
        restored = PaperWorkspace.from_dict(snapshot)
        ws.__dict__.update(restored.__dict__)
        raise


def commit(
    repo: WorkspaceRepository,
    ws: PaperWorkspace,
    gate: GuardrailGate,
    changes: list[ProposedChange],
) -> GateOutcome:
    """改工作区工具的统一落盘出口：先过闸门，再原子落盘可接受意图。

    返回 ``GateOutcome`` 供调用方（Agent_Loop）读取被拒改动与差额说明，
    以便回灌智能体修正或如实上报用户。
    """
    outcome = gate.screen(ws, changes)
    apply_screened(repo, ws, outcome)
    return outcome


__all__ = ["apply_screened", "commit"]
